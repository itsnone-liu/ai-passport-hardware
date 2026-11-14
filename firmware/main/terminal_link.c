// main/terminal_link.c — 命令分发与响应广播，与具体传输无关。
#include "terminal_link.h"

#include "bsp_audio.h"
#include "bsp_battery.h"
#include "bsp_display.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "terminal_net.h"
#include "terminal_audio.h"
#include "terminal_ui.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

static const char *TAG = "link";

#define LINK_MAX_SINKS 2
#define LINE_MAX 512          // 单条入站命令的上限
#define TX_MAX 2048           // 出站单条消息上限：scan/get_net 的 JSON 比 512 长得多，
                              // 用 512 的缓冲会 vsnprintf 截断成非法 JSON

static link_sink_t s_sinks[LINK_MAX_SINKS];
static int s_sink_count;
static int64_t s_boot_us;
static char s_tx[TX_MAX];              // 出站组包缓冲（静态，见 terminal_link_init）
static SemaphoreHandle_t s_tx_mu;

static const char *btn_name(int i) {
    switch (i) {
    case 0: return "up";
    case 1: return "down";
    case 2: return "ok";
    default: return "?";
    }
}

static const char *ev_name(int i) {
    switch (i) {
    case 0: return "press";
    case 1: return "click";
    case 2: return "double";
    case 3: return "long";
    case 4: return "release";
    default: return "?";
    }
}

void terminal_link_init(void) {
    s_boot_us = esp_timer_get_time();
    // 发送缓冲放在静态区：2048 字节的栈帧会把 dsh_usb 任务的 4 KB 栈直接打爆，
    // 现场表现是"一发命令设备就重启"（Stack protection fault）。多任务共用同一个
    // 缓冲，所以配一把锁。
    if (!s_tx_mu) s_tx_mu = xSemaphoreCreateMutex();
}

void terminal_link_register(link_sink_t sink) {
    if (!sink) return;
    if (s_sink_count >= LINK_MAX_SINKS) {
        ESP_LOGW(TAG, "载体已满，忽略多余的 sink");
        return;
    }
    s_sinks[s_sink_count++] = sink;
}

void terminal_link_send(const char *line) {
    if (!line) return;
    const size_t n = strlen(line);
    for (int i = 0; i < s_sink_count; i++) s_sinks[i](line, n);
}

// 应答组包：只发回请求来源。与 send_fmt 分开是因为广播和定向的语义不同，
// 混用会让"谁的应答"变成靠运气。
static void reply_fmt(const link_reply_t *to, const char *fmt, ...) {
    if (s_tx_mu) xSemaphoreTake(s_tx_mu, portMAX_DELAY);
    va_list ap;
    va_start(ap, fmt);
    const int n = vsnprintf(s_tx, sizeof s_tx, fmt, ap);
    va_end(ap);
    if (to && to->sink) {
        to->sink(s_tx, (n > 0 && (size_t)n < sizeof s_tx) ? (size_t)n : strlen(s_tx));
    } else {
        terminal_link_send(s_tx);  // 没有来源信息时才退化成广播
    }
    if (s_tx_mu) xSemaphoreGive(s_tx_mu);
}

static void reply_status(const link_reply_t *to, int id) {
    const int soc = bsp_battery_soc();
    const int mv = bsp_battery_mv();
    const int64_t up = (esp_timer_get_time() - s_boot_us) / 1000000;
    // 堆的三个数决定了"一条话最多能缓冲多久"，也是判断'是不是内存不够'的唯一现场依据，
    // 所以放进 status 而不是另开命令。
    const unsigned heap_now = (unsigned)esp_get_free_heap_size();
    const unsigned heap_min = (unsigned)esp_get_minimum_free_heap_size();
    const unsigned heap_blk =
        (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT | MALLOC_CAP_DEFAULT);
    reply_fmt(to, "{\"id\":%d,\"ok\":true,\"cmd\":\"status\",\"battery\":%d,\"mv\":%d,"
                  "\"volume\":%d,\"brightness\":%d,\"uptime\":%lld,"
                  "\"heap\":%u,\"heap_min\":%u,\"heap_block\":%u}\n",
              id, soc, mv, terminal_ui_volume(), terminal_ui_brightness(), (long long)up, heap_now,
              heap_min, heap_blk);
}

static void apply_volume(int value) {
    if (value < 0) value = 0;
    if (value > 100) value = 100;
    bsp_audio_set_volume((uint8_t)value);
    if (bsp_lvgl_lock(200)) {
        terminal_ui_set_volume(value);
        bsp_lvgl_unlock();
    }
}

static void apply_brightness(int value) {
    if (value < 0) value = 0;
    if (value > 100) value = 100;
    bsp_display_backlight((uint8_t)value);
    if (bsp_lvgl_lock(200)) {
        terminal_ui_set_brightness(value);
        bsp_lvgl_unlock();
    }
}

// 返回 false 表示没写进屏幕（锁超时）。调用方必须据此回 error，而不是回 ok——
// "显示成功"和"根本没显示"在主机侧无法区分，后续自动化会照着假象继续跑。
static bool apply_text(const char *text) {
    if (!bsp_lvgl_lock(200)) return false;
    terminal_ui_set_message(text);
    bsp_lvgl_unlock();
    return true;
}

static int int_field(const cJSON *root, const char *name, int dflt) {
    const cJSON *it = cJSON_GetObjectItemCaseSensitive(root, name);
    return cJSON_IsNumber(it) ? it->valueint : dflt;
}

void terminal_link_dispatch(char *line, const link_reply_t *reply) {
    if (!line || !*line) return;

    // 只处理以 '{' 开头的行。接到的可能不是我们自己人：路由器/HTTP 网关/任何误连上来
    // 的东西都会往 socket 里写非 JSON 字节，若逐行回 "bad_json" 就会变成回复风暴，
    // 把链路彻底淹掉。协议一致规定：不像 JSON 的行直接丢掉。
    while (*line == ' ' || *line == '\t') line++;
    if (*line != '{') return;

    cJSON *root = cJSON_Parse(line);
    if (!root) {
        // 以 '{' 开头却解析失败，多半是真正想调命令却拼错了，此时报错才有助于调试。
        reply_fmt(reply, "{\"ok\":false,\"err\":\"bad_json\"}\n");
        return;
    }

    const int id = int_field(root, "id", 0);
    const cJSON *cmd = cJSON_GetObjectItemCaseSensitive(root, "cmd");
    if (!cJSON_IsString(cmd)) {
        reply_fmt(reply, "{\"id\":%d,\"ok\":false,\"err\":\"no cmd\"}\n", id);
        cJSON_Delete(root);
        return;
    }

    const char *c = cmd->valuestring;
    if (strcmp(c, "get_status") == 0) {
        reply_status(reply, id);
    } else if (strcmp(c, "show_text") == 0) {
        const cJSON *t = cJSON_GetObjectItemCaseSensitive(root, "text");
        if (apply_text(cJSON_IsString(t) ? t->valuestring : "")) {
            reply_fmt(reply, "{\"id\":%d,\"ok\":true,\"cmd\":\"show_text\"}\n", id);
        } else {
            reply_fmt(reply, "{\"id\":%d,\"ok\":false,\"cmd\":\"show_text\",\"err\":\"display busy\"}\n",
                      id);
        }
    } else if (strcmp(c, "set_volume") == 0) {
        apply_volume(int_field(root, "value", terminal_ui_volume()));
        reply_fmt(reply, "{\"id\":%d,\"ok\":true,\"cmd\":\"set_volume\",\"volume\":%d}\n", id,
                  terminal_ui_volume());
    } else if (strcmp(c, "set_brightness") == 0) {
        apply_brightness(int_field(root, "value", terminal_ui_brightness()));
        reply_fmt(reply, "{\"id\":%d,\"ok\":true,\"cmd\":\"set_brightness\",\"brightness\":%d}\n",
                  id, terminal_ui_brightness());
    } else if (strcmp(c, "ping") == 0) {
        reply_fmt(reply, "{\"id\":%d,\"ok\":true,\"cmd\":\"pong\"}\n", id);
    } else if (strcmp(c, "set_net") == 0) {
        // 现场改网络参数：换 Wi-Fi 或换服务器地址不必重新刷固件。
        const cJSON *ssid = cJSON_GetObjectItemCaseSensitive(root, "ssid");
        const cJSON *pw = cJSON_GetObjectItemCaseSensitive(root, "password");
        const cJSON *host = cJSON_GetObjectItemCaseSensitive(root, "host");
        const cJSON *port = cJSON_GetObjectItemCaseSensitive(root, "port");
        // 先落参数、先回执，最后才断链路重连：重配网络必然掐掉当前 TCP，回执如果排在
        // 断开之后，发起方永远收不到"到底成没成"。
        const esp_err_t err = terminal_net_set(
            cJSON_IsString(ssid) ? ssid->valuestring : NULL,
            cJSON_IsString(pw) ? pw->valuestring : NULL,
            cJSON_IsString(host) ? host->valuestring : NULL,
            cJSON_IsNumber(port) ? port->valueint : 0);
        if (err == ESP_OK) {
            reply_fmt(reply, "{\"id\":%d,\"ok\":true,\"cmd\":\"set_net\"}\n", id);
            terminal_net_reconnect();  // 回执已经发出去了，这时断链才安全
        } else {
            reply_fmt(reply, "{\"id\":%d,\"ok\":false,\"cmd\":\"set_net\",\"err\":\"apply failed\"}\n",
                      id);
        }
    } else if (strcmp(c, "get_net") == 0) {
        // ssid/host 转义后最长约 6 倍，256 装不下一个含中文或引号的 SSID
        char body[768];
        terminal_net_describe(body, sizeof body);
        reply_fmt(reply, "{\"id\":%d,\"ok\":true,\"cmd\":\"net\",%s}\n", id, body);
    } else if (strcmp(c, "scan") == 0) {
        // 现场诊断用：列出芯片实际看得见的 2.4 GHz 热点。
        static char s_scan[1024];
        const esp_err_t err = terminal_net_scan(s_scan, sizeof s_scan);
        if (err == ESP_OK) {
            reply_fmt(reply, "{\"id\":%d,\"ok\":true,\"cmd\":\"scan\",%s}\n", id, s_scan);
        } else {
            reply_fmt(reply, "{\"id\":%d,\"ok\":false,\"cmd\":\"scan\",\"err\":\"%s\"}\n", id,
                      esp_err_to_name(err));
        }
    } else if (strcmp(c, "audio_selftest") == 0) {
        // 设备侧音频自测：录一段再原样回放，用来验证 codec 采播与帧时序。
        const int ms = int_field(root, "ms", 2000);
        const int frame_ms = int_field(root, "frame_ms", 20);
        const esp_err_t err = terminal_audio_selftest_start(ms, frame_ms);
        if (err == ESP_OK) {
            reply_fmt(reply, "{\"id\":%d,\"ok\":true,\"cmd\":\"audio_selftest\",\"ms\":%d}\n", id,
                      ms);
        } else {
            reply_fmt(reply, "{\"id\":%d,\"ok\":false,\"cmd\":\"audio_selftest\",\"err\":\"%s\"}\n",
                      id, esp_err_to_name(err));
        }
    } else {
        reply_fmt(reply, "{\"id\":%d,\"ok\":false,\"err\":\"unknown_cmd\"}\n", id);
    }

    cJSON_Delete(root);
}

void terminal_link_emit_key(int btn_index, int ev_index) {
    if (s_sink_count == 0) return;
    // 按键是事件，按协议 §6.2 广播给所有载体（不放 reply_fmt：它没有"请求"可回）。
    if (s_tx_mu) xSemaphoreTake(s_tx_mu, portMAX_DELAY);
    snprintf(s_tx, sizeof s_tx, "{\"event\":\"key\",\"btn\":\"%s\",\"ev\":\"%s\"}\n",
             btn_name(btn_index), ev_name(ev_index));
    terminal_link_send(s_tx);
    if (s_tx_mu) xSemaphoreGive(s_tx_mu);
}