#include "terminal_adpcm.h"

#include <string.h>

// IMA ADPCM 标准步长表与索引调整表（IMA/DVI 规范附录）。
static const int16_t k_step[89] = {
    7,     8,     9,     10,    11,    12,    13,    14,    16,    17,    19,    21,    23,
    25,    28,    31,    34,    37,    41,    45,    50,    55,    60,    66,    73,    80,
    88,    97,    107,   118,   130,   143,   157,   173,   190,   209,   230,   253,   279,
    307,   337,   371,   408,   449,   494,   544,   598,   658,   724,   796,   876,   963,
    1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,  2272,  2499,  2749,  3024,  3327,
    3660,  4026,  4428,  4871,  5358,  5894,  6484,  7132,  7845,  8630,  9493,  10442, 11487,
    12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767};

static const int8_t k_index[16] = {-1, -1, -1, -1, 2, 4, 6, 8,
                                   -1, -1, -1, -1, 2, 4, 6, 8};

static inline int32_t clamp16(int32_t v) {
    if (v > 32767) return 32767;
    if (v < -32768) return -32768;
    return v;
}

void adpcm_enc_reset(adpcm_enc_t *s) {
    s->predicted = 0;
    s->step_index = 0;
}

void adpcm_enc_block(adpcm_enc_t *s, const int16_t *in, uint8_t *out) {
    // 块头写的是"这一块开始时的解码器状态"，解码端据此独立起步。
    out[0] = (uint8_t)(s->predicted & 0xff);
    out[1] = (uint8_t)((s->predicted >> 8) & 0xff);
    out[2] = (uint8_t)s->step_index;
    out[3] = 0;

    uint8_t *q = out + 4;
    for (int i = 0; i < ADPCM_SAMPLES; i += 2) {
        uint8_t byte = 0;
        for (int k = 0; k < 2; k++) {
            int32_t step = k_step[s->step_index];
            int32_t diff = (int32_t)in[i + k] - s->predicted;
            uint8_t nib = 0;
            if (diff < 0) {
                nib = 8;
                diff = -diff;
            }
            int32_t vpdiff = step >> 3;
            if (diff >= step) {
                nib |= 4;
                diff -= step;
                vpdiff += step;
            }
            step >>= 1;
            if (diff >= step) {
                nib |= 2;
                diff -= step;
                vpdiff += step;
            }
            step >>= 1;
            if (diff >= step) {
                nib |= 1;
                vpdiff += step;
            }
            s->predicted = clamp16((nib & 8) ? s->predicted - vpdiff : s->predicted + vpdiff);
            s->step_index += k_index[nib];
            if (s->step_index < 0) s->step_index = 0;
            if (s->step_index > 88) s->step_index = 88;
            byte |= (uint8_t)((nib & 0x0f) << (4 * k));
        }
        *q++ = byte;
    }
}

void adpcm_dec_block(const uint8_t *in, int16_t *out) {
    int32_t predicted = (int16_t)((uint16_t)in[0] | ((uint16_t)in[1] << 8));
    int32_t step_index = in[2] < 89 ? in[2] : 88;
    const uint8_t *q = in + 4;
    for (int i = 0; i < ADPCM_SAMPLES; i += 2) {
        const uint8_t byte = *q++;
        for (int k = 0; k < 2; k++) {
            const uint8_t nib = (uint8_t)((byte >> (4 * k)) & 0x0f);
            int32_t step = k_step[step_index];
            int32_t vpdiff = step >> 3;
            if (nib & 4) vpdiff += step;
            if (nib & 2) vpdiff += step >> 1;
            if (nib & 1) vpdiff += step >> 2;
            predicted = clamp16((nib & 8) ? predicted - vpdiff : predicted + vpdiff);
            step_index += k_index[nib];
            if (step_index < 0) step_index = 0;
            if (step_index > 88) step_index = 88;
            out[i + k] = (int16_t)predicted;
        }
    }
}