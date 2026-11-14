// IMA/DVI ADPCM 分块编码（上行用）。
//
// 为什么不是 Opus：实测 libopus 的编码器状态 opus_encoder_get_size() = 24,716 B，
// 且必须是一块连续堆，再加上编码任务 12 KB 栈，一共要 ~37 KB 连续 RAM；本机最大
// 连续块只有 28.7 KB，opus_encoder_create 直接返回 OPUS_ALLOC_FAIL(-7)。
// 要腾出这个量就得把放音环从 640 ms 再砍到 400 ms 以下，那是拿已经会断音的下行
// 去换上行压缩，不划算。ADPCM 4:1 后上行只要 8.2 KB/s，200 ms RTT 下在途 1.6 KB，
// 而链路实测能跑 18.7 KB/s，余量 2 倍以上——问题解决，代价是编码器状态 16 字节。
//
// 分块而不是裸流：每 20 ms（320 采样）自带 4 字节块头（预测值 + 步长索引），
// 任何一块都能独立解码。这样第 3 段走 UDP 丢包时，坏一块只烂 20 ms，
// 不会像裸流那样从此全体失步。
#pragma once

#include <stdint.h>

#define ADPCM_SAMPLE_HZ 16000
#define ADPCM_FRAME_MS 20
#define ADPCM_SAMPLES (ADPCM_SAMPLE_HZ / 1000 * ADPCM_FRAME_MS)   // 320
#define ADPCM_NIBBLE_BYTES (ADPCM_SAMPLES / 2)                    // 160
#define ADPCM_BLOCK_BYTES (4 + ADPCM_NIBBLE_BYTES)                // 164

typedef struct {
    int32_t predicted;
    int32_t step_index;
} adpcm_enc_t;

void adpcm_enc_reset(adpcm_enc_t *s);

// 编一块：in 是 ADPCM_SAMPLES 个 int16，out 至少 ADPCM_BLOCK_BYTES 字节。
// 块头：[0..1] 预测值(int16 LE) [2] 步长索引 [3] 保留(0)。
void adpcm_enc_block(adpcm_enc_t *s, const int16_t *in, uint8_t *out);

// 解码一块（设备侧用不到，但单测和服务端对齐语义时要靠它自证）。
void adpcm_dec_block(const uint8_t *in, int16_t *out);