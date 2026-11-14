// Opus 编码器冒烟测试（第 1 段）。
// 目的只有一个：在真机上把"能不能扛"量出来，而不是推出来。
// 判据（docs/next-plan-opus-udp.md）：常驻堆 >= 10 KB、单帧编码 < 5 ms、200 帧无泄漏。
//
// 两个实测教训，都写在这里免得下次再踩：
// 1) 不能在 USB 链路任务里跑编码——libopus 的 CELT/SILK 路径要几 KB 栈，
//    链路任务的栈只有几 KB，实测直接 "Guru Meditation ... Stack protection fault"。
//    所以固定开一个 12 KB 栈的专用任务；第 2 段正式接入时也是这个任务在跑。
// 2) 编码器状态 opus_encoder_get_size() = 24,716 B（16 kHz 单声道），
//    必须是一块**连续**的堆。堆够但最大连续块不够时，opus_encoder_create 一样失败。
#include "terminal_opus.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "opus.h"
#include "terminal_link.h"

static const char *TAG = "opus";

#define BENCH_SR 16000
#define BENCH_CH 1
#define BENCH_FRAME_MS 60
#define BENCH_SAMPLES (BENCH_SR / 1000 * BENCH_FRAME_MS)   // 960
#define BENCH_BITRATE 24000
#define BENCH_FRAMES 200
#define BENCH_TASK_STACK 12288
#define BENCH_TASK_PRIO 5

static int16_t s_pcm[BENCH_SAMPLES];
static uint8_t s_out[1500];
static volatile bool s_running;

// 合成一段"像人声"的激励：基频 180 Hz + 谐波 + 一点噪声。
// 纯正弦会被 Opus 的 VAD/DTX 判成静音，测出来的帧长和耗时都不具代表性。
static void fill_pcm(int frame) {
    static uint32_t seed = 12345;
    for (int i = 0; i < BENCH_SAMPLES; i++) {
        const double t = (double)(frame * BENCH_SAMPLES + i) / BENCH_SR;
        double v = 0.0;
        for (int h = 1; h <= 6; h++) {
            v += sin(2.0 * M_PI * 180.0 * h * t) / (double)h;
        }
        v += 0.6 * sin(2.0 * M_PI * 900.0 * t);
        seed = seed * 1103515245u + 12345u;
        v += 0.15 * (((int32_t)(seed >> 16) & 0x7fff) / 16384.0 - 1.0);
        double s = v * 2500.0;
        if (s > 30000) s = 30000;
        if (s < -30000) s = -30000;
        s_pcm[i] = (int16_t)s;
    }
}

static void bench_task(void *arg) {
    (void)arg;
    char rep[640];
    const size_t heap0 = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    const int state_bytes = opus_encoder_get_size(BENCH_CH);

    int err = 0;
    OpusEncoder *enc = opus_encoder_create(BENCH_SR, BENCH_CH, OPUS_APPLICATION_VOIP, &err);
    if (!enc || err != OPUS_OK) {
        snprintf(rep, sizeof rep,
                 "\"ok\":false,\"err\":\"create\",\"opus_err\":%d,\"state_bytes\":%d,"
                 "\"heap_before\":%u,\"heap_block\":%u",
                 err, state_bytes, (unsigned)heap0,
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
        ESP_LOGE(TAG, "编码器创建失败 err=%d（状态 %d B）", err, state_bytes);
        goto report;
    }
    const size_t heap1 = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);

    opus_encoder_ctl(enc, OPUS_SET_BITRATE(BENCH_BITRATE));
    opus_encoder_ctl(enc, OPUS_SET_COMPLEXITY(0));
    opus_encoder_ctl(enc, OPUS_SET_VBR(1));
    opus_encoder_ctl(enc, OPUS_SET_DTX(1));
    opus_encoder_ctl(enc, OPUS_SET_INBAND_FEC(0));
    opus_encoder_ctl(enc, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
    {
        int64_t sum_us = 0, max_us = 0, bytes = 0;
        int min_bytes = 1 << 20, max_bytes = 0, fails = 0;
        uint32_t heap_low = (uint32_t)heap1;
        for (int f = 0; f < BENCH_FRAMES; f++) {
            fill_pcm(f);
            const int64_t t0 = esp_timer_get_time();
            const int n = opus_encode(enc, s_pcm, BENCH_SAMPLES, s_out, (opus_int32)sizeof s_out);
            const int64_t dt = esp_timer_get_time() - t0;
            if (n < 0) {
                fails++;
                continue;
            }
            sum_us += dt;
            if (dt > max_us) max_us = dt;
            bytes += n;
            if (n < min_bytes) min_bytes = n;
            if (n > max_bytes) max_bytes = n;
            if ((f & 15) == 15) {
                const uint32_t h = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
                if (h < heap_low) heap_low = h;
            }
        }
        const size_t heap2 = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        opus_encoder_destroy(enc);
        const size_t heap3 = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        const int okn = BENCH_FRAMES - fails;
        snprintf(rep, sizeof rep,
                 "\"ok\":true,\"frame_ms\":%d,\"frames\":%d,\"fails\":%d,"
                 "\"state_bytes\":%d,\"resident\":%u,\"heap_before\":%u,\"heap_low\":%u,"
                 "\"heap_after\":%u,\"heap_freed\":%u,\"avg_us\":%d,\"max_us\":%d,"
                 "\"avg_bytes\":%d,\"min_bytes\":%d,\"max_bytes\":%d,\"kbps\":%d",
                 BENCH_FRAME_MS, BENCH_FRAMES, fails, state_bytes, (unsigned)(heap0 - heap1),
                 (unsigned)heap0, (unsigned)heap_low, (unsigned)heap2, (unsigned)(heap2 - heap3),
                 okn ? (int)(sum_us / okn) : -1, (int)max_us, okn ? (int)(bytes / okn) : -1,
                 min_bytes < (1 << 20) ? min_bytes : -1, max_bytes,
                 okn ? (int)((bytes * 8) / (okn * BENCH_FRAME_MS)) : -1);
        ESP_LOGI(TAG, "冒烟完成：%d 帧，均 %d us，峰 %d us，均长 %d B，最低堆 %u", BENCH_FRAMES,
                 okn ? (int)(sum_us / okn) : -1, (int)max_us, okn ? (int)(bytes / okn) : -1,
                 (unsigned)heap_low);
    }

report: {
    char ev[768];
    snprintf(ev, sizeof ev, "{\"event\":\"opus_bench\",%s}\n", rep);
    terminal_link_send_src(LINK_SRC_TCP, ev);
}
    s_running = false;
    vTaskDelete(NULL);
}

bool terminal_opus_bench_start(void) {
    if (s_running) return false;
    s_running = true;
    if (xTaskCreate(bench_task, "opus_bench", BENCH_TASK_STACK, NULL, BENCH_TASK_PRIO, NULL) !=
        pdPASS) {
        s_running = false;
        return false;
    }
    return true;
}