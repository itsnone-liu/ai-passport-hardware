// main/terminal_capture.c — 采集任务：pre-roll、实时送帧、轮次收尾。
#include "terminal_capture.h"

#include "bsp_audio.h"
#include "terminal_link.h"
#include "terminal_pav1.h"

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

static const char *TAG = "cap";

#define SAMPLE_HZ 16000
#define FRAME_BYTES 640              // 20 ms @ 16 kHz mono s16（§4 定案，不协商）
#define PREROLL_FRAMES 15            // 300 ms
#define MAX_TURN_MS 30000            // 与服务端 30 s 缓冲上限对齐
#define DROP_LIMIT 25                // 连续丢 25 帧（500 ms）判 overflow
#define ARM_TIMEOUT_MS 3000          // 按键后等 capture.start 的时限（3 s 容一次网络抖动）
#define STALL_MS 2000                // 连续这么久没有一帧送出去 = 上行已死，收轮
#define WARMUP_FRAMES 6              // codec 就绪后丢弃的旧帧数（6×20 ms = 120 ms）

// 上行安全垫。实测：对端不读时堆 28.2 KB → 2.6 KB，收轮后不返还，随后控制通道
// 发不出包、会话掉线、重连也建不起来，设备就锁死到重启。
// 判据用 free，不用 largest：起轮瞬间 DMA 与 codec 缓冲会把堆打碎，largest 会
// 立刻掉到 7.6 KB（此时 free 还有 24 KB），用 largest 会把正常轮次全部误杀。
// 阈值 4 KB：SO_SNDBUF 已压到 4 KB、待发环是静态 BSS 不占堆，正常起轮后 free
// 落在 15 KB 以上；跌到 4 KB 说明对端确实没在收，这时收轮比继续灌更划算。
#define HEAP_GUARD_BYTES (4 * 1024)
#define HEAP_GUARD_MALLOC (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)
#define HEAP_GUARD_GRACE_MS 300      // 起轮头 300 ms 不判：这里本来就是分配尖峰
#define HEAP_GUARD_STREAK 3          // 连续 3 拍（60 ms）低于阈值才算真见底

// 待发环。采集任务只往这里放，放不进就丢并计数——绝不在写上阻塞。
// 4 帧 = 80 ms 余量。静态缓冲直接吃 RAM：8 帧（5.1 KB）实测把空闲堆从 28.3 KB
// 压到 19.1 KB，而这台机器总共才 400 KB SRAM，宁可少缓冲多留堆。
#define PEND_FRAMES 4
static uint8_t s_pend[PEND_FRAMES][FRAME_BYTES];
static int s_pend_head;
static int s_pend_count;

static void pend_clear(void) {
    s_pend_head = 0;
    s_pend_count = 0;
}

static bool pend_push(const uint8_t *frame) {
    if (s_pend_count >= PEND_FRAMES) return false;
    memcpy(s_pend[(s_pend_head + s_pend_count) % PEND_FRAMES], frame, FRAME_BYTES);
    s_pend_count++;
    return true;
}

static const uint8_t *pend_front(void) { return s_pend[s_pend_head % PEND_FRAMES]; }

static void pend_pop(void) {
    if (s_pend_count <= 0) return;
    s_pend_head = (s_pend_head + 1) % PEND_FRAMES;
    s_pend_count--;
}

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
static uint32_t s_stall_ref_frames;   // 上次判定"还在动"时的 s_sent_frames
static int64_t s_last_sent_us;        // 上次"还在动"的墙钟
static uint32_t s_dropped;
static uint32_t s_overflows;
static uint32_t s_turns;
static uint32_t s_read_fail;
static double s_sum_sq;
static uint32_t s_samples;
static int32_t s_peak;
static int s_low_streak;             // 连续几拍堆低于安全垫
static char s_state[16] = "idle";

// pre-roll 单独记账：服务端要能分清"这 300 ms 到底有声音没有"，
// 否则它只能看到帧数对得上，而全零帧也能凑出正确计数。
static uint32_t s_pre_frames;
static double s_pre_sum_sq;
static uint32_t s_pre_samples;
static int32_t s_pre_peak;

// 上行安全垫：返回 true 表示还有余量继续送帧。
static bool heap_headroom(void) {
    return heap_caps_get_free_size(HEAP_GUARD_MALLOC) >= HEAP_GUARD_BYTES;
}

static void pre_reset(void) {
    s_pre_frames = 0;
    s_pre_sum_sq = 0;
    s_pre_samples = 0;
    s_pre_peak = 0;
}

static void pre_accumulate(const uint8_t *frame) {
    const int16_t *p = (const int16_t *)frame;
    for (int i = 0; i < FRAME_BYTES / 2; i++) {
        const int32_t v = p[i];
        s_pre_sum_sq += (double)v * (double)v;
        if (v > s_pre_peak) s_pre_peak = v;
        if (-v > s_pre_peak) s_pre_peak = -v;
    }
    s_pre_samples += FRAME_BYTES / 2;
    s_pre_frames++;
}

// 上行携带的到底是不是真实声音？帧数对得上只说明边界没错，
// 全零帧也能凑出正确的计数——所以统计这一轮的 RMS 与峰值。
static void accumulate(const uint8_t *frame) {
    const int16_t *p = (const int16_t *)frame;
    for (int i = 0; i < FRAME_BYTES / 2; i++) {
        const int32_t v = p[i];
        s_sum_sq += (double)v * (double)v;
        if (v > s_peak) s_peak = v;
        if (-v > s_peak) s_peak = -v;
    }
    s_samples += FRAME_BYTES / 2;
}

static void set_state(const char *st) { snprintf(s_state, sizeof s_state, "%s", st); }

static void emit_capture_started(uint32_t turn_id, int pre_roll_ms) {
    char ev[128];
    snprintf(ev, sizeof ev,
             "{\"event\":\"capture.started\",\"turn_id\":%u,\"pre_roll_ms\":%d}\n",
             (unsigned)turn_id, pre_roll_ms);
    terminal_link_send_src(LINK_SRC_TCP, ev);
}

static void emit_capture_ended(uint32_t turn_id, const char *reason) {
    char ev[256];
    const int rms = s_samples ? (int)(sqrt(s_sum_sq / (double)s_samples) + 0.5) : 0;
    snprintf(ev, sizeof ev,
             "{\"event\":\"capture.ended\",\"turn_id\":%u,\"reason\":\"%s\",\"frames\":%u,"
             "\"dropped\":%u,\"rms\":%d,\"peak\":%d}\n",
             (unsigned)turn_id, reason, (unsigned)s_sent_frames, (unsigned)s_dropped, rms,
             (int)s_peak);
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
// 用非阻塞写 + 待发环：起轮瞬间对端 ACK 还没回来，写不进去是正常的，
// 帧留在环里，下一拍继续，不该因此把整个 pre-roll 丢掉。
static void flush_preroll(void) {
    const int n = s_pre_count;
    const int first = (s_pre_head - s_pre_count + PREROLL_FRAMES) % PREROLL_FRAMES;
    for (int i = 0; i < n; i++) {
        const int idx = (first + i) % PREROLL_FRAMES;
        if (!pend_push(s_pre[idx])) s_dropped++;
    }
    s_pre_count = 0;
    s_pre_head = 0;
}

// 把待发环尽量写出去。返回 1 = 已排空；0 = 此刻写不进去（对端窗口关/ACK 未回）；
// -1 = 通道已死。
static int drain_pending(void) {
    while (s_pend_count > 0) {
        const int r = terminal_pav1_send_pcm_nowait(s_turn, pend_front(), FRAME_BYTES);
        if (r > 0) {
            s_sent_frames++;
            pend_pop();
            continue;
        }
        return r;   // 0 = 此刻写不进去，-1 = 通道已死
    }
    return 1;
}

static void begin_turn(void) {
    s_turn = s_req_turn;
    s_req_start = false;
    const int pre_ms = s_pre_count * 20;
    s_active = true;
    s_armed = false;
    s_turn_start_us = esp_timer_get_time();
    s_sent_frames = 0;
    s_stall_ref_frames = 0;
    s_last_sent_us = s_turn_start_us;
    s_dropped = 0;
    s_sum_sq = 0;
    s_samples = 0;
    s_peak = 0;
    s_low_streak = 0;
    pend_clear();
    s_turns++;
    set_state("listening");
    flush_preroll();
    emit_capture_started(s_turn, pre_ms);
    ESP_LOGI(TAG, "轮次 %u 开始（pre-roll %d ms，heap=%u）", (unsigned)s_turn, pre_ms,
             (unsigned)esp_get_free_heap_size());
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
            // 打开/切换 codec 之后 DMA 里可能还是上一轮的旧数据：BSP 是 6 个描述符 ×
            // 240 帧 = 1440 帧 = 90 ms 深，直接读出来填 pre-roll，等于把"上一句话的尾巴"
            // 或者复位前的残留当成本次开口。上游小智固件同样在开麦后丢 120 ms
            // （audio_service.cc 的 warmup 分支），数字与 DMA 深度对得上。
            for (int i = 0; i < WARMUP_FRAMES; i++) {
                if (bsp_audio_read(frame, FRAME_BYTES) != ESP_OK) break;
            }
            ESP_LOGI(TAG, "codec 就绪，丢弃前 %d ms 旧帧", WARMUP_FRAMES * 20);
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
                // 松手：把还压在环里的尾巴尽力写完再收轮，最多等 200 ms。
                // 这段尾巴就是用户最后半句话，丢了比晚 200 ms 更糟。
                for (int i = 0; i < 10 && s_pend_count > 0; i++) {
                    if (drain_pending() != 0) break;
                    vTaskDelay(pdMS_TO_TICKS(20));
                }
                // 丢的比送出去的多，说明这一轮对端根本没在收，别报 released 骗人：
                // 服务端拿它当"完整一轮"去跑 ASR，只会得到半句或空结果。
                if (s_dropped > s_sent_frames) {
                    ESP_LOGW(TAG, "轮次 %u 松手，但只送出 %u 帧、丢了 %u 帧：对端没在收",
                             (unsigned)s_turn, (unsigned)s_sent_frames, (unsigned)s_dropped);
                    finish_turn("uplink_stall");
                } else {
                    finish_turn("released");
                }
                continue;
            }
            // 堆见底就收轮，但先给起轮一个宽限窗：头 300 ms 本来就是分配尖峰，
            // 在那之前判"见底"必然误杀正常轮次。而且要连续 3 拍都低才算数。
            const int64_t in_turn_ms = (esp_timer_get_time() - s_turn_start_us) / 1000;
            if (in_turn_ms > HEAP_GUARD_GRACE_MS && !heap_headroom()) {
                s_low_streak++;
                if (s_low_streak >= HEAP_GUARD_STREAK) {
                    ESP_LOGW(TAG, "堆见底（free=%u < %d，连续 %d 拍），提前收轮保住控制通道",
                             (unsigned)heap_caps_get_free_size(HEAP_GUARD_MALLOC),
                             HEAP_GUARD_BYTES, s_low_streak);
                    finish_turn("heap_guard");
                    continue;
                }
            } else {
                s_low_streak = 0;
            }
            // 卡死检测（按墙钟，不按丢帧计数）：对端不读时，丢帧计数要 25 次才收轮，
            // 而每次 send 都可能阻塞几百毫秒，加起来是几十秒的"看起来还在采集"。
            // 用"连续 STALL_MS 没有一帧送出去"来判定，时间边界是可解释的。
            const int64_t now_us = esp_timer_get_time();
            if (s_sent_frames != s_stall_ref_frames) {
                s_stall_ref_frames = s_sent_frames;
                s_last_sent_us = now_us;
            } else if ((now_us - s_last_sent_us) / 1000 > STALL_MS) {
                ESP_LOGW(TAG, "上行卡住 %d ms（已送 %u 帧，环里还压 %d 帧），收轮", STALL_MS,
                         (unsigned)s_sent_frames, s_pend_count);
                s_overflows++;
                emit_error("audio_overflow", s_turn);
                finish_turn("error");
                continue;
            }
            if (in_turn_ms > MAX_TURN_MS) {
                finish_turn("max_duration");
                continue;
            }
            // 采集到的都算进统计（RMS 说的是"收到了什么声音"，与能否立即送出去无关）
            accumulate(frame);
            if (!pend_push(frame)) {
                // 环满：对端真的跟不上。丢最新并计数，绝不阻塞采集任务。
                s_dropped++;
                s_overflows++;
                continue;
            }
            const int r = drain_pending();
            if (r < 0) {
                emit_error("audio_send_fail", s_turn);
                finish_turn("error");
            }
            continue;
        }

        // 还没开始送：填 pre-roll 环形缓冲（覆盖最旧的）
        memcpy(s_pre[s_pre_head], frame, FRAME_BYTES);
        s_pre_head = (s_pre_head + 1) % PREROLL_FRAMES;
        if (s_pre_count < PREROLL_FRAMES) s_pre_count++;
        pre_accumulate(frame);
        if ((esp_timer_get_time() - s_arm_start_us) / 1000 > ARM_TIMEOUT_MS) {
            // 按键起了个头但服务端没在时限内给 capture.start：放弃本轮。
            ESP_LOGW(TAG, "按键后 %d ms 未收到 capture.start，放弃", ARM_TIMEOUT_MS);
            emit_error("cap_start_timeout", 0);
            s_armed = false;
            s_pre_count = 0;
            s_pre_head = 0;
            pre_reset();
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
    // 堆不够就不开轮。开了的结果是上行把控制通道一起噎死（实测），
    // 而控制通道死了连"这一轮为什么失败"都报不回去。
    if (!heap_headroom()) {
        ESP_LOGW(TAG, "拒绝起采：堆余量不足（free=%u < %d）",
                 (unsigned)heap_caps_get_free_size(HEAP_GUARD_MALLOC), HEAP_GUARD_BYTES);
        return ESP_ERR_NO_MEM;
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

// 按键松手。说话的结束权在按键这一侧：服务端不参与判定，否则松手到服务端
// 发现之间会多送一段尾巴（实测 200 ms，够录进半句话）。
// armed 阶段松手 = 用户什么都没说就取消，不该留一个空轮次等超时。
void terminal_capture_request_end(void) {
    if (s_active) {
        s_stop_turn = s_turn;
        s_req_stop = true;
        return;
    }
    if (s_armed) {
        s_armed = false;
        s_pre_count = 0;
        s_pre_head = 0;
        pre_reset();
        set_state("idle");
        ESP_LOGI(TAG, "未起采即松手，取消本轮");
    }
}

// 扫描热点必须断开 Wi-Fi，而采集正靠 Wi-Fi 送帧。给 scan 一个"现在能不能打断"的判断：
// armed 也算忙——那时 pre-roll 正在填，断开就等于把这一轮的开口吃掉。
bool terminal_capture_busy(void) { return s_active || s_armed; }

// 只表示"正在为某一轮送实时帧"。上行看门狗用它：armed 阶段还没有帧要送，
// 拿"没有进展"去判它会误杀"按键预热完等 capture.start"的正常流程。
bool terminal_capture_capturing(void) { return s_active; }

// 从别的任务强制收轮。存在的理由：采集任务自己可能阻塞在 I2S 读或 socket 发送里，
// 这时它能做的检查一个都跑不到。收轮必须由还活着的那个人完成。
void terminal_capture_force_stop(const char *reason) {
    if (!s_active && !s_armed) return;
    const uint32_t turn = s_turn;
    s_active = false;
    s_armed = false;
    s_req_stop = false;
    emit_capture_ended(turn, reason);
    set_state("processing");
    ESP_LOGW(TAG, "轮次 %u 被强制收尾（%s）", (unsigned)turn, reason);
}

void terminal_capture_arm(void) {
    if (s_active) return;
    s_armed = true;
    s_pre_count = 0;
    s_pre_head = 0;
    pre_reset();
    s_arm_start_us = esp_timer_get_time();
    set_state("listening");
    if (s_task) xTaskNotifyGive(s_task);
    ESP_LOGI(TAG, "按键起采，等 capture.start");
}

void terminal_capture_get_audio(char *buf, size_t n) {
    if (!buf || n == 0) return;
    const char *st = s_active ? "capturing" : (s_armed ? "armed" : s_state);
    const int pre_rms =
        s_pre_samples ? (int)(sqrt(s_pre_sum_sq / (double)s_pre_samples) + 0.5) : 0;
    snprintf(buf, n,
             "\"state\":\"%s\",\"turn_id\":%u,\"ring_ms\":0,\"underruns\":0,"
             "\"overflows\":%u,\"frames\":%u,\"dropped\":%u,\"read_fail\":%u,\"turns\":%u,"
             "\"rms\":%d,\"peak\":%d,\"pre_frames\":%u,\"pre_ms\":%u,\"pre_rms\":%d,"
             "\"pre_peak\":%d",
             st, (unsigned)(s_active ? s_turn : 0), (unsigned)s_overflows,
             (unsigned)s_sent_frames, (unsigned)s_dropped, (unsigned)s_read_fail,
             (unsigned)s_turns, s_samples ? (int)(sqrt(s_sum_sq / (double)s_samples) + 0.5) : 0,
             (int)s_peak, (unsigned)s_pre_frames, (unsigned)(s_pre_frames * 20), pre_rms,
             (int)s_pre_peak);
}

void terminal_capture_describe(char *buf, size_t n) {
    if (!buf || n == 0) return;
    snprintf(buf, n, "\"cap_state\":\"%s\",\"cap_turn\":%u,\"cap_frames\":%u",
             s_active ? "capturing" : (s_armed ? "armed" : s_state),
             (unsigned)(s_active ? s_turn : 0), (unsigned)s_sent_frames);
}