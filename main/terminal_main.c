// main/terminal_main.c — dsh 瘦终端的应用入口。
//
// 职责边界（对齐 AGENTS.md）：本文件只做页面/状态机/任务编排；所有板级能力都来自
// components/bsp。UI 由 terminal_ui 自绘，不使用 demo 菜单外壳。
#include "bsp_audio.h"
#include "bsp_battery.h"
#include "bsp_button.h"
#include "bsp_display.h"
#include "bsp_i2c.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "terminal_link.h"
#include "terminal_boot.h"
#include "terminal_capture.h"
#include "terminal_playback.h"
#include "terminal_auth.h"
#include "terminal_pav1.h"
#include "terminal_net.h"
#include "terminal_proto.h"
#include "terminal_ui.h"

static const char *TAG = "dsh";

#define KEY_QUEUE_DEPTH 8
#define VOLUME_STEP 10
#define DEFAULT_VOLUME 60
#define DEFAULT_BRIGHTNESS 80

typedef struct {
    int btn;
    int ev;
} key_ev_t;

static QueueHandle_t s_key_q;
static esp_timer_handle_t s_tick;
static bool s_batt_ok;
static bool s_audio_ok;

// 按键回调跑在共享 esp_timer 任务上，只入队，不做任何其他事情。
static void on_key(bsp_btn_t btn, bsp_btn_ev_t ev, void *user) {
    (void)user;
    if (!s_key_q) return;
    const key_ev_t e = { .btn = (int)btn, .ev = (int)ev };
    (void)xQueueSend(s_key_q, &e, 0);
}

// 厂商扩展入口：把一次按键直接塞进按键队列，与 BSP 回调走同一条路径。
// 用途：没有人在设备旁边时验证 press/release 语义与 pre-roll 记账。
// 不绕开 key_task、不直接调 capture，所以它验的是真实分发路径——
// 唯一没被覆盖的只有按键硬件本身（那一环必须手指来验）。
void terminal_app_key_inject(int btn, int ev) {
    if (!s_key_q) return;
    const key_ev_t e = { .btn = btn, .ev = ev };
    (void)xQueueSend(s_key_q, &e, 0);
}

static void key_task(void *arg) {
    (void)arg;
    key_ev_t e;
    for (;;) {
        if (xQueueReceive(s_key_q, &e, portMAX_DELAY) != pdTRUE) continue;

        // 上报给服务器（非阻塞，USB 与网络两个载体都会收到）
        terminal_link_emit_key(e.btn, e.ev);

        // 说话键按"按住说、松手停"语义处理。
        // 旧写法是 OK 的 CLICK 才起采、LONG 去调亮度：人手按住说话必然超过 500 ms，
        // 于是永远走 LONG 分支，只把屏幕调暗，采集一次都没启动过——真人测试失败的真因。
        // PRESS 起采让 pre-roll 从开口那一刻就填起来；RELEASE 当场收轮，
        // 不等服务端往返 capture.stop，否则这句话的尾巴会被 300 ms 的 RTT 切掉。
        if (e.btn == BSP_BTN_OK && e.ev == BSP_BTN_PRESS) {
            terminal_capture_arm();
        } else if (e.btn == BSP_BTN_OK && e.ev == BSP_BTN_RELEASE) {
            terminal_capture_request_end();
        } else if (e.ev == BSP_BTN_CLICK || e.ev == BSP_BTN_DOUBLE) {
            int vol = terminal_ui_volume();
            int bright = terminal_ui_brightness();
            bool changed = false;

            if (e.btn == BSP_BTN_UP) {
                if (bsp_lvgl_lock(200)) {
                    vol = terminal_ui_step_volume(+VOLUME_STEP);
                    bsp_lvgl_unlock();
                }
                changed = true;
            } else if (e.btn == BSP_BTN_DOWN) {
                if (bsp_lvgl_lock(200)) {
                    vol = terminal_ui_step_volume(-VOLUME_STEP);
                    bsp_lvgl_unlock();
                }
                changed = true;
            } else if (e.btn == BSP_BTN_OK && e.ev == BSP_BTN_DOUBLE) {
                // 亮度挪到双击：长按已经是"说话"了，不能再兼职调亮度。
                if (bsp_lvgl_lock(200)) {
                    bright = terminal_ui_step_brightness(-20);
                    bsp_lvgl_unlock();
                }
                bsp_display_backlight((uint8_t)bright);
            }

            if (changed) {
                bsp_audio_set_volume((uint8_t)vol);
                ESP_LOGI(TAG, "音量 -> %d", vol);
            }
        }
    }
}

static void tick_cb(void *arg) {
    (void)arg;
    terminal_status_t st = {
        .battery_pct = s_batt_ok ? bsp_battery_soc() : -1,
        .battery_mv = s_batt_ok ? bsp_battery_mv() : -1,
        .volume = terminal_ui_volume(),
        .brightness = terminal_ui_brightness(),
        .uptime_s = esp_timer_get_time() / 1000000,
        .link_mode = terminal_net_ready()    ? UI_LINK_NET
                     : terminal_net_ip_ready() ? UI_LINK_WIFI
                     : terminal_proto_ready()  ? UI_LINK_USB
                                               : UI_LINK_NONE,
    };
    if (bsp_lvgl_lock(100)) {
        terminal_ui_refresh(&st);
        bsp_lvgl_unlock();
    }
}

void app_main(void) {
    ESP_LOGI(TAG, "dsh terminal 启动");
    terminal_boot_record();  // 最先做：把本次复位原因与上次运行的堆统计钉下来
    terminal_link_init();
    terminal_net_preload();  // 配置先于 USB 通道就绪，避免开机瞬间 get_net 答空值
    terminal_auth_init();    // 设备 id 与密钥：必须在第一次握手之前就绪
    terminal_pav1_init();    // 音频通道任务（等 auth 通过后才会真连）
    terminal_capture_init(); // 采集任务（按键起采 / capture.start）
    terminal_playback_init(); // 播放任务（playback.start 后按 I2S 时钟出声）

    bsp_i2c_init();
    bsp_i2c_scan();

    if (bsp_display_init() != ESP_OK || !bsp_lvgl_init()) {
        ESP_LOGE(TAG, "显示/LVGL 初始化失败，终端无法运行");
        return;
    }
    bsp_display_backlight(DEFAULT_BRIGHTNESS);

    s_batt_ok = (bsp_battery_init() == ESP_OK);
    if (!s_batt_ok) ESP_LOGW(TAG, "电量计不可用，电量为 --");

    s_audio_ok = (bsp_audio_init() == ESP_OK);
    if (s_audio_ok) {
        bsp_audio_set_volume(DEFAULT_VOLUME);
    } else {
        ESP_LOGW(TAG, "音频 codec 不可用，音量控制降级为仅显示");
    }

    s_key_q = xQueueCreate(KEY_QUEUE_DEPTH, sizeof(key_ev_t));
    if (s_key_q) {
        if (xTaskCreate(key_task, "dsh_key", 3072, NULL, 4, NULL) == pdPASS) {
            if (bsp_button_init(on_key, NULL) != ESP_OK) {
                ESP_LOGW(TAG, "按键初始化失败，仅保留服务器控制");
            }
        }
    }

    if (bsp_lvgl_lock(1000)) {
        terminal_ui_create();
        terminal_ui_set_brightness(DEFAULT_BRIGHTNESS);
        bsp_lvgl_unlock();
    }

    terminal_proto_start();
    terminal_net_start();

    const esp_timer_create_args_t tick_args = { .callback = tick_cb, .name = "dsh_tick" };
    if (esp_timer_create(&tick_args, &s_tick) == ESP_OK) {
        esp_timer_start_periodic(s_tick, 1000000);
    }

    ESP_LOGI(TAG, "就绪: battery=%d audio=%d usb=%d", (int)s_batt_ok, (int)s_audio_ok,
             (int)terminal_proto_ready());
}