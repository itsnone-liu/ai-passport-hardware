// main/terminal_playback.h — 下行播放：环形缓冲 + I2S 输出（协议 §6.3 / §6.4）。
//
// 服务端是下行时钟源，设备只负责把到的帧按时喂给 codec：
// 缓冲不够就说不够（underrun），不假装播完；满了就丢帧计数（overflow），不撑爆内存。
#pragma once

#include "esp_err.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

void terminal_playback_init(void);

// playback.start：同步校验（busy = 已有一轮在播）后返回，预缓冲 300 ms 才开始出声。
esp_err_t terminal_playback_start(uint32_t turn_id);

// playback.stop：打断当前轮，清空缓冲并静音。
esp_err_t terminal_playback_stop(uint32_t turn_id);

// pav1 收到 PCM / END 帧时调用。返回 false 表示该帧被丢弃（非当前轮或缓冲满）。
bool terminal_playback_push(uint32_t turn_id, const void *pcm, size_t n);
void terminal_playback_mark_end(uint32_t turn_id);

// get_audio 用的排障字段。
void terminal_playback_describe(char *buf, size_t n);

// 厂商自测：按服务端节奏把正弦推进播放环，走真环/真任务/真 I2S。
esp_err_t terminal_playback_selftest(int ms);