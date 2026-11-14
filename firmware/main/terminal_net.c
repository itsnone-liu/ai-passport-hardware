// main/terminal_net.c — Wi-Fi STA + 原生 TCP 载体。
//
// 为什么是裸 TCP 而不是 WebSocket：协议本来就是"一行一条 JSON"的字节流，裸 TCP 零外部
// 依赖（lwIP 自带）、体积更小、重连逻辑完全可控；WebSocket 只在"需要过 HTTP 代理或被
// 浏览器直连"时才有额外价值，将来要加也只是多一个 sink，命令层不用动。
#include "terminal_net.h"

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "terminal_link.h"
#include "terminal_auth.h"
#include "terminal_pav1.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *TAG = "net";

#define LINE_MAX 512
#define RETRY_MS 2400          // 首次重连间隔（处暑建议 2.4 s 起）
#define RETRY_MAX_MS 30000     // 指数退避封顶
#define IDLE_DROP_S 180        // 控制通道完全静默这么久就认为流已死，弃掉重拨
#define LINK_OK_US (5 * 1000 * 1000)  // 连接活过 5 s 才算"真的连上了"
#define CONNECT_TIMEOUT_S 5
#define MAX_SCAN_APS 12   // 12 个够现场判断"2.4G 有没有这个 SSID"了
#define NVS_NS "dshtc"

static int s_sock = -1;
static volatile bool s_connected;
static volatile bool s_ip_ready;
static volatile bool s_link_dead;  // tcp_sink 发现发不出去，交给 net_task 处理
static volatile bool s_scanning;  // 扫描期间抑制自动重连，否则状态机抢不到扫描权
static bool s_cfg_loaded;         // NVS 里的网络参数是否已读进内存
static bool s_wifi_started;       // esp_wifi_start() 是否已执行
static uint32_t s_retry_ms = RETRY_MS;
static int s_retry_count;
static int64_t s_last_rx_us;
static int64_t s_conn_us;  // 本次连接建立的时刻：用来判断"连上就断"不算真连上
static SemaphoreHandle_t s_tx_lock;
static char s_line[LINE_MAX];
static size_t s_line_len;
static bool s_rx_discard;  // 正在丢弃一条超长行（见 feed）

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
        if (w <= 0) {
            // 发不出去就是链路已死。旧写法只 break，net_task 毫不知情，连接会一直
            // 挂着"已连接"却什么也发不出去。标记出来交给 net_task 弃掉重拨。
            s_link_dead = true;
            break;
        }
        off += (size_t)w;
    }
    xSemaphoreGive(s_tx_lock);
}

static void drop_link(const char *why);

// 连接失败后的等待：2.4 s 起每次翻倍，封顶 30 s。服务端重启、路由器抖动的恢复时间
// 不可预测，固定间隔会在长时间不可用时变成无意义的密集重试。
static void backoff(void) {
    s_retry_count++;
    const uint32_t delay_ms = s_retry_ms;
    if (s_retry_ms < RETRY_MAX_MS) s_retry_ms *= 2;
    if (s_retry_ms > RETRY_MAX_MS) s_retry_ms = RETRY_MAX_MS;
    ESP_LOGW(TAG, "第 %d 次重连，%u ms 后重试", s_retry_count, (unsigned)delay_ms);
    vTaskDelay(pdMS_TO_TICKS(delay_ms));
}

// 断线统一入口：丢弃链路，再决定是"快速重拨"还是"退避"。
// 判据是这一条连接活了多久——能建立不等于可用。对端 accept 之后立刻 close、或者
// 中间设备把流量吞掉，都会表现为"连上就断"；若每次成功都清零退避，就会变成
// 2.4 s 一轮的永久快循环，把重试预算全烧在一条坏链路上。
static void link_lost(const char *why) {
    const int64_t lived = (s_conn_us > 0) ? (esp_timer_get_time() - s_conn_us) : 0;
    terminal_auth_reset();  // session_id 随连接一起作废（§7.4）
    terminal_pav1_stop();   // 控制通道没了，音频会话也失去归属（§3.3）
    drop_link(why);
    if (lived > LINK_OK_US) {
        s_retry_ms = RETRY_MS;  // 曾经用通过，回到快速重拨
        s_retry_count = 0;
    } else {
        ESP_LOGW(TAG, "本次连接只活了 %d ms，按失败计", (int)(lived / 1000));
    }
    backoff();
}

static void drop_link(const char *why) {
    // 先摘掉发送资格、再关 fd，且整个过程持锁：否则 tcp_sink 可能正在 send() 一个
    // 刚被别的任务 close() 掉的描述符（轻则 EBADF，重则把数据写到复用后的新连接上）。
    int fd = -1;
    const bool was_connected = s_connected;
    s_connected = false;
    if (s_tx_lock && xSemaphoreTake(s_tx_lock, pdMS_TO_TICKS(200)) == pdTRUE) {
        fd = s_sock;
        s_sock = -1;
        xSemaphoreGive(s_tx_lock);
    } else {
        fd = s_sock;
        s_sock = -1;
    }
    if (fd >= 0) close(fd);
    if (was_connected) ESP_LOGW(TAG, "连接断开(%s)，%d ms 后重连", why, RETRY_MS);
}

// 收到的字节按 '\n' 切行后分发，与 USB 载体行为一致。
static void feed(const char *data, int len) {
    // 超长行必须整行丢弃直到换行符：旧写法把 len 清零后继续接收，行尾那截会被当成
    // 一条新命令去解析，等于把半截命令注入到协议里。
    for (int i = 0; i < len; i++) {
        const char ch = data[i];
        if (ch == '\r') continue;
        if (ch == '\n') {
            if (s_rx_discard) {
                s_rx_discard = false;
                terminal_link_send("{\"ok\":false,\"err\":\"line_too_long\"}\n");
            } else {
                s_line[s_line_len] = '\0';
                if (s_line_len > 0) {
                    // 应答只回这条 TCP 连接：USB 同时在线时不能把应答广播过去。
                    static const link_reply_t k_tcp_reply = {LINK_SRC_TCP, tcp_sink};
                    terminal_link_dispatch(s_line, &k_tcp_reply);
                }
            }
            s_line_len = 0;
        } else if (s_rx_discard) {
            // 继续丢弃到行尾
        } else if (s_line_len < sizeof(s_line) - 1) {
            s_line[s_line_len++] = ch;
        } else {
            s_rx_discard = true;
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

    // recv 必须有超时。NAT 映射过期后，路由器会把这条流静默丢掉：服务端早已
    // close（对端停在 FIN-WAIT，因为 FIN 再也到不了设备），而设备这边的阻塞 recv
    // 会永远等下去——表现是"Wi-Fi 连着、控制通道没了、也不重连"，一次重启服务就
    // 能让设备静默到重启为止。2 s 一次的 EAGAIN 让心跳与死亡判断有地方落。
    struct timeval rcv = { .tv_sec = 2, .tv_usec = 0 };
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &rcv, sizeof rcv);

    // 尽快发现死链路：默认 keepalive 要 2 小时，等于没有。这三个参数依赖
    // CONFIG_LWIP_TCP_KEEPALIVE，没开时 setsockopt 会失败，此时靠空闲超时兜底。
    const int idle = 10, intvl = 5, cnt = 3;
    if (setsockopt(s, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof idle) != 0 ||
        setsockopt(s, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof intvl) != 0 ||
        setsockopt(s, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof cnt) != 0) {
        ESP_LOGW(TAG, "keepalive 参数未生效（CONFIG_LWIP_TCP_KEEPALIVE 未开），靠空闲超时兜底");
    }

    s_sock = s;
    return 0;
}

static void net_task(void *arg) {
    (void)arg;
    char buf[256];
    int no_ip_ticks = 0;

    for (;;) {
        if (!s_ip_ready) {
            // 关联着却迟迟拿不到 IP（NAT/租约/DHCP 异常）同样是静默状态：不重试就永远不动。
            // 60 s 还没有 IP 就主动断开重连一次，把决定权交回给驱动。
            if (++no_ip_ticks >= 120) {
                no_ip_ticks = 0;
                ESP_LOGW(TAG, "60 s 没有 IP，强制重新关联");
                esp_wifi_disconnect();
                esp_wifi_connect();
            }
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }
        no_ip_ticks = 0;

        if (s_link_dead && s_connected) {
            link_lost("send failed");
            continue;
        }
        if (!s_connected) {
            s_link_dead = false;
            if (dial() == 0) {
                s_connected = true;
                s_line_len = 0;
                s_rx_discard = false;
                s_last_rx_us = esp_timer_get_time();
                s_conn_us = s_last_rx_us;
                ESP_LOGI(TAG, "已连上服务器 %s:%d", s_host, s_port);
                terminal_link_start_net_session();  // 握手：先 hello，认证通过后才由 dispatch 发 ready
            } else {
                backoff();
            }
            continue;
        }

        const int n = recv(s_sock, buf, sizeof buf, 0);
        if (n > 0) {
            s_last_rx_us = esp_timer_get_time();
            feed(buf, n);
            continue;
        }
        if (n == 0) {
            // 对端主动 close：立刻重拨，不要等 Wi-Fi 事件（服务器重启时根本不会有）。
            link_lost("peer closed");
            continue;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            // 2 s 无数据是正常的；长时间完全静默说明这条流已经死了（NAT 超时、对端僵死），
            // 这种连接不会自己恢复，只能弃掉重拨。
            const auth_act_t act = terminal_auth_tick_step();
            if (act == AUTH_ACT_READY) {
                terminal_link_emit_net_ready();
            } else if (act == AUTH_ACT_FAILED || terminal_auth_failed()) {
                link_lost("auth failed");
                continue;
            }
            if (esp_timer_get_time() - s_last_rx_us > IDLE_DROP_S * 1000000LL) {
                link_lost("idle timeout");
            }
            continue;
        }
        link_lost("recv error");
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
        // 实时语音链路必须关掉 Wi-Fi 省电。默认是 WIFI_PS_MIN_MODEM，设备每
        // DTIM 周期才醒一次收包（实测 listen interval 300 ms），下行数据在 AP
        // 那里排队等设备醒来，表现为接收速率"一阵一阵"、低速脉冲，而且会把
        // RTT 凭空拉长。省的那点电换不来一句正常的话。
        const esp_err_t ps_err = esp_wifi_set_ps(WIFI_PS_NONE);
        ESP_LOGI(TAG, "关闭 Wi-Fi 省电：%s", esp_err_to_name(ps_err));
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

    terminal_link_register(LINK_SRC_TCP, tcp_sink);

    if (xTaskCreate(net_task, "dsh_net", 4096, NULL, 5, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "连接 Wi-Fi \"%s\"，服务器 %s:%d", s_ssid, s_host, s_port);
    const esp_err_t start_err = esp_wifi_start();
    if (start_err == ESP_OK) s_wifi_started = true;
    return start_err;
}

esp_err_t terminal_net_set(const char *ssid, const char *password, const char *host, int port) {
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
    }
    return ESP_OK;
}

// 拆成 set + reconnect 两步，是为了让回执先走完再断链路：重配网络必然要掐掉当前
// TCP，如果先断开再回执，发起这条命令的那条连接已经不存在，回执永远到不了对方，
// 主机只能看到"命令发出、连接断了、不知道成没成"。
void terminal_net_reconnect(void) {
    if (!s_wifi_started) return;
    drop_link("reconfigured");
    wifi_apply_sta();
    ESP_LOGI(TAG, "网络参数已更新: ssid=\"%s\" server=%s:%d", s_ssid, s_host, s_port);
}

void terminal_net_kill(const char *why) {
    // 只置标志：真正的 close 由 net_task 做，避免在两个任务里同时动 fd 与锁。
    ESP_LOGW(TAG, "主动弃掉当前会话：%s", why ? why : "?");
    s_link_dead = true;
}

esp_err_t terminal_net_apply(const char *ssid, const char *password, const char *host, int port) {
    const esp_err_t err = terminal_net_set(ssid, password, host, port);
    if (err == ESP_OK) terminal_net_reconnect();
    return err;
}

// 每次续写都夹住 used，避免 snprintf 返回"本想写多长"导致 used > n、
// 进而让 n - used 这个 size_t 减法下溢成天文数字。
static size_t str_append(char *dst, size_t n, size_t used, const char *s) {
    if (used >= n) return n - 1;
    const int w = snprintf(dst + used, n - used, "%s", s);
    if (w < 0) return used;
    const size_t next = used + (size_t)w;
    return next >= n ? n - 1 : next;
}

// 把原始字节转义成 JSON 字符串内容（不含两侧引号）。SSID 允许包含引号、反斜杠和任意
// 8-bit 字节，直接 %s 拼进去会生成非法 JSON，主机侧整条消息都解析失败。
static size_t json_escape(char *dst, size_t dstn, const char *src) {
    if (!dst || dstn == 0) return 0;
    size_t w = 0;
    for (const unsigned char *p = (const unsigned char *)src; p && *p; p++) {
        const char *esc = NULL;
        char uni[8];
        if (*p == '"') esc = "\\\"";
        else if (*p == '\\') esc = "\\\\";
        else if (*p == '\n') esc = "\\n";
        else if (*p == '\r') esc = "\\r";
        else if (*p == '\t') esc = "\\t";
        else if (*p < 0x20) {
            snprintf(uni, sizeof uni, "\\u%04x", *p);
            esc = uni;
        }
        if (esc) {
            const size_t len = strlen(esc);
            if (w + len >= dstn) break;
            memcpy(dst + w, esc, len);
            w += len;
        } else {
            if (w + 1 >= dstn) break;
            dst[w++] = (char)*p;
        }
    }
    dst[w] = '\0';
    return w;
}

void terminal_net_describe(char *buf, size_t n) {
    if (!buf || n == 0) return;
    char ssid[6 * 33];
    char host[6 * 65];
    json_escape(ssid, sizeof ssid, s_ssid);
    json_escape(host, sizeof host, s_host);
    snprintf(buf, n, "\"ssid\":\"%s\",\"host\":\"%s\",\"port\":%d,\"password_set\":%s", ssid, host,
             s_port, (s_password[0] != '\0') ? "true" : "false");
}

const char *terminal_net_host(void) { return s_host; }
int terminal_net_port(void) { return s_port; }

// 扫描周边 AP，用于现场诊断"为什么连不上"：芯片只支持 2.4 GHz，5 GHz-only 的 SSID
// 在这里根本不会出现。
esp_err_t terminal_net_scan(char *buf, size_t n) {
    if (!buf || n < 8) return ESP_ERR_INVALID_ARG;

    // 扫描是唯一的"会主动踢掉当前关联"的操作，两个并发扫描会把状态机搅乱，
    // 也会让后一个的收尾去重连一个已经不该重连的连接。直接回绝。
    if (s_scanning) return ESP_ERR_INVALID_STATE;

    wifi_scan_config_t sc;
    memset(&sc, 0, sizeof sc);
    sc.show_hidden = true;

    // esp_wifi_scan_start 在"正在连接"状态下会返回 ESP_ERR_WIFI_STATE，
    // 所以先主动断开并让重连逻辑暂停，扫完再交还。
    s_scanning = true;
    esp_wifi_disconnect();
    vTaskDelay(pdMS_TO_TICKS(200));

    esp_err_t err = ESP_OK;
    uint16_t found = 0;
    uint16_t total = 0;  // 芯片扫到的总数，用来判断输出是否被截断
    wifi_ap_record_t *recs = NULL;

    // 单一出口：任何一条失败路径都必须把 s_scanning 清掉并重新发起连接，
    // 否则自动重连的抑制永久生效，设备扫描一次就静默掉线，再也连不回来。
    err = esp_wifi_scan_start(&sc, true);  // 阻塞至扫描完成
    if (err != ESP_OK) goto done;

    esp_wifi_scan_get_ap_num(&found);
    total = found;
    if (found > MAX_SCAN_APS) found = MAX_SCAN_APS;

    if (found > 0) {
        recs = calloc(found, sizeof *recs);
        if (!recs) {
            err = ESP_ERR_NO_MEM;
            goto done;
        }
        uint16_t got = found;
        const esp_err_t rc = esp_wifi_scan_get_ap_records(&got, recs);
        if (rc != ESP_OK) {
            err = rc;
            goto done;
        }
        found = got;
    }

    // 数组在前、计数在后：count 永远是"真正写出去的数量"，不会出现 count=12 而
    // 数组只有 3 项这种自相矛盾的输出（缓冲不够时会截断，但截断的仍是合法 JSON）。
    size_t used = str_append(buf, n, 0, "\"aps\":[");
    unsigned emitted = 0;
    for (uint16_t i = 0; i < found; i++) {
        char esc[6 * 33];
        char entry[6 * 33 + 40];
        json_escape(esc, sizeof esc, (const char *)recs[i].ssid);
        const int need = snprintf(entry, sizeof entry, "%s{\"ssid\":\"%s\",\"rssi\":%d}",
                                 (emitted == 0) ? "" : ",", esc, recs[i].rssi);
        if (need < 0) continue;
        if (used + (size_t)need + 32 >= n) break;  // 留出 "],"count":NNN 的收尾空间
        used = str_append(buf, n, used, entry);
        emitted++;
    }
    used = str_append(buf, n, used, "],\"count\":");
    char num[8];
    snprintf(num, sizeof num, "%u", emitted);
    used = str_append(buf, n, used, num);
    // 只在真截断时带 truncated：服务端据此知道列表不完整，而不是以为附近只有这么几个 AP。
    if (emitted < total) str_append(buf, n, used, ",\"truncated\":true");

done:
    free(recs);
    s_scanning = false;
    esp_wifi_connect();
    return err;
}

bool terminal_net_ready(void) { return s_connected; }

bool terminal_net_ip_ready(void) { return s_ip_ready; }
// ---- 下行吞吐隔离测试：对任意 TCP 主机猛读，量真实的字节速率 ----
// 不经过 PAV1、不经过播放、不经过处暑的服务器。用来把"链路/lwIP/读循环"
// 和"协议/播放"彻底切开：本机起一个只会灌数据的 sink 就能做 A/B。
static void sink_task(void *arg) {
    char *spec = (char *)arg;  // "host port ms"
    char host[48] = {0};
    int port = 0, ms = 3000;
    if (sscanf(spec, "%47s %d %d", host, &port, &ms) != 3) {
        terminal_link_send_src(LINK_SRC_USB,
                               "{\"event\":\"tcp_sink\",\"ok\":false,\"err\":\"bad_args\"}\n");
        free(spec);
        vTaskDelete(NULL);
        return;
    }
    free(spec);
    struct addrinfo hints = {0}, *res = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    char portstr[8];
    snprintf(portstr, sizeof portstr, "%d", port);
    if (getaddrinfo(host, portstr, &hints, &res) != 0 || !res) {
        terminal_link_send_src(LINK_SRC_USB,
                               "{\"event\":\"tcp_sink\",\"ok\":false,\"err\":\"resolve\"}\n");
        vTaskDelete(NULL);
        return;
    }
    const int fd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd < 0) {
        freeaddrinfo(res);
        terminal_link_send_src(LINK_SRC_USB,
                               "{\"event\":\"tcp_sink\",\"ok\":false,\"err\":\"socket\"}\n");
        vTaskDelete(NULL);
        return;
    }
    const struct timeval rcv = {.tv_sec = 1, .tv_usec = 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &rcv, sizeof rcv);
    int rcvbuf = 16384;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof rcvbuf);
    if (connect(fd, res->ai_addr, res->ai_addrlen) != 0) {
        close(fd);
        freeaddrinfo(res);
        terminal_link_send_src(LINK_SRC_USB,
                               "{\"event\":\"tcp_sink\",\"ok\":false,\"err\":\"connect\"}\n");
        vTaskDelete(NULL);
        return;
    }
    freeaddrinfo(res);
    static uint8_t buf[4096];
    uint32_t total = 0;
    int calls = 0;
    const int64_t t0 = esp_timer_get_time();
    while ((esp_timer_get_time() - t0) / 1000 < ms) {
        const int n = recv(fd, buf, sizeof buf, 0);
        calls++;
        if (n > 0) {
            total += (uint32_t)n;
        } else if (n == 0) {
            break;
        } else if (errno != EAGAIN && errno != EWOULDBLOCK) {
            break;
        }
    }
    const int elapsed = (int)((esp_timer_get_time() - t0) / 1000);
    close(fd);
    const uint32_t kbps_x10 = elapsed ? (uint32_t)((uint64_t)total * 10 / (uint64_t)elapsed) : 0;
    ESP_LOGI(TAG, "tcp_sink: %u B / %d ms = %u.%u KB/s，%d 次 recv", (unsigned)total, elapsed,
             (unsigned)(kbps_x10 / 10), (unsigned)(kbps_x10 % 10), calls);
    char ev[288];
    snprintf(ev, sizeof ev,
             "{\"event\":\"tcp_sink\",\"ok\":true,\"host\":\"%s\",\"port\":%d,\"ms\":%d,"
             "\"bytes\":%u,\"kbps\":%u.%u,\"calls\":%d,\"heap\":%u,\"heap_min\":%u}\n",
             host, port, elapsed, (unsigned)total, (unsigned)(kbps_x10 / 10),
             (unsigned)(kbps_x10 % 10), calls,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT));
    terminal_link_send_src(LINK_SRC_USB, ev);
    vTaskDelete(NULL);
}

esp_err_t terminal_net_sink_test(const char *host, int port, int ms) {
    char *spec = malloc(64);
    if (!spec) return ESP_ERR_NO_MEM;
    snprintf(spec, 64, "%s %d %d", host, port, ms);
    if (xTaskCreate(sink_task, "sink", 4096, spec, 4, NULL) != pdPASS) {
        free(spec);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

// 运行时切换 Wi-Fi 省电模式，用于 A/B：0=NONE 1=MIN_MODEM 2=MAX_MODEM
esp_err_t terminal_net_set_ps(int mode) {
    return esp_wifi_set_ps(mode == 0 ? WIFI_PS_NONE
                                     : (mode == 2 ? WIFI_PS_MAX_MODEM : WIFI_PS_MIN_MODEM));
}

// ---- 反向吞吐测试：设备监听，对端连进来猛灌 ----
// 为什么反过来：Windows 防火墙默认挡跨主机入站，加规则要管理员权限；
// 出站不受限。接收路径（lwIP 窗口/pbuf/recv 循环）两个方向完全一样，
// 所以"谁主动连"不影响要测的东西。产品里设备永不监听，这里只是厂商调试。
static void accept_task(void *arg) {
    const int port = (int)(intptr_t)arg;
    const int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (fd < 0) {
        terminal_link_send_src(LINK_SRC_USB,
                               "{\"event\":\"tcp_accept\",\"ok\":false,\"err\":\"socket\"}\n");
        vTaskDelete(NULL);
        return;
    }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((uint16_t)port);
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) != 0 || listen(fd, 1) != 0) {
        close(fd);
        terminal_link_send_src(LINK_SRC_USB,
                               "{\"event\":\"tcp_accept\",\"ok\":false,\"err\":\"bind\"}\n");
        vTaskDelete(NULL);
        return;
    }
    // 监听 30 s 等对端来连；连上后最多收 20 s。
    struct timeval tv = {.tv_sec = 30, .tv_usec = 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    const int conn = accept(fd, NULL, NULL);
    close(fd);
    if (conn < 0) {
        terminal_link_send_src(LINK_SRC_USB,
                               "{\"event\":\"tcp_accept\",\"ok\":false,\"err\":\"accept_timeout\"}\n");
        vTaskDelete(NULL);
        return;
    }
    int rcvbuf = 16384;
    setsockopt(conn, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof rcvbuf);
    tv.tv_sec = 2;
    setsockopt(conn, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    static uint8_t buf[4096];
    uint32_t total = 0;
    int calls = 0;
    const int64_t t0 = esp_timer_get_time();
    for (;;) {
        const int n = recv(conn, buf, sizeof buf, 0);
        calls++;
        if (n > 0) {
            total += (uint32_t)n;
            if ((esp_timer_get_time() - t0) / 1000 > 20000) break;
        } else if (n == 0) {
            break;
        } else if (errno != EAGAIN && errno != EWOULDBLOCK) {
            break;
        } else {
            break;  // 2 s 没数据就当结束
        }
    }
    const int elapsed = (int)((esp_timer_get_time() - t0) / 1000);
    close(conn);
    const uint32_t kbps_x10 = elapsed ? (uint32_t)((uint64_t)total * 10 / (uint64_t)elapsed) : 0;
    ESP_LOGI(TAG, "tcp_accept: %u B / %d ms = %u.%u KB/s，%d 次 recv", (unsigned)total, elapsed,
             (unsigned)(kbps_x10 / 10), (unsigned)(kbps_x10 % 10), calls);
    char ev[288];
    snprintf(ev, sizeof ev,
             "{\"event\":\"tcp_accept\",\"ok\":true,\"port\":%d,\"bytes\":%u,\"ms\":%d,"
             "\"kbps\":%u.%u,\"calls\":%d,\"heap\":%u,\"heap_min\":%u,\"heap_block\":%u}\n",
             port, (unsigned)total, elapsed, (unsigned)(kbps_x10 / 10),
             (unsigned)(kbps_x10 % 10), calls,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
    terminal_link_send_src(LINK_SRC_USB, ev);
    vTaskDelete(NULL);
}

esp_err_t terminal_net_accept_test(int port) {
    if (port <= 0 || port > 65535) return ESP_ERR_INVALID_ARG;
    return xTaskCreate(accept_task, "acc", 4096, (void *)(intptr_t)port, 4, NULL) == pdPASS
               ? ESP_OK
               : ESP_ERR_NO_MEM;
}

// ---- 路径质量测量：n 次 TCP 连接（SYN→SYN-ACK 完成即一个 RTT）----
// 连接失败也算一次样本：SYN 丢了就是最直接的丢包证据。
static void rtt_task(void *arg) {
    char *spec = (char *)arg;
    char host[48] = {0};
    int port = 0, n = 6;
    if (sscanf(spec, "%47s %d %d", host, &port, &n) != 3) {
        free(spec);
        terminal_link_send_src(LINK_SRC_USB,
                               "{\"event\":\"rtt\",\"ok\":false,\"err\":\"bad_args\"}\n");
        vTaskDelete(NULL);
        return;
    }
    free(spec);
    if (n > 20) n = 20;
    struct addrinfo hints = {0}, *res = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    char portstr[8];
    snprintf(portstr, sizeof portstr, "%d", port);
    if (getaddrinfo(host, portstr, &hints, &res) != 0 || !res) {
        terminal_link_send_src(LINK_SRC_USB, "{\"event\":\"rtt\",\"ok\":false,\"err\":\"resolve\"}\n");
        vTaskDelete(NULL);
        return;
    }
    int ok = 0, fail = 0, min_ms = 100000, max_ms = 0, sum_ms = 0;
    for (int i = 0; i < n; i++) {
        const int fd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
        if (fd < 0) {
            fail++;
            continue;
        }
        // 连接超时要短于默认值：这里量的是"能不能在一个 RTT 内握上手"。
        struct timeval tv = {.tv_sec = 3, .tv_usec = 0};
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
        const int64_t t0 = esp_timer_get_time();
        const int rc = connect(fd, res->ai_addr, res->ai_addrlen);
        const int ms = (int)((esp_timer_get_time() - t0) / 1000);
        close(fd);
        if (rc == 0) {
            ok++;
            sum_ms += ms;
            if (ms < min_ms) min_ms = ms;
            if (ms > max_ms) max_ms = ms;
        } else {
            fail++;
        }
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    freeaddrinfo(res);
    const int avg = ok ? sum_ms / ok : 0;
    ESP_LOGI(TAG, "rtt: %s:%d ok=%d fail=%d min=%d avg=%d max=%d ms", host, port, ok, fail,
             ok ? min_ms : 0, avg, max_ms);
    char ev[256];
    snprintf(ev, sizeof ev,
             "{\"event\":\"rtt\",\"ok\":true,\"host\":\"%s\",\"port\":%d,\"tries\":%d,"
             "\"success\":%d,\"fail\":%d,\"min_ms\":%d,\"avg_ms\":%d,\"max_ms\":%d}\n",
             host, port, n, ok, fail, ok ? min_ms : 0, avg, max_ms);
    terminal_link_send_src(LINK_SRC_USB, ev);
    vTaskDelete(NULL);
}

esp_err_t terminal_net_rtt_test(const char *host, int port, int tries) {
    char *spec = malloc(64);
    if (!spec) return ESP_ERR_NO_MEM;
    snprintf(spec, 64, "%s %d %d", host, port, tries);
    if (xTaskCreate(rtt_task, "rtt", 4096, spec, 4, NULL) != pdPASS) {
        free(spec);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
