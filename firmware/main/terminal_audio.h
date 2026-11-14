// main/terminal_audio.h — 设备侧音频自测（麦克风 → 内存 → 扬声器）。
//
// 这一步只证明样机自己能采、能播、能把 20 ms 帧按时喂进编解码器，不涉及任何
// 网络协议。协议定下来之后，同样的读写原语直接复用到收发路径上。
#pragma once

#include "esp_err.h"
#include <stdbool.h>

// 起一个后台任务：录 ms 毫秒（16 kHz/单声道/s16le）后原样回放，结束发
// {"event":"audio_selftest",...}。非阻塞，成功后立刻返回；正在跑时返回
// ESP_ERR_INVALID_STATE（同一时刻只允许一个自测，避免两个任务抢同一个 codec）。
esp_err_t terminal_audio_selftest_start(int ms);

// 是否有自测正在跑。
bool terminal_audio_busy(void);