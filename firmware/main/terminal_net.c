// main/terminal_net.c — Wi-Fi STA + 原生 TCP 载体。
//
// 为什么是裸 TCP 而不是 WebSocket：协议本来就是"一行一条 JSON"的字节流，裸 TCP 零外部
// 依赖（lwIP 自带）、体积更小、重连逻辑完全可控；WebSocket 只在"需要过 HTTP 代理或被
// 浏览器直连"时才有额外价值，将来要加也只是多一个 sink，命令层不用动。
#include "terminal_net.h"

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "terminal_link.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *TAG = "net";

#define LINE_MAX 512
#define RETRY_MS 3000
#define CONNECT_TIMEOUT_S 5
#define NVS_NS "dshtc"

static int s_sock = -1;
static volatile bool s_connected;
static volatile bool s_ip_ready;
static volatile bool s_scanning;  // 扫描期间抑制自动重连，否则状态机抢不到扫描权
static bool s_cfg_loaded;         // NVS 里的网络参数是否已读进内存
static bool s_wifi_started;       // esp_wifi_start() 是否已执行
static SemaphoreHandle_t s_tx_lock;
static char s_line[LINE_MAX];
static size_t s_line_len;

static char s_ssid[33];
static char s_password[65];
static char s_host[64];
static int s_port;

// ---------------------------------------------------------------- 配置持久化

static void nvs_str_or(nvs_handle_t h, const char *key, char *dst, size_t dstsz,
                       const char *fallback) {
    size_t len = dstsz;
    if (nvs_get_str(h, key, dst, &len) != ESP_OK) {
        strncpy(dst, fallback, dstsz - 1);
        dst[dstsz - 1] = '\0';
    }
}

static void config_load(void) {
    strncpy(s_ssid, CONFIG_TERMINAL_WIFI_SSID, sizeof s_ssid - 1);
    strncpy(s_password, CONFIG_TERMINAL_WIFI_PASSWORD, sizeof s_password - 1);
    strncpy(s_host, CONFIG_TERMINAL_SERVER_HOST, sizeof s_host - 1);
    s_port = CONFIG_TERMINAL_SERVER_PORT;

    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGW(TAG, "NVS 打不开，使用编译期默认网络参数");
        return;
    }
    nvs_str_or(h, "ssid", s_ssid, sizeof s_ssid, CONFIG_TERMINAL_WIFI_SSID);
    nvs_str_or(h, "pass", s_password, sizeof s_password, CONFIG_TERMINAL_WIFI_PASSWORD);
    nvs_str_or(h, "host", s_host, sizeof s_host, CONFIG_TERMINAL_SERVER_HOST);

    int32_t port = 0;
    if (nvs_get_i32(h, "port", &port) == ESP_OK && port > 0 && port < 65536) {
        s_port = (int)port;
    } else {
        s_port = CONFIG_TERMINAL_SERVER_PORT;
    }

    // 首次启动把当前值落盘，之后即使换固件默认值也不会悄悄改变设备配置。
    size_t probe = 0;
    if (nvs_get_str(h, "ssid", NULL, &probe) != ESP_OK) {
        nvs_set_str(h, "ssid", s_ssid);
        nvs_set_str(h, "pass", s_password);
        nvs_set_str(h, "host", s_host);
        nvs_set_i32(h, "port", s_port);
        nvs_commit(h);
        ESP_LOGI(TAG, "网络配置首次写入 NVS");
    }
    nvs_close(h);
}

static void config_save(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_str(h, "ssid", s_ssid);
    nvs_set_str(h, "pass", s_password);
    nvs_set_str(h, "host", s_host);
    nvs_set_i32(h, "port", s_port);
    nvs_commit(h);
    nvs_close(h);
}

// ---------------------------------------------------------------- 发送/接收

static void tcp_sink(const char *line, size_t len) {
    if (!s_connected || s_sock < 0 || len == 0 || !s_tx_lock) return;
    if (xSemaphoreTake(s_tx_lock, pdMS_TO_TICKS(100)) != pdTRUE) return;
    size_t off = 0;
    while (off < len) {
        const int w = send(s_sock, line + off, len - off, 0);
        if (w <= 0) break;
        off += (size_t)w;
    }
    xSemaphoreGive(s_tx_lock);
}

static void drop_link(const char *why) {
    if (s_sock >= 0) {
        close(s_sock);
        s_sock = -1;
    }
    if (s_connected) ESP_LOGW(TAG, "连接断开(%s)，%d ms 后重连", why, RETRY_MS);
    s_connected = false;
}

// 收到的字节按 '\n' 切行后分发，与 USB 载体行为一致。
static void feed(const char *data, int len) {
    for (int i = 0; i < len; i++) {
        const char ch = data[i];
        if (ch == '\r') continue;
        if (ch == '\n') {
            s_line[s_line_len] = '\0';
            if (s_line_len > 0) terminal_link_dispatch(s_line);
            s_line_len = 0;
        } else if (s_line_len < sizeof(s_line) - 1) {
            s_line[s_line_len++] = ch;
        } else {
            s_line_len = 0;
        }
    }
}

static int dial(void) {
    char port[8];
    snprintf(port, sizeof port, "%d", s_port);

    struct addrinfo hints;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;

    struct addrinfo *res = NULL;
    if (getaddrinfo(s_host, port, &hints, &res) != 0 || !res) {
        ESP_LOGW(TAG, "解析 %s 失败", s_host);
        return -1;
    }

    const int s = socket(res->ai_family, res->ai_socktype, 0);
    if (s < 0) {
        freeaddrinfo(res);
        return -1;
    }

    // 非阻塞 connect + select 超时。阻塞式 connect 在"对端丢包/被防火墙静默丢弃"
    // 时会一直卡在 SYN 重传里（几十秒到两分钟），任务挂住且不报错，现场只能看到
    // 设备一片沉默。宁可显式失败，也不要无声地卡住。
    const int flags = fcntl(s, F_GETFL, 0);
    fcntl(s, F_SETFL, flags | O_NONBLOCK);

    int rc = connect(s, res->ai_addr, res->ai_addrlen);
    if (rc != 0 && errno != EINPROGRESS) {
        ESP_LOGW(TAG, "连接 %s:%d 失败 errno=%d", s_host, s_port, errno);
        close(s);
        freeaddrinfo(res);
        return -1;
    }
    if (rc != 0) {
        fd_set wf;
        FD_ZERO(&wf);
        FD_SET(s, &wf);
        struct timeval tv = { .tv_sec = CONNECT_TIMEOUT_S, .tv_usec = 0 };
        if (select(s + 1, NULL, &wf, NULL, &tv) <= 0) {
            ESP_LOGW(TAG, "连接 %s:%d 超时（%d s）", s_host, s_port, CONNECT_TIMEOUT_S);
            close(s);
            freeaddrinfo(res);
            return -1;
        }
        int so_err = 0;
        socklen_t len = sizeof so_err;
        getsockopt(s, SOL_SOCKET, SO_ERROR, &so_err, &len);
        if (so_err != 0) {
            ESP_LOGW(TAG, "连接 %s:%d 失败 errno=%d", s_host, s_port, so_err);
            close(s);
            freeaddrinfo(res);
            return -1;
        }
    }
    freeaddrinfo(res);

    fcntl(s, F_SETFL, flags);  // 恢复阻塞模式供 recv 使用
    const int one = 1;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);  // 交互按键要低延迟
    setsockopt(s, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof one);  // 半死连接能被发现

    s_sock = s;
    return 0;
}

static void net_task(void *arg) {
    (void)arg;
    char buf[256];

    for (;;) {
        if (!s_ip_ready) {
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }
        if (!s_connected) {
            if (dial() == 0) {
                s_connected = true;
                s_line_len = 0;
                ESP_LOGI(TAG, "已连上服务器 %s:%d", s_host, s_port);
                terminal_link_send("{\"event\":\"ready\",\"link\":\"net\"}\n");
            } else {
                vTaskDelay(pdMS_TO_TICKS(RETRY_MS));
            }
            continue;
        }
        const int n = recv(s_sock, buf, sizeof buf, 0);
        if (n <= 0) {
            drop_link("peer closed");
            vTaskDelay(pdMS_TO_TICKS(RETRY_MS));
        } else {
            feed(buf, n);
        }
    }
}

// ---------------------------------------------------------------- Wi-Fi

static void wifi_apply_sta(void) {
    wifi_config_t wc;
    memset(&wc, 0, sizeof wc);
    strncpy((char *)wc.sta.ssid, s_ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, s_password, sizeof(wc.sta.password) - 1);
    wc.sta.threshold.authmode = (s_password[0] != '\0') ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;

    esp_wifi_disconnect();
    esp_wifi_set_config(WIFI_IF_STA, &wc);
    esp_wifi_connect();
}

static void net_event_handler(void *arg, esp_event_base_t base, int32_t event_id, void *data) {
    (void)arg;
    if (base == WIFI_EVENT) {
        if (event_id == WIFI_EVENT_STA_START) {
            esp_wifi_connect();
        } else if (event_id == WIFI_EVENT_STA_DISCONNECTED) {
            const wifi_event_sta_disconnected_t *d = (const wifi_event_sta_disconnected_t *)data;
            s_ip_ready = false;
            drop_link("wifi down");
            // reason 是关键诊断信息：201=没扫到该 AP（常见于 5GHz 或信号太弱），
            // 15/204=四次握手失败（密码错），205=连接被 AP 拒绝。
            ESP_LOGW(TAG, "Wi-Fi 断开 reason=%d，重连中", d ? d->reason : -1);
            if (!s_scanning) esp_wifi_connect();
        }
    } else if (base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *ev = (const ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "拿到 IP " IPSTR, IP2STR(&ev->ip_info.ip));
        s_ip_ready = true;
    }
}

void terminal_net_preload(void) {
    if (s_cfg_loaded) return;

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "NVS 初始化失败(%s)", esp_err_to_name(err));
        return;
    }
    config_load();
    s_cfg_loaded = true;
}

esp_err_t terminal_net_start(void) {
    s_tx_lock = xSemaphoreCreateMutex();

    // 配置要在 USB 通道开始受理命令之前就读好，否则 get_net 会答出一串空字段。
    terminal_net_preload();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        net_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        net_event_handler, NULL, NULL));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

    wifi_config_t wc;
    memset(&wc, 0, sizeof wc);
    strncpy((char *)wc.sta.ssid, s_ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, s_password, sizeof(wc.sta.password) - 1);
    wc.sta.threshold.authmode = (s_password[0] != '\0') ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));

    terminal_link_register(tcp_sink);

    if (xTaskCreate(net_task, "dsh_net", 4096, NULL, 5, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "连接 Wi-Fi \"%s\"，服务器 %s:%d", s_ssid, s_host, s_port);
    const esp_err_t start_err = esp_wifi_start();
    if (start_err == ESP_OK) s_wifi_started = true;
    return start_err;
}

esp_err_t terminal_net_apply(const char *ssid, const char *password, const char *host, int port) {
    if (ssid && *ssid) {
        strncpy(s_ssid, ssid, sizeof s_ssid - 1);
        s_ssid[sizeof s_ssid - 1] = '\0';
    }
    if (password) {
        strncpy(s_password, password, sizeof s_password - 1);
        s_password[sizeof s_password - 1] = '\0';
    }
    if (host && *host) {
        strncpy(s_host, host, sizeof s_host - 1);
        s_host[sizeof s_host - 1] = '\0';
    }
    if (port > 0 && port < 65536) s_port = port;

    config_save();

    // USB 通道比 Wi-Fi 初始化更早就绪，主机若在启动瞬间下发 set_net，此时驱动还不存在，
    // 直接调 esp_wifi_* 是未定义行为。只落盘，交给随后的初始化生效。
    if (!s_wifi_started) {
        ESP_LOGI(TAG, "网络参数已保存，将在 Wi-Fi 初始化后生效: ssid=\"%s\" server=%s:%d", s_ssid,
                 s_host, s_port);
        return ESP_OK;
    }

    drop_link("reconfigured");
    wifi_apply_sta();

    ESP_LOGI(TAG, "网络参数已更新: ssid=\"%s\" server=%s:%d", s_ssid, s_host, s_port);
    return ESP_OK;
}

void terminal_net_describe(char *buf, size_t n) {
    if (!buf || n == 0) return;
    snprintf(buf, n, "\"ssid\":\"%s\",\"host\":\"%s\",\"port\":%d,\"password_set\":%s", s_ssid,
             s_host, s_port, (s_password[0] != '\0') ? "true" : "false");
}

// 扫描周边 AP，用于现场诊断"为什么连不上"：芯片只支持 2.4 GHz，5 GHz-only 的 SSID
// 在这里根本不会出现。
esp_err_t terminal_net_scan(char *buf, size_t n) {
    if (!buf || n < 8) return ESP_ERR_INVALID_ARG;

    wifi_scan_config_t sc;
    memset(&sc, 0, sizeof sc);
    sc.show_hidden = true;

    // esp_wifi_scan_start 在"正在连接"状态下会返回 ESP_ERR_WIFI_STATE，
    // 所以先主动断开并让重连逻辑暂停，扫完再交还。
    s_scanning = true;
    esp_wifi_disconnect();
    vTaskDelay(pdMS_TO_TICKS(200));

    esp_err_t err = esp_wifi_scan_start(&sc, true);  // 阻塞至扫描完成
    if (err != ESP_OK) {
        s_scanning = false;
        esp_wifi_connect();
        return err;
    }

    uint16_t found = 0;
    esp_wifi_scan_get_ap_num(&found);
    if (found > 12) found = 12;

    size_t used = 0;
    used += (size_t)snprintf(buf + used, n - used, "\"count\":%u,\"aps\":[", (unsigned)found);

    if (found > 0) {
        wifi_ap_record_t *recs = calloc(found, sizeof *recs);
        if (!recs) return ESP_ERR_NO_MEM;
        uint16_t got = found;
        if (esp_wifi_scan_get_ap_records(&got, recs) == ESP_OK) {
            for (uint16_t i = 0; i < got && used + 64 < n; i++) {
                used += (size_t)snprintf(buf + used, n - used, "%s{\"ssid\":\"%s\",\"rssi\":%d}",
                                         (i == 0) ? "" : ",", (const char *)recs[i].ssid,
                                         recs[i].rssi);
            }
        }
        free(recs);
    }
    snprintf(buf + used, n - used, "]");
    s_scanning = false;
    esp_wifi_connect();
    return ESP_OK;
}

bool terminal_net_ready(void) { return s_connected; }

bool terminal_net_ip_ready(void) { return s_ip_ready; }