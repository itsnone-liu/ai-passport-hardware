// main/terminal_audio.c — ES8311 全双工自测。
//
// 要回答的问题都是"真机才答得出"的：16 kHz/单声道/s16le 到底能不能采、20 ms 帧
// 实际耗时多少（时钟准不准）、内存装得下几秒、回放有没有欠载。
// 这些数字直接决定协议里帧长、抖动缓冲和最大语句时长的取值。
#include "terminal_audio.h"

#include "bsp_audio.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "terminal_link.h"
#include "terminal_ui.h"
#include <math.h>

#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *TAG = "audio";

#define SAMPLE_HZ 16000
#define FRAME_MS_DEFAULT 20
#define FRAME_MS_MAX 40
#define FRAME_BYTES_MAX (SAMPLE_HZ / 1000 * FRAME_MS_MAX * 2)  // 1280
#define MAX_RECORD_MS 2000  // 再长就要 128 KB 连续堆，实测分配不出来（见 no mem 分支）
#define TASK_STACK 6144

// 协议 D6 要在 20 ms / 40 ms 之间做决定，所以帧长做成可选的：
// 参数用一个 int 同时捎带两个值，避免为一次自测去申请结构体。
#define ARG_PACK(ms, frame) (((ms) & 0xFFFF) | ((frame) << 16))
#define ARG_MS(a) ((int)((a) & 0xFFFF))
#define ARG_FRAME(a) ((int)(((a) >> 16) & 0xFFFF))

static volatile bool s_busy;
static uint32_t s_seq;

static void emit(const char *fmt, ...) {
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    terminal_link_send(buf);
}

static void selftest_task(void *arg) {
    const int packed = (int)(intptr_t)arg;
    const int want_ms = ARG_MS(packed);
    int frame_ms = ARG_FRAME(packed);
    if (frame_ms != 20 && frame_ms != 40) frame_ms = FRAME_MS_DEFAULT;
    const int frame_bytes = SAMPLE_HZ / 1000 * frame_ms * 2;  // 20 ms→640 B, 40 ms→1280 B

    int rec_ms = want_ms;
    if (rec_ms < frame_ms) rec_ms = frame_ms;
    if (rec_ms > MAX_RECORD_MS) rec_ms = MAX_RECORD_MS;
    const int frames = rec_ms / frame_ms;
    const size_t total = (size_t)frames * (size_t)frame_bytes;

    // 明确用堆而不是栈：4 秒就是 128 KB，任何任务栈都放不下。
    int16_t *buf = malloc(total);
    if (!buf) {
        // 报出真实可用块：这条日志就是"一条话到底能缓冲多久"的现场答案。
        emit("{\"event\":\"audio_selftest\",\"ok\":false,\"err\":\"no mem\",\"need\":%u,"
             "\"heap\":%u,\"heap_block\":%u}\n",
             (unsigned)total, (unsigned)esp_get_free_heap_size(),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT | MALLOC_CAP_DEFAULT));
        s_busy = false;
        vTaskDelete(NULL);
        return;
    }

    esp_err_t err = bsp_audio_set_format(SAMPLE_HZ, 16, 1);
    if (err != ESP_OK) {
        emit("{\"event\":\"audio_selftest\",\"ok\":false,\"err\":\"set_format %s\"}\n",
             esp_err_to_name(err));
        free(buf);
        s_busy = false;
        vTaskDelete(NULL);
        return;
    }

    // ---- 采集 ----
    size_t got_bytes = 0;
    int read_fail = 0;
    const int64_t t_rec0 = esp_timer_get_time();
    for (int i = 0; i < frames; i++) {
        const esp_err_t r =
            bsp_audio_read((uint8_t *)buf + (size_t)i * (size_t)frame_bytes, frame_bytes);
        if (r == ESP_OK) {
            got_bytes += (size_t)frame_bytes;
        } else {
            read_fail++;
        }
    }
    const int64_t t_rec1 = esp_timer_get_time();

    // ---- 电平 ----
    const size_t got_samples = got_bytes / 2;
    int32_t peak = 0;
    int64_t sumsq = 0;
    int64_t sum = 0;
    int clips = 0;
    int nonzero = 0;
    for (size_t i = 0; i < got_samples; i++) {
        const int32_t v = buf[i];
        const int32_t a = (v < 0) ? -v : v;
        if (a > peak) peak = a;
        if (a >= 32000) clips++;  // 接近满量程就是削波，ASR 会变差
        if (v != 0) nonzero++;
        sumsq += (int64_t)v * v;
        sum += v;
    }
    const int rms = got_samples ? (int)__builtin_sqrt((double)(sumsq / (int64_t)got_samples)) : 0;
    const int dc = got_samples ? (int)(sum / (int64_t)got_samples) : 0;
    // 削波占比大 → 麦克风增益过高；采样几乎全是 0 → 没采到东西；
    // 前 8 个值若是 0/重复/极端交替 → 位宽或对齐不对（32 bit 槽按 16 bit 读的典型症状）。
    ESP_LOGI(TAG, "采样: rms=%d peak=%d dc=%d clips=%d nonzero=%d 前8: %d %d %d %d %d %d %d %d", rms,
             peak, dc, clips, nonzero, got_samples > 0 ? buf[0] : 0, got_samples > 1 ? buf[1] : 0,
             got_samples > 2 ? buf[2] : 0, got_samples > 3 ? buf[3] : 0,
             got_samples > 4 ? buf[4] : 0, got_samples > 5 ? buf[5] : 0,
             got_samples > 6 ? buf[6] : 0, got_samples > 7 ? buf[7] : 0);

    // ---- 回放 ----
    int write_fail = 0;
    const int64_t t_play0 = esp_timer_get_time();
    for (int i = 0; i < frames; i++) {
        if (bsp_audio_write((uint8_t *)buf + (size_t)i * (size_t)frame_bytes, frame_bytes) !=
            ESP_OK) {
            write_fail++;
        }
    }
    const int64_t t_play1 = esp_timer_get_time();

    const int rec_elapsed = (int)((t_rec1 - t_rec0) / 1000);
    const int play_elapsed = (int)((t_play1 - t_play0) / 1000);

    ESP_LOGI(TAG, "自测: 录 %d ms 用 %d ms，放 %d ms 用 %d ms，rms=%d peak=%d", rec_ms, rec_elapsed,
             rec_ms, play_elapsed, rms, peak);

    // ratio/100 = 实际耗时 ÷ 名义时长。接近 100 说明帧节拍与 codec 时钟一致；
    // 明显小于 100 说明读接口没在等时钟（数据是凭空返回的），大于 100 说明欠载。
    emit("{\"event\":\"audio_selftest\",\"ok\":true,\"seq\":%u,\"format\":\"pcm_s16le/%d/1\","
         "\"frame_ms\":%d,\"frames\":%d,\"frame_bytes\":%d,\"bytes\":%u,\"rms\":%d,\"peak\":%d,"
         "\"dc\":%d,\"clips\":%d,\"nonzero\":%d,"
         "\"record_ms\":%d,\"play_ms\":%d,\"rate_ratio\":%d,\"read_fail\":%d,\"write_fail\":%d}\n",
         (unsigned)++s_seq, SAMPLE_HZ, frame_ms, frames, frame_bytes, (unsigned)got_bytes, rms, peak,
         dc, clips, nonzero, rec_elapsed, play_elapsed,
         rec_ms ? (rec_elapsed * 100 / rec_ms) : 0, read_fail, write_fail);

    free(buf);
    s_busy = false;
    vTaskDelete(NULL);
}

esp_err_t terminal_audio_selftest_start(int ms, int frame_ms) {
    if (s_busy) return ESP_ERR_INVALID_STATE;
    if (frame_ms != 20 && frame_ms != 40) return ESP_ERR_INVALID_ARG;
    s_busy = true;
    if (xTaskCreate(selftest_task, "dsh_audio_test", TASK_STACK,
                    (void *)(intptr_t)ARG_PACK(ms, frame_ms), 4, NULL) != pdPASS) {
        s_busy = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

bool terminal_audio_busy(void) { return s_busy; }

// 纯音测试：只走"代码 → I2S → codec DAC → 扬声器"，不经过麦克风。
// 回环自测听不见时分不清是喇叭坏了还是麦克风没采到，这条能一刀切开。
static void tone_task(void *arg) {
    // ARG_PACK(ms, x) 把 ms 放低 16 位、x 放高 16 位，解码必须按同一约定。
    const int ms = (int)((intptr_t)arg & 0xffff);
    const int hz = (int)(((intptr_t)arg >> 16) & 0xffff);
    const int amp = 8000;
    const size_t frame_bytes = 640;  // 20 ms
    int16_t buf[frame_bytes / 2];
    bsp_audio_set_format(SAMPLE_HZ, 16, 1);
    const int frames = ms / 20;
    double phase = 0.0;
    const double step = 2.0 * M_PI * (double)hz / (double)SAMPLE_HZ;
    int write_fail = 0;
    const int64_t t0 = esp_timer_get_time();
    for (int i = 0; i < frames; i++) {
        for (size_t j = 0; j < frame_bytes / 2; j++) {
            buf[j] = (int16_t)(sin(phase) * (double)amp);
            phase += step;
            if (phase > 2.0 * M_PI) phase -= 2.0 * M_PI;
        }
        if (bsp_audio_write(buf, frame_bytes) != ESP_OK) write_fail++;
    }
    const int elapsed = (int)((esp_timer_get_time() - t0) / 1000);
    ESP_LOGI(TAG, "纯音: %d Hz %d ms 用 %d ms，write_fail=%d，volume=%d", hz, ms, elapsed, write_fail,
             terminal_ui_volume());
    char ev[192];
    snprintf(ev, sizeof ev,
             "{\"event\":\"play_tone\",\"ok\":%s,\"hz\":%d,\"ms\":%d,\"elapsed_ms\":%d,\"amp\":%d,"
             "\"volume\":%d,\"write_fail\":%d}\n",
             write_fail == 0 ? "true" : "false", hz, ms, elapsed, amp, terminal_ui_volume(),
             write_fail);
    terminal_link_send_src(LINK_SRC_USB, ev);
    s_busy = false;
    vTaskDelete(NULL);
}

// 测 bsp_audio_write 到底会不会阻塞：连续写 N 帧静音，量总耗时。
// 若耗时 ≈ N×20 ms → 写操作被 I2S 时钟节流（可以依赖它当节拍）；
// 若明显更短 → 驱动只是把数据拷进内部缓冲就返回（不能依赖它当节拍）。
static void pace_task(void *arg) {
    const int frames = (int)(intptr_t)arg;
    static int16_t silence[320];
    bsp_audio_set_format(SAMPLE_HZ, 16, 1);
    int64_t t_first_done = 0;
    const int64_t t0 = esp_timer_get_time();
    for (int i = 0; i < frames; i++) {
        bsp_audio_write(silence, sizeof silence);
        if (i == 0) t_first_done = esp_timer_get_time();
    }
    const int64_t t1 = esp_timer_get_time();
    const int total_ms = (int)((t1 - t0) / 1000);
    const int first_ms = (int)((t_first_done - t0) / 1000);
    const int nominal = frames * 20;
    ESP_LOGI(TAG, "写入节流测试: %d 帧（名义 %d ms）实际 %d ms，首帧 %d ms", frames, nominal,
             total_ms, first_ms);
    char ev[224];
    snprintf(ev, sizeof ev,
             "{\"event\":\"i2s_pace\",\"frames\":%d,\"nominal_ms\":%d,\"elapsed_ms\":%d,"
             "\"first_frame_ms\":%d,\"ratio_pct\":%d}\n",
             frames, nominal, total_ms, first_ms, nominal ? (total_ms * 100 / nominal) : 0);
    terminal_link_send_src(LINK_SRC_USB, ev);
    s_busy = false;
    vTaskDelete(NULL);
}

esp_err_t terminal_audio_pace_test(int frames) {
    if (s_busy) return ESP_ERR_INVALID_STATE;
    if (frames <= 0 || frames > 400) return ESP_ERR_INVALID_ARG;
    s_busy = true;
    if (xTaskCreate(pace_task, "dsh_pace", 4096, (void *)(intptr_t)frames, 4, NULL) != pdPASS) {
        s_busy = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t terminal_audio_tone_start(int ms, int hz) {
    if (s_busy) return ESP_ERR_INVALID_STATE;
    if (ms <= 0 || ms > 5000 || hz < 100 || hz > 8000) return ESP_ERR_INVALID_ARG;
    s_busy = true;
    if (xTaskCreate(tone_task, "dsh_tone", 4096, (void *)(intptr_t)ARG_PACK(ms, hz), 4, NULL) !=
        pdPASS) {
        s_busy = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}