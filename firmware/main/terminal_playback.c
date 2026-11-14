// main/terminal_playback.c — 下行播放任务与环形缓冲。
#include "terminal_playback.h"

#include "bsp_audio.h"
#include "terminal_link.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

static const char *TAG = "play";

#define SAMPLE_HZ 16000
#define FRAME_BYTES 640          // 20 ms，和上行同一口径（§4）
#define RING_FRAMES 60           // 60 × 640 = 38,400 B = 1.2 s
#define PREBUF_FRAMES 15         // 300 ms 预缓冲，够就出声（§6.3）
#define STALL_MS 2000            // playback.start 后 2 s 无帧（§6.5）
#define UNDERRUN_MS 500          // 见底后 500 ms 无新数据（§6.5）

// 定长缓冲用静态分配：定长的东西没理由去和 lwIP 抢堆碎片（§8）。
static uint8_t s_ring[RING_FRAMES][FRAME_BYTES];
static int s_head;   // 写入位置
static int s_tail;   // 读出位置
static int s_count;  // 现有帧数

static SemaphoreHandle_t s_mu;
static TaskHandle_t s_task;
static volatile bool s_req_stop;
static volatile uint32_t s_turn;
static volatile bool s_input_ended;
static volatile int64_t s_last_data_us;
static volatile int64_t s_start_us;

static enum { PB_IDLE, PB_WAIT_FIRST, PB_PLAYING } s_state;
static uint32_t s_played_frames;
static uint32_t s_overflows;
static uint32_t s_underruns;
static uint32_t s_mismatch;
static char s_state_txt[16] = "idle";

static void emit(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

static void emit(const char *fmt, ...) {
    char ev[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(ev, sizeof ev, fmt, ap);
    va_end(ap);
    terminal_link_send_src(LINK_SRC_TCP, ev);
}

static void emit_started(uint32_t turn) {
    emit("{\"event\":\"playback.started\",\"turn_id\":%u}\n", (unsigned)turn);
}

static void emit_ended(uint32_t turn, const char *reason) {
    emit("{\"event\":\"playback.ended\",\"turn_id\":%u,\"reason\":\"%s\",\"frames\":%u,"
         "\"overflows\":%u,\"underruns\":%u}\n",
         (unsigned)turn, reason, (unsigned)s_played_frames, (unsigned)s_overflows,
         (unsigned)s_underruns);
    ESP_LOGI(TAG, "轮次 %u 结束：%s，播 %u 帧，溢出 %u，欠载 %u", (unsigned)turn, reason,
             (unsigned)s_played_frames, (unsigned)s_overflows, (unsigned)s_underruns);
}

static void emit_error(const char *code, uint32_t turn) {
    emit("{\"event\":\"error\",\"turn_id\":%u,\"code\":\"%s\"}\n", (unsigned)turn, code);
}

static void reset_ring(void) {
    s_head = s_tail = s_count = 0;
}

static bool pop_frame(uint8_t *out) {
    if (s_count == 0) return false;
    memcpy(out, s_ring[s_tail], FRAME_BYTES);
    s_tail = (s_tail + 1) % RING_FRAMES;
    s_count--;
    return true;
}

static void playback_task(void *arg) {
    (void)arg;
    static uint8_t frame[FRAME_BYTES];
    bool format_ok = false;

    for (;;) {
        if (s_req_stop) {
            s_req_stop = false;
            if (s_state != PB_IDLE) {
                xSemaphoreTake(s_mu, portMAX_DELAY);
                reset_ring();
                xSemaphoreGive(s_mu);
                emit_ended(s_turn, "cancelled");
                s_state = PB_IDLE;
                snprintf(s_state_txt, sizeof s_state_txt, "idle");
            }
            continue;
        }

        if (s_state == PB_IDLE) {
            xTaskNotifyWait(0, 0xffffffff, NULL, portMAX_DELAY);
            continue;
        }

        if (s_state == PB_WAIT_FIRST) {
            if (!format_ok) {
                format_ok = (bsp_audio_set_format(SAMPLE_HZ, 16, 1) == ESP_OK);
                if (!format_ok) {
                    ESP_LOGW(TAG, "codec 格式设置失败");
                    emit_error("playback_stall", s_turn);
                    emit_ended(s_turn, "error");
                    s_state = PB_IDLE;
                    snprintf(s_state_txt, sizeof s_state_txt, "idle");
                    continue;
                }
            }
            int have;
            xSemaphoreTake(s_mu, portMAX_DELAY);
            have = s_count;
            xSemaphoreGive(s_mu);
            if (have >= PREBUF_FRAMES) {
                s_state = PB_PLAYING;
                snprintf(s_state_txt, sizeof s_state_txt, "playing");
                emit_started(s_turn);
                continue;
            }
            if ((esp_timer_get_time() - s_start_us) / 1000 > STALL_MS) {
                // 说好要播却一帧都没来：报错回 idle，不能让服务端以为在播（§6.5）。
                emit_error("playback_stall", s_turn);
                emit_ended(s_turn, "error");
                s_state = PB_IDLE;
                snprintf(s_state_txt, sizeof s_state_txt, "idle");
                continue;
            }
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        // PB_PLAYING
        xSemaphoreTake(s_mu, portMAX_DELAY);
        const bool got = pop_frame(frame);
        xSemaphoreGive(s_mu);

        if (got) {
            s_last_data_us = esp_timer_get_time();
            if (bsp_audio_write(frame, FRAME_BYTES) == ESP_OK) s_played_frames++;
            continue;
        }

        if (s_input_ended) {
            emit_ended(s_turn, "completed");
            s_state = PB_IDLE;
            snprintf(s_state_txt, sizeof s_state_txt, "idle");
            continue;
        }

        if ((esp_timer_get_time() - s_last_data_us) / 1000 > UNDERRUN_MS) {
            s_underruns++;
            emit_error("audio_underrun", s_turn);
            emit_ended(s_turn, "underrun");
            s_state = PB_IDLE;
            snprintf(s_state_txt, sizeof s_state_txt, "idle");
            continue;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

void terminal_playback_init(void) {
    if (!s_mu) s_mu = xSemaphoreCreateMutex();
    if (!s_task) xTaskCreate(playback_task, "play", 4096, NULL, 5, &s_task);
}

esp_err_t terminal_playback_start(uint32_t turn_id) {
    if (s_state != PB_IDLE) return ESP_ERR_INVALID_STATE;  // busy
    // 状态必须在返回应答之前就切好：应答一发出去，对端就会立刻推帧，
    // 若此时任务还没跑到"接帧"那一步，第一帧就被当成无主帧丢掉（实测丢过 1 帧）。
    xSemaphoreTake(s_mu, portMAX_DELAY);
    reset_ring();
    s_turn = turn_id;
    s_input_ended = false;
    s_played_frames = 0;
    s_overflows = 0;
    s_underruns = 0;
    s_start_us = esp_timer_get_time();
    s_last_data_us = s_start_us;
    s_state = PB_WAIT_FIRST;
    snprintf(s_state_txt, sizeof s_state_txt, "buffering");
    xSemaphoreGive(s_mu);
    ESP_LOGI(TAG, "playback.start turn=%u", (unsigned)turn_id);
    if (s_task) xTaskNotifyGive(s_task);
    return ESP_OK;
}

esp_err_t terminal_playback_stop(uint32_t turn_id) {
    if (s_state == PB_IDLE) return ESP_ERR_INVALID_STATE;
    if (s_turn != turn_id) return ESP_ERR_INVALID_ARG;
    s_req_stop = true;
    if (s_task) xTaskNotifyGive(s_task);
    return ESP_OK;
}

bool terminal_playback_push(uint32_t turn_id, const void *pcm, size_t n) {
    if (s_state == PB_IDLE) {
        s_mismatch++;  // 没人在播：这帧无人认领（§5.1 TURN_MISMATCH 语义，只计数）
        return false;
    }
    if (turn_id != s_turn) {
        s_mismatch++;
        return false;
    }
    if (n != FRAME_BYTES) {
        // 下行也按 20 ms 一帧收；别的一律不猜怎么切（§4 只定了 20 ms）。
        s_mismatch++;
        return false;
    }
    xSemaphoreTake(s_mu, portMAX_DELAY);
    bool ok = true;
    if (s_count >= RING_FRAMES) {
        s_overflows++;
        ok = false;  // 缓冲满：丢这一帧并计数，不撑爆内存（§6.5 audio_overflow）
    } else {
        memcpy(s_ring[s_head], pcm, FRAME_BYTES);
        s_head = (s_head + 1) % RING_FRAMES;
        s_count++;
        s_last_data_us = esp_timer_get_time();
    }
    xSemaphoreGive(s_mu);
    if (s_task) xTaskNotifyGive(s_task);
    return ok;
}

void terminal_playback_mark_end(uint32_t turn_id) {
    if (s_state == PB_IDLE || turn_id != s_turn) return;
    s_input_ended = true;  // 把环里剩的放完再收轮（不能一到 END 就切歌）
    if (s_task) xTaskNotifyGive(s_task);
}

void terminal_playback_describe(char *buf, size_t n) {
    if (!buf || n == 0) return;
    int have;
    xSemaphoreTake(s_mu, portMAX_DELAY);
    have = s_count;
    xSemaphoreGive(s_mu);
    snprintf(buf, n,
             "\"play_state\":\"%s\",\"play_turn\":%u,\"ring_frames\":%d,\"ring_ms\":%d,"
             "\"played\":%u,\"play_overflows\":%u,\"play_underruns\":%u,\"play_mismatch\":%u",
             s_state_txt, (unsigned)(s_state == PB_IDLE ? 0 : s_turn), have, have * 20,
             (unsigned)s_played_frames, (unsigned)s_overflows, (unsigned)s_underruns,
             (unsigned)s_mismatch);
}

// ---- 厂商自测：把正弦按服务端的节奏推进播放环 ----
// 走的是真环、真任务、真 I2S，只是不经过网络。用来在接入对端之前
// 先把 start → 预缓冲 → 出声 → END → completed 这条状态机验干净。
#define SELFTEST_TURN 9001

static void selftest_push_task(void *arg) {
    const int frames = (int)(intptr_t)arg;
    static int16_t pcm[FRAME_BYTES / 2];
    double phase = 0.0;
    const double step = 2.0 * M_PI * 440.0 / (double)SAMPLE_HZ;
    for (int i = 0; i < frames; i++) {
        for (size_t j = 0; j < FRAME_BYTES / 2; j++) {
            pcm[j] = (int16_t)(sin(phase) * 8000.0);
            phase += step;
            if (phase > 2.0 * M_PI) phase -= 2.0 * M_PI;
        }
        terminal_playback_push(SELFTEST_TURN, pcm, FRAME_BYTES);
        vTaskDelay(pdMS_TO_TICKS(20));  // 服务端节奏：不突发灌满环
    }
    terminal_playback_mark_end(SELFTEST_TURN);
    vTaskDelete(NULL);
}

esp_err_t terminal_playback_selftest(int ms) {
    if (ms <= 0 || ms > 5000) return ESP_ERR_INVALID_ARG;
    const esp_err_t err = terminal_playback_start(SELFTEST_TURN);
    if (err != ESP_OK) return err;
    if (xTaskCreate(selftest_push_task, "pb_test", 3072, (void *)(intptr_t)(ms / 20), 4, NULL) !=
        pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}