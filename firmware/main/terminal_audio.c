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

#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *TAG = "audio";

#define SAMPLE_HZ 16000
#define FRAME_MS 20
#define FRAME_SAMPLES (SAMPLE_HZ / 1000 * FRAME_MS)  // 320
#define FRAME_BYTES (FRAME_SAMPLES * 2)              // 640
#define MAX_RECORD_MS 2000  // 再长就要 128 KB 连续堆，实测分配不出来（见 no mem 分支）
#define TASK_STACK 6144

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
    const int want_ms = (int)(intptr_t)arg;

    int rec_ms = want_ms;
    if (rec_ms < FRAME_MS) rec_ms = FRAME_MS;
    if (rec_ms > MAX_RECORD_MS) rec_ms = MAX_RECORD_MS;
    const int frames = rec_ms / FRAME_MS;
    const size_t total = (size_t)frames * FRAME_BYTES;

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
        const esp_err_t r = bsp_audio_read((uint8_t *)buf + (size_t)i * FRAME_BYTES, FRAME_BYTES);
        if (r == ESP_OK) {
            got_bytes += FRAME_BYTES;
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
        if (bsp_audio_write((uint8_t *)buf + (size_t)i * FRAME_BYTES, FRAME_BYTES) != ESP_OK) {
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
         "\"frame_ms\":%d,\"frames\":%d,\"bytes\":%u,\"rms\":%d,\"peak\":%d,\"dc\":%d,"
         "\"clips\":%d,\"nonzero\":%d,"
         "\"record_ms\":%d,\"play_ms\":%d,\"rate_ratio\":%d,\"read_fail\":%d,\"write_fail\":%d}\n",
         (unsigned)++s_seq, SAMPLE_HZ, FRAME_MS, frames, (unsigned)got_bytes, rms, peak, dc, clips,
         nonzero, rec_elapsed, play_elapsed, rec_ms ? (rec_elapsed * 100 / rec_ms) : 0, read_fail,
         write_fail);

    free(buf);
    s_busy = false;
    vTaskDelete(NULL);
}

esp_err_t terminal_audio_selftest_start(int ms) {
    if (s_busy) return ESP_ERR_INVALID_STATE;
    s_busy = true;
    if (xTaskCreate(selftest_task, "dsh_audio_test", TASK_STACK, (void *)(intptr_t)ms, 4, NULL) !=
        pdPASS) {
        s_busy = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

bool terminal_audio_busy(void) { return s_busy; }