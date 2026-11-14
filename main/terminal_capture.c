// main/terminal_capture.c — 采集任务：pre-roll、实时送帧、轮次收尾。
#include "terminal_capture.h"

#include "bsp_audio.h"
#include "terminal_adpcm.h"
#include "terminal_link.h"
#include "terminal_pav1.h"
#include "terminal_udp.h"

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

// 待发环：按**时间**定深，不按帧数——"能扛多久抖动"才是要表达的量。
// 静态缓冲直接吃 RAM：8 帧（5.1 KB）实测把空闲堆从 28.3 KB 压到 19.1 KB，
// 这台机器总共 400 KB SRAM，所以环先压在 80 ms；真正的深缓冲要等 Opus
// （60 ms/包 × 40 包 = 2.4 s，代价约 7 KB，见 docs/next-plan-opus-udp.md）。
#define PEND_MS 200
#define PEND_FRAMES (PEND_MS / 20)
static uint8_t s_pend[PEND_FRAMES][FRAME_BYTES];
static uint16_t s_pend_len[PEND_FRAMES];  // 实际上线字节数：PCM 640 / ADPCM 168
static uint32_t s_pend_seq[PEND_FRAMES];  // ADPCM 块序号，随载荷上行
static int64_t s_pend_us[PEND_FRAMES];   // 每帧入环时刻，用来算"最老帧年龄"
static int s_pend_head;
static int s_pend_count;

// 上行编解码：默认 ADPCM。PCM 保留为回退路径（联调时用来对照），
// 但它在本机 200 ms RTT 的链路上实测只能送出 4/96 帧，不可用。
typedef enum { CAP_CODEC_PCM = 0, CAP_CODEC_ADPCM = 1 } cap_codec_t;
static cap_codec_t s_codec = CAP_CODEC_ADPCM;
static adpcm_enc_t s_adpcm;
static uint32_t s_seq;                   // 本轮的块序号
static uint8_t s_blk[ADPCM_BLOCK_BYTES];

static void pend_clear(void) {
    s_pend_head = 0;
    s_pend_count = 0;
}

// 环满时丢**最旧**（与上游小智 audio_service.cc 同策），而不是丢最新。
// 理由：丢最新会让流变成"开头保真、结尾被砍"，而一句话的落点通常在结尾，
// ASR 拿到半句只会给出跑偏的答案；丢最旧是"开头保真、中间开洞、结尾保住"，
// 音素基本都在，识别仍能成立。返回 0 表示挤掉了 1 帧（调用方计入 dropped）。
static int pend_push_ex(const uint8_t *data, size_t len, uint32_t seq) {
    if (len > FRAME_BYTES) return 1;   // 放不进去的一律拒收，别写坏内存
    int slot;
    int ret;
    if (s_pend_count >= PEND_FRAMES) {
        slot = s_pend_head;            // 覆盖最旧的那一格
        s_pend_head = (s_pend_head + 1) % PEND_FRAMES;
        ret = 0;
    } else {
        slot = (s_pend_head + s_pend_count) % PEND_FRAMES;
        s_pend_count++;
        ret = 1;
    }
    memcpy(s_pend[slot], data, len);
    s_pend_len[slot] = (uint16_t)len;
    s_pend_seq[slot] = seq;
    s_pend_us[slot] = esp_timer_get_time();
    return ret;
}

// 放一帧 PCM：按当前编解码在此处编码，环里存的是"上线形态"，
// 这样 drain 的时候不需要再做一次编码，也就不会出现"编了一半被打断"的状态。
static int pend_push_pcm(const uint8_t *pcm) {
    if (s_codec == CAP_CODEC_ADPCM) {
        adpcm_enc_block(&s_adpcm, (const int16_t *)pcm, s_blk);
        return pend_push_ex(s_blk, ADPCM_BLOCK_BYTES, s_seq++);
    }
    return pend_push_ex(pcm, FRAME_BYTES, 0);
}

static const uint8_t *pend_front(void) { return s_pend[s_pend_head % PEND_FRAMES]; }

static void pend_pop(void) {
    if (s_pend_count <= 0) return;
    s_pend_head = (s_pend_head + 1) % PEND_FRAMES;
    s_pend_count--;
}

// 环里最旧那一帧已经等了多久（ms）。它同时解释两件事：
// 对内——积压多深；对外——服务端看到的延迟来自哪里。
static int pend_age_ms(void) {
    if (s_pend_count <= 0) return 0;
    const int64_t age = (esp_timer_get_time() - s_pend_us[s_pend_head % PEND_FRAMES]) / 1000;
    return age > 0 ? (int)age : 0;
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
    char ev[224];
    // audio_codec 是这一轮上行的"体格式"，服务端必须照它解，不能靠猜：
    // adpcm-ima16k = kind=5，payload = [u32 seq][4 B 块头][160 B 半字节]，20 ms/块。
    snprintf(ev, sizeof ev,
             "{\"event\":\"capture.started\",\"turn_id\":%u,\"pre_roll_ms\":%d,"
             "\"audio_codec\":\"%s\",\"sample_rate\":%d,\"channels\":1,\"frame_ms\":%d,"
             "\"frame_bytes\":%d}\n",
             (unsigned)turn_id, pre_roll_ms,
             s_codec == CAP_CODEC_ADPCM ? "adpcm-ima16k" : "pcm-s16le",
             SAMPLE_HZ, 20,
             s_codec == CAP_CODEC_ADPCM ? ADPCM_BLOCK_BYTES + 4 : FRAME_BYTES);
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

static int drain_pending(void);   // 定义在下面；flush_preroll 要边走边发

// pre-roll 里最旧的先发：按 head 反推起点，顺序不发反（发反了 ASR 听到的是倒放）。
// 用非阻塞写 + 待发环：起轮瞬间对端 ACK 还没回来，写不进去是正常的，
// 帧留在环里，下一拍继续，不该因此把整个 pre-roll 丢掉。
static void flush_preroll(void) {
    const int n = s_pre_count;
    const int first = (s_pre_head - s_pre_count + PREROLL_FRAMES) % PREROLL_FRAMES;
    for (int i = 0; i < n; i++) {
        const int idx = (first + i) % PREROLL_FRAMES;
        if (!pend_push_pcm(s_pre[idx])) s_dropped++;
        // 边走边发。pre-roll 是 15 帧（300 ms），待发环只有 10 帧（200 ms），
        // 整段塞完再发必然把最旧的 5 帧挤掉——那正是用户开口前的那句话头。
        // 实测每轮固定 dropped=6，就是这里丢的。
        drain_pending();
    }
    s_pre_count = 0;
    s_pre_head = 0;
}

// 把待发环尽量写出去。返回 1 = 已排空；0 = 此刻写不进去（对端窗口关/ACK 未回）；
// -1 = 通道已死。
//
// UDP 优先：一旦服务端用 ack 证明了它听得见我们的 UDP，就走数据报。
// 这里没有"写不进去"这个状态——UDP 要么交出去要么失败，所以环不会因为
// 对端窗口关闭而堆积（TCP 版的 frames=4/dropped=92 那个坑在 UDP 下不存在）。
// 失败不回退到 TCP：半路换传输会把两块音频的顺序弄乱，不如丢这一块（20 ms）。
static int drain_pending(void) {
    while (s_pend_count > 0) {
        const int slot = s_pend_head;
        int r;
        if (s_codec == CAP_CODEC_ADPCM && terminal_udp_ready()) {
            r = terminal_udp_send_adpcm(s_turn, s_pend_seq[slot], s_pend[slot],
                                        s_pend_len[slot])
                    ? 1
                    : -1;
        } else {
            r = (s_codec == CAP_CODEC_ADPCM)
                    ? terminal_pav1_send_adpcm_nowait(s_turn, s_pend_seq[slot], s_pend[slot],
                                                      s_pend_len[slot])
                    : terminal_pav1_send_pcm_nowait(s_turn, s_pend[slot], s_pend_len[slot]);
        }
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
    adpcm_enc_reset(&s_adpcm);   // 每轮从头起步：块头自带状态，轮与轮之间不继承
    s_seq = 0;
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
            // 收轮之后必须把状态复位。finish_turn() 把状态设成 processing，
            // 而正常收轮路径（released/uplink_stall/heap_guard/max_duration/error）
            // 原来没有任何地方把它改回 idle，于是 get_status 永远显示 processing，
            // 外部（含联调的人）会误读成"设备卡在上一轮里"。
            if (strcmp(s_state, "idle") != 0) set_state("idle");
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
            // 环满时也要照常尝试发送。这里以前是 `if (!push) { dropped++; continue; }`，
            // 也就是"满了就不发"——而环只能是"发出去"才会腾空，于是第一次满就永久自锁：
            // 实测表现为 frames=4 / dropped=92，看着像对端不读，其实是自己不再发。
            if (!pend_push_pcm(frame)) {
                s_dropped++;
                s_overflows++;
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

esp_err_t terminal_capture_set_codec(const char *name) {
    if (!name) return ESP_ERR_INVALID_ARG;
    if (s_active || s_req_start) return ESP_ERR_INVALID_STATE;   // 轮次里不换编码
    if (strcmp(name, "adpcm") == 0) {
        s_codec = CAP_CODEC_ADPCM;
    } else if (strcmp(name, "pcm") == 0) {
        s_codec = CAP_CODEC_PCM;
    } else {
        return ESP_ERR_INVALID_ARG;
    }
    ESP_LOGI(TAG, "上行编解码切换为 %s", name);
    return ESP_OK;
}

const char *terminal_capture_codec_name(void) {
    return s_codec == CAP_CODEC_ADPCM ? "adpcm" : "pcm";
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
             "\"state\":\"%s\",\"turn_id\":%u,\"codec\":\"%s\",\"ring_ms\":%d,\"underruns\":0,"
             "\"overflows\":%u,\"frames\":%u,\"dropped\":%u,\"read_fail\":%u,\"turns\":%u,"
             "\"rms\":%d,\"peak\":%d,\"pre_frames\":%u,\"pre_ms\":%u,\"pre_rms\":%d,"
             "\"pre_peak\":%d,\"pend\":%d,\"pend_age_ms\":%d,\"pend_ms\":%d,\"seq\":%u",
             st, (unsigned)(s_active ? s_turn : 0), terminal_capture_codec_name(), pend_age_ms(),
             (unsigned)s_overflows,
             (unsigned)s_sent_frames, (unsigned)s_dropped, (unsigned)s_read_fail,
             (unsigned)s_turns, s_samples ? (int)(sqrt(s_sum_sq / (double)s_samples) + 0.5) : 0,
             (int)s_peak, (unsigned)s_pre_frames, (unsigned)(s_pre_frames * 20), pre_rms,
             (int)s_pre_peak, s_pend_count, pend_age_ms(), s_pend_count * 20, (unsigned)s_seq);
    // UDP 通道自证：ready 之前采集一定还在走 TCP，所以这两个字段能直接
    // 说明"这一轮的音频到底走的是哪条路"。
    // 截断在这里是致命的：JSON 一断，上位机把整条 get_audio 都解析不出来，
    // 表现成"设备没应答"，比丢一个字段难查得多（加 udp_badmac 时就踩过）。
    const size_t used = strlen(buf);
    if (used + 2 < n) {
        buf[used] = ',';
        const int w = terminal_udp_diag(buf + used + 1, n - used - 1);
        if (w < 0 || (size_t)w >= n - used - 1) {
            ESP_LOGW(TAG, "get_audio 被截断：缓冲 %u 字节不够（需要约 %u），调大 terminal_link.c",
                     (unsigned)n, (unsigned)(used + 1 + (w > 0 ? (size_t)w : 0)));
        }
    } else {
        ESP_LOGW(TAG, "get_audio 没空间放 udp 诊断：%u/%u 已用", (unsigned)used, (unsigned)n);
    }
}

void terminal_capture_describe(char *buf, size_t n) {
    if (!buf || n == 0) return;
    snprintf(buf, n, "\"cap_state\":\"%s\",\"cap_turn\":%u,\"cap_frames\":%u",
             s_active ? "capturing" : (s_armed ? "armed" : s_state),
             (unsigned)(s_active ? s_turn : 0), (unsigned)s_sent_frames);
}