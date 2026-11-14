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
// 下行环深度：原为 60 帧（1.2 s）。实测 Opus 编码器状态需要 24.7 KB 常驻，
// 而空闲堆只有 20.8 KB，装不下；把下行环收到 40 帧（800 ms）腾出 12.8 KB。
// 依据：下行走 TCP，网络抖动已被协议栈吸收，这个环只吸收调度抖动；
// 800 ms 仍是实测最大帧间隔（~0.45 s）的近 2 倍。下行改为 Opus 后再评估。
#define RING_FRAMES 32           // 32 × 640 = 20,480 B = 640 ms
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
static int64_t s_next_frame_us;  // 下一帧的播放时刻（自走的播放时钟）

static enum { PB_IDLE, PB_WAIT_FIRST, PB_PLAYING } s_state;
static uint32_t s_played_frames;
static uint32_t s_overflows;
static uint32_t s_underruns;
static uint32_t s_mismatch;
// 帧为什么没进环：三种完全不同的原因，各自计数（混成一个数就只能靠猜）。
static uint32_t s_rej_idle, s_rej_turn, s_rej_size, s_push_ok;
// 上面这些是 per-turn（playback.start 归零）。校验守恒时若拿 per-turn 的 played
// 去差分累计的 rx，每轮都会得出"played+0 而 rx+114"这种看起来不可能的结论。
// 所以另记一个自启动累计值，让差分口径唯一。
static uint32_t s_played_total;
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
                s_next_frame_us = esp_timer_get_time();  // 播放时钟从"出声这一刻"起算
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
        // 播放时钟：每帧按 20 ms 推进，写操作快就等、慢就立刻追上。
        // 不能只依赖 bsp_audio_write 阻塞——它现在确实阻塞（实测 150 帧名义 3000 ms、
        // 实际 2930 ms），但一旦哪天驱动只把数据拷进内部缓冲就返回，环会被瞬间抽干，
        // 表现为"起播后几百毫秒就 underrun"。节拍必须由自己掌握。
        const int64_t now = esp_timer_get_time();
        if (now < s_next_frame_us) {
            vTaskDelay(pdMS_TO_TICKS(2));
            if (s_input_ended) {
                int have;
                xSemaphoreTake(s_mu, portMAX_DELAY);
                have = s_count;
                xSemaphoreGive(s_mu);
                if (have == 0) {
                    emit_ended(s_turn, "completed");
                    s_state = PB_IDLE;
                    snprintf(s_state_txt, sizeof s_state_txt, "idle");
                }
            }
            continue;
        }

        xSemaphoreTake(s_mu, portMAX_DELAY);
        const bool got = pop_frame(frame);
        xSemaphoreGive(s_mu);

        if (got) {
            s_last_data_us = esp_timer_get_time();
            if (bsp_audio_write(frame, FRAME_BYTES) == ESP_OK) {
                s_played_frames++;
                s_played_total++;
            }
            s_next_frame_us += 20000;  // 20 ms/帧，与 §4 的帧长严格对应
            const int64_t after = esp_timer_get_time();
            if (s_next_frame_us < after) s_next_frame_us = after;  // 落后就追上，不补播
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
        s_mismatch++;
        s_rej_idle++;  // 没人在播：这帧无人认领（§5.1 TURN_MISMATCH 语义，只计数）
        return false;
    }
    if (turn_id != s_turn) {
        s_mismatch++;
        s_rej_turn++;  // 轮次不符：服务端推的是另一轮
        return false;
    }
    if (n != FRAME_BYTES) {
        // 下行也按 20 ms 一帧收；别的一律不猜怎么切（§4 只定了 20 ms）。
        s_mismatch++;
        s_rej_size++;  // 帧长不对：640 B 之外不接受
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
        s_push_ok++;
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

bool terminal_playback_busy(void) { return s_state != PB_IDLE; }

void terminal_playback_describe(char *buf, size_t n) {
    if (!buf || n == 0) return;
    int have;
    xSemaphoreTake(s_mu, portMAX_DELAY);
    have = s_count;
    xSemaphoreGive(s_mu);
    snprintf(buf, n,
             "\"play_state\":\"%s\",\"play_turn\":%u,\"ring_frames\":%d,\"ring_ms\":%d,"
             "\"played\":%u,\"played_total\":%u,"
             "\"play_overflows\":%u,\"play_underruns\":%u,\"play_mismatch\":%u,"
             "\"push_ok\":%u,\"rej_idle\":%u,\"rej_turn\":%u,\"rej_size\":%u",
             s_state_txt, (unsigned)(s_state == PB_IDLE ? 0 : s_turn), have, have * 20,
             (unsigned)s_played_frames, (unsigned)s_played_total, (unsigned)s_overflows,
             (unsigned)s_underruns, (unsigned)s_mismatch, (unsigned)s_push_ok,
             (unsigned)s_rej_idle, (unsigned)s_rej_turn, (unsigned)s_rej_size);
}

// ---- 厂商自测：把正弦按服务端的节奏推进播放环 ----
// 走的是真环、真任务、真 I2S，只是不经过网络。用来在接入对端之前
// 先把 start → 预缓冲 → 出声 → END → completed 这条状态机验干净。
#define SELFTEST_TURN 9001

static void selftest_push_task(void *arg) {
    // 低 16 位 = 帧数，高 16 位 = 每 20 ms 推几帧（speed）。
    const int frames = (int)((intptr_t)arg & 0xffff);
    const int speed = (int)(((intptr_t)arg >> 16) & 0xffff);
    static int16_t pcm[FRAME_BYTES / 2];
    double phase = 0.0;
    const double step = 2.0 * M_PI * 440.0 / (double)SAMPLE_HZ;
    int pushed = 0;
    while (pushed < frames) {
        for (int k = 0; k < speed && pushed < frames; k++, pushed++) {
            for (size_t j = 0; j < FRAME_BYTES / 2; j++) {
                pcm[j] = (int16_t)(sin(phase) * 8000.0);
                phase += step;
                if (phase > 2.0 * M_PI) phase -= 2.0 * M_PI;
            }
            terminal_playback_push(SELFTEST_TURN, pcm, FRAME_BYTES);
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    terminal_playback_mark_end(SELFTEST_TURN);
    vTaskDelete(NULL);
}

esp_err_t terminal_playback_selftest(int ms, int speed) {
    if (ms <= 0 || ms > 5000) return ESP_ERR_INVALID_ARG;
    if (speed < 1 || speed > 8) return ESP_ERR_INVALID_ARG;
    const esp_err_t err = terminal_playback_start(SELFTEST_TURN);
    if (err != ESP_OK) return err;
    const int frames = ms / 20;
    if (xTaskCreate(selftest_push_task, "pb_test", 3072,
                    (void *)(intptr_t)((frames & 0xffff) | (speed << 16)), 4, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}