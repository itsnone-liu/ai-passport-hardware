// main/terminal_capture.c — 采集任务：pre-roll、实时送帧、轮次收尾。
#include "terminal_capture.h"

#include "bsp_audio.h"
#include "terminal_link.h"
#include "terminal_pav1.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <stdio.h>
#include <string.h>

static const char *TAG = "cap";

#define SAMPLE_HZ 16000
#define FRAME_BYTES 640              // 20 ms @ 16 kHz mono s16（§4 定案，不协商）
#define PREROLL_FRAMES 15            // 300 ms
#define MAX_TURN_MS 30000            // 与服务端 30 s 缓冲上限对齐
#define DROP_LIMIT 25                // 连续丢 25 帧（500 ms）判 overflow
#define ARM_TIMEOUT_MS 1500          // 按键后等 capture.start 的时限

static uint8_t s_pre[PREROLL_FRAMES][FRAME_BYTES];
static int s_pre_count;
static int s_pre_head;

static volatile bool s_armed;        // 正在填 pre-roll（按键后）
static volatile bool s_active;       // 正在为某一轮送实时帧
static volatile bool s_req_start;
static volatile bool s_req_stop;
static volatile uint32_t s_turn;
static volatile uint32_t s_req_turn;
static volatile uint32_t s_stop_turn;

static TaskHandle_t s_task;
static int64_t s_turn_start_us;
static int64_t s_arm_start_us;
static uint32_t s_sent_frames;
static uint32_t s_dropped;
static uint32_t s_overflows;
static uint32_t s_turns;
static uint32_t s_read_fail;
static char s_state[16] = "idle";

static void set_state(const char *st) { snprintf(s_state, sizeof s_state, "%s", st); }

static void emit_capture_started(uint32_t turn_id, int pre_roll_ms) {
    char ev[128];
    snprintf(ev, sizeof ev,
             "{\"event\":\"capture.started\",\"turn_id\":%u,\"pre_roll_ms\":%d}\n",
             (unsigned)turn_id, pre_roll_ms);
    terminal_link_send_src(LINK_SRC_TCP, ev);
}

static void emit_capture_ended(uint32_t turn_id, const char *reason) {
    char ev[192];
    snprintf(ev, sizeof ev,
             "{\"event\":\"capture.ended\",\"turn_id\":%u,\"reason\":\"%s\",\"frames\":%u,"
             "\"dropped\":%u}\n",
             (unsigned)turn_id, reason, (unsigned)s_sent_frames, (unsigned)s_dropped);
    terminal_link_send_src(LINK_SRC_TCP, ev);
    ESP_LOGI(TAG, "轮次 %u 结束：%s，%u 帧，丢 %u", (unsigned)turn_id, reason,
             (unsigned)s_sent_frames, (unsigned)s_dropped);
}

static void emit_error(const char *code, uint32_t turn_id) {
    char ev[128];
    snprintf(ev, sizeof ev, "{\"event\":\"error\",\"turn_id\":%u,\"code\":\"%s\"}\n",
             (unsigned)turn_id, code);
    terminal_link_send_src(LINK_SRC_TCP, ev);
}

// pre-roll 里最旧的先发：按 head 反推起点，顺序不发反（发反了 ASR 听到的是倒放）。
static void flush_preroll(void) {
    const int n = s_pre_count;
    const int first = (s_pre_head - s_pre_count + PREROLL_FRAMES) % PREROLL_FRAMES;
    for (int i = 0; i < n; i++) {
        const int idx = (first + i) % PREROLL_FRAMES;
        if (terminal_pav1_send_pcm(s_turn, s_pre[idx], FRAME_BYTES) > 0) s_sent_frames++;
        else s_dropped++;
    }
    s_pre_count = 0;
    s_pre_head = 0;
}

static void begin_turn(void) {
    s_turn = s_req_turn;
    s_req_start = false;
    const int pre_ms = s_pre_count * 20;
    s_active = true;
    s_armed = false;
    s_turn_start_us = esp_timer_get_time();
    s_sent_frames = 0;
    s_dropped = 0;
    s_turns++;
    set_state("listening");
    flush_preroll();
    emit_capture_started(s_turn, pre_ms);
    ESP_LOGI(TAG, "轮次 %u 开始（pre-roll %d ms）", (unsigned)s_turn, pre_ms);
}

static void finish_turn(const char *reason) {
    if (!s_active) return;
    terminal_pav1_send_end(s_turn);  // END 帧必须立即发（§6.3）
    emit_capture_ended(s_turn, reason);
    s_active = false;
    s_armed = false;
    s_req_stop = false;
    set_state("processing");
}

static void capture_task(void *arg) {
    (void)arg;
    static uint8_t frame[FRAME_BYTES];
    bool format_ok = false;

    for (;;) {
        if (!s_armed && !s_active && !s_req_start) {
            xTaskNotifyWait(0, 0xffffffff, NULL, portMAX_DELAY);
            continue;
        }
        if (!format_ok) {
            format_ok = (bsp_audio_set_format(SAMPLE_HZ, 16, 1) == ESP_OK);
            if (!format_ok) {
                ESP_LOGW(TAG, "codec 格式设置失败，稍后重试");
                vTaskDelay(pdMS_TO_TICKS(500));
                continue;
            }
        }

        // 直接来的 capture.start（没有按键预热）也要能起：pre-roll 为空而已，
        // 但要如实告诉服务端实际补了多少毫秒，不让它以为上来了 300 ms。
        if (s_req_start && !s_active) {
            if (!s_armed) s_armed = true;
            begin_turn();
        }
        if (!s_armed && !s_active) continue;

        if (bsp_audio_read(frame, FRAME_BYTES) != ESP_OK) {
            s_read_fail++;
            continue;
        }

        if (s_active) {
            if (s_req_stop) {
                finish_turn("released");
                continue;
            }
            if ((esp_timer_get_time() - s_turn_start_us) / 1000 > MAX_TURN_MS) {
                finish_turn("max_duration");
                continue;
            }
            if (terminal_pav1_send_pcm(s_turn, frame, FRAME_BYTES) > 0) {
                s_sent_frames++;
                s_dropped = 0;
            } else {
                // 发不出去就是发不出去：计数，连续 500 ms 才判 overflow 并收轮，
                // 偶发一帧丢弃不值得打断整轮对话。
                s_dropped++;
                if (s_dropped >= DROP_LIMIT) {
                    s_overflows++;
                    emit_error("audio_overflow", s_turn);
                    finish_turn("error");
                }
            }
            continue;
        }

        // 还没开始送：填 pre-roll 环形缓冲（覆盖最旧的）
        memcpy(s_pre[s_pre_head], frame, FRAME_BYTES);
        s_pre_head = (s_pre_head + 1) % PREROLL_FRAMES;
        if (s_pre_count < PREROLL_FRAMES) s_pre_count++;
        if ((esp_timer_get_time() - s_arm_start_us) / 1000 > ARM_TIMEOUT_MS) {
            // 按键起了个头但服务端没在 1.5 s 内给 capture.start：放弃本轮。
            ESP_LOGW(TAG, "按键后 %d ms 未收到 capture.start，放弃", ARM_TIMEOUT_MS);
            emit_error("cap_start_timeout", 0);
            s_armed = false;
            s_pre_count = 0;
            s_pre_head = 0;
            set_state("idle");
        }
    }
}

void terminal_capture_init(void) {
    if (!s_task) xTaskCreate(capture_task, "cap", 4096, NULL, 6, &s_task);
}

esp_err_t terminal_capture_start(uint32_t turn_id) {
    if (s_active || s_req_start) return ESP_ERR_INVALID_STATE;  // busy（§6.5）
    // 音频通道正在拨号/重连：这是可重试的瞬态，报 busy 而不是 not_supported，
    // 否则服务端会以为设备不具备上行能力。
    if (!terminal_pav1_ready()) {
        return terminal_pav1_starting() ? ESP_ERR_INVALID_STATE : ESP_ERR_NOT_SUPPORTED;
    }
    s_req_turn = turn_id;
    s_req_start = true;
    set_state("listening");
    if (s_task) xTaskNotifyGive(s_task);
    ESP_LOGI(TAG, "capture.start turn=%u", (unsigned)turn_id);
    return ESP_OK;
}

esp_err_t terminal_capture_stop(uint32_t turn_id) {
    if (!s_active) return ESP_ERR_INVALID_STATE;
    if (turn_id != s_turn) return ESP_ERR_INVALID_ARG;  // turn_mismatch：不是当前轮
    s_stop_turn = turn_id;
    s_req_stop = true;
    return ESP_OK;
}

void terminal_capture_arm(void) {
    if (s_active) return;
    s_armed = true;
    s_pre_count = 0;
    s_pre_head = 0;
    s_arm_start_us = esp_timer_get_time();
    set_state("listening");
    if (s_task) xTaskNotifyGive(s_task);
    ESP_LOGI(TAG, "按键起采，等 capture.start");
}

void terminal_capture_get_audio(char *buf, size_t n) {
    if (!buf || n == 0) return;
    const char *st = s_active ? "capturing" : (s_armed ? "armed" : s_state);
    snprintf(buf, n,
             "\"state\":\"%s\",\"turn_id\":%u,\"ring_ms\":0,\"underruns\":0,"
             "\"overflows\":%u,\"frames\":%u,\"dropped\":%u,\"read_fail\":%u,\"turns\":%u",
             st, (unsigned)(s_active ? s_turn : 0), (unsigned)s_overflows,
             (unsigned)s_sent_frames, (unsigned)s_dropped, (unsigned)s_read_fail,
             (unsigned)s_turns);
}

void terminal_capture_describe(char *buf, size_t n) {
    if (!buf || n == 0) return;
    snprintf(buf, n, "\"cap_state\":\"%s\",\"cap_turn\":%u,\"cap_frames\":%u",
             s_active ? "capturing" : (s_armed ? "armed" : s_state),
             (unsigned)(s_active ? s_turn : 0), (unsigned)s_sent_frames);
}