// main/terminal_proto.c — USB-Serial/JTAG 上的载体实现（只是字节搬运）。
//
// 解析与分发全部在 terminal_link 里；本文件只负责：装驱动、读字节、按行切分、
// 把要发的行写回 USB。串口上还混着 ESP-IDF 日志，主机侧忽略不以 '{' 开头的行即可。
#include "terminal_proto.h"

#include "driver/usb_serial_jtag.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "terminal_link.h"

#include <stddef.h>
#include <string.h>

static const char *TAG = "usb";

#define LINE_MAX 512
#define CHUNK_MAX 160

static volatile bool s_ready;

static void usb_sink(const char *line, size_t len) {
    if (!s_ready) return;
    size_t off = 0;
    while (off < len) {
        const int w = usb_serial_jtag_write_bytes(line + off, len - off, pdMS_TO_TICKS(50));
        if (w <= 0) break;
        off += (size_t)w;
    }
}

static void proto_task(void *arg) {
    (void)arg;
    char line[LINE_MAX];
    size_t len = 0;
    uint8_t chunk[CHUNK_MAX];

    for (;;) {
        const int n = usb_serial_jtag_read_bytes(chunk, sizeof chunk, pdMS_TO_TICKS(100));
        for (int i = 0; i < n; i++) {
            const char ch = (char)chunk[i];
            if (ch == '\r') continue;
            if (ch == '\n') {
                line[len] = '\0';
                if (len > 0) terminal_link_dispatch(line);
                len = 0;
            } else if (len < sizeof(line) - 1) {
                line[len++] = ch;
            } else {
                len = 0;  // 超长行丢弃，避免粘包后误解析
            }
        }
    }
}

esp_err_t terminal_proto_start(void) {
    usb_serial_jtag_driver_config_t cfg = {
        .tx_buffer_size = 2048,
        .rx_buffer_size = 2048,
    };
    const esp_err_t err = usb_serial_jtag_driver_install(&cfg);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "USB-Serial/JTAG 驱动安装失败(%s)，USB 载体不可用", esp_err_to_name(err));
        return err;
    }

    s_ready = true;
    terminal_link_register(usb_sink);

    if (xTaskCreate(proto_task, "dsh_usb", 4096, NULL, 6, NULL) != pdPASS) {
        s_ready = false;
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "USB 载体就绪");
    terminal_link_send("{\"event\":\"ready\",\"link\":\"usb\"}\n");
    return ESP_OK;
}

bool terminal_proto_ready(void) { return s_ready; }