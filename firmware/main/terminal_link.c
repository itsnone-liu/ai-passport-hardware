// main/terminal_link.c — 命令分发与响应广播，与具体传输无关。
#include "terminal_link.h"

#include "bsp_audio.h"
#include "bsp_battery.h"
#include "bsp_display.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "terminal_net.h"
#include "terminal_ui.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

static const char *TAG = "link";

#define LINK_MAX_SINKS 2
#define LINE_MAX 512

static link_sink_t s_sinks[LINK_MAX_SINKS];
static int s_sink_count;
static int64_t s_boot_us;

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

void terminal_link_init(void) { s_boot_us = esp_timer_get_time(); }

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

static void send_fmt(const char *fmt, ...) {
    char buf[LINE_MAX];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    terminal_link_send(buf);
}

static void reply_status(int id) {
    const int soc = bsp_battery_soc();
    const int mv = bsp_battery_mv();
    const int64_t up = (esp_timer_get_time() - s_boot_us) / 1000000;
    send_fmt("{\"id\":%d,\"ok\":true,\"cmd\":\"status\",\"battery\":%d,\"mv\":%d,"
             "\"volume\":%d,\"brightness\":%d,\"uptime\":%lld}\n",
             id, soc, mv, terminal_ui_volume(), terminal_ui_brightness(), (long long)up);
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

static void apply_text(const char *text) {
    if (bsp_lvgl_lock(200)) {
        terminal_ui_set_message(text);
        bsp_lvgl_unlock();
    }
}

static int int_field(const cJSON *root, const char *name, int dflt) {
    const cJSON *it = cJSON_GetObjectItemCaseSensitive(root, name);
    return cJSON_IsNumber(it) ? it->valueint : dflt;
}

void terminal_link_dispatch(char *line) {
    if (!line || !*line) return;

    // 只处理以 '{' 开头的行。接到的可能不是我们自己人：路由器/HTTP 网关/任何误连上来
    // 的东西都会往 socket 里写非 JSON 字节，若逐行回 "bad json" 就会变成回复风暴，
    // 把链路彻底淹掉。协议一致规定：不像 JSON 的行直接丢掉。
    while (*line == ' ' || *line == '\t') line++;
    if (*line != '{') return;

    cJSON *root = cJSON_Parse(line);
    if (!root) {
        // 以 '{' 开头却解析失败，多半是真正想调命令却拼错了，此时报错才有助于调试。
        terminal_link_send("{\"ok\":false,\"err\":\"bad json\"}\n");
        return;
    }

    const int id = int_field(root, "id", 0);
    const cJSON *cmd = cJSON_GetObjectItemCaseSensitive(root, "cmd");
    if (!cJSON_IsString(cmd)) {
        send_fmt("{\"id\":%d,\"ok\":false,\"err\":\"no cmd\"}\n", id);
        cJSON_Delete(root);
        return;
    }

    const char *c = cmd->valuestring;
    if (strcmp(c, "get_status") == 0) {
        reply_status(id);
    } else if (strcmp(c, "show_text") == 0) {
        const cJSON *t = cJSON_GetObjectItemCaseSensitive(root, "text");
        apply_text(cJSON_IsString(t) ? t->valuestring : "");
        send_fmt("{\"id\":%d,\"ok\":true,\"cmd\":\"show_text\"}\n", id);
    } else if (strcmp(c, "set_volume") == 0) {
        apply_volume(int_field(root, "value", terminal_ui_volume()));
        send_fmt("{\"id\":%d,\"ok\":true,\"cmd\":\"set_volume\",\"volume\":%d}\n", id,
                 terminal_ui_volume());
    } else if (strcmp(c, "set_brightness") == 0) {
        apply_brightness(int_field(root, "value", terminal_ui_brightness()));
        send_fmt("{\"id\":%d,\"ok\":true,\"cmd\":\"set_brightness\",\"brightness\":%d}\n", id,
                 terminal_ui_brightness());
    } else if (strcmp(c, "ping") == 0) {
        send_fmt("{\"id\":%d,\"ok\":true,\"cmd\":\"pong\"}\n", id);
    } else if (strcmp(c, "set_net") == 0) {
        // 现场改网络参数：换 Wi-Fi 或换服务器地址不必重新刷固件。
        const cJSON *ssid = cJSON_GetObjectItemCaseSensitive(root, "ssid");
        const cJSON *pw = cJSON_GetObjectItemCaseSensitive(root, "password");
        const cJSON *host = cJSON_GetObjectItemCaseSensitive(root, "host");
        const cJSON *port = cJSON_GetObjectItemCaseSensitive(root, "port");
        const esp_err_t err = terminal_net_apply(
            cJSON_IsString(ssid) ? ssid->valuestring : NULL,
            cJSON_IsString(pw) ? pw->valuestring : NULL,
            cJSON_IsString(host) ? host->valuestring : NULL,
            cJSON_IsNumber(port) ? port->valueint : 0);
        if (err == ESP_OK) {
            send_fmt("{\"id\":%d,\"ok\":true,\"cmd\":\"set_net\"}\n", id);
        } else {
            send_fmt("{\"id\":%d,\"ok\":false,\"cmd\":\"set_net\",\"err\":\"apply failed\"}\n", id);
        }
    } else if (strcmp(c, "get_net") == 0) {
        char body[256];
        terminal_net_describe(body, sizeof body);
        send_fmt("{\"id\":%d,\"ok\":true,\"cmd\":\"net\",%s}\n", id, body);
    } else if (strcmp(c, "scan") == 0) {
        // 现场诊断用：列出芯片实际看得见的 2.4 GHz 热点。
        static char s_scan[1024];
        const esp_err_t err = terminal_net_scan(s_scan, sizeof s_scan);
        if (err == ESP_OK) {
            send_fmt("{\"id\":%d,\"ok\":true,\"cmd\":\"scan\",%s}\n", id, s_scan);
        } else {
            send_fmt("{\"id\":%d,\"ok\":false,\"cmd\":\"scan\",\"err\":\"%s\"}\n", id,
                     esp_err_to_name(err));
        }
    } else {
        send_fmt("{\"id\":%d,\"ok\":false,\"err\":\"unknown cmd\"}\n", id);
    }

    cJSON_Delete(root);
}

void terminal_link_emit_key(int btn_index, int ev_index) {
    if (s_sink_count == 0) return;
    send_fmt("{\"event\":\"key\",\"btn\":\"%s\",\"ev\":\"%s\"}\n", btn_name(btn_index),
             ev_name(ev_index));
}