// main/terminal_capture.h — 上行采集与轮次生命周期（协议 §6.3 / §6.4）。
//
// 设备是上行的时钟源：分帧节拍来自 I2S 真实时钟，不用定时器凑数（§4）。
// 按键先本机起采填 pre-roll，等 capture.start 到了立刻补发再转实时。
#pragma once

#include "esp_err.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

void terminal_capture_init(void);

// capture.start：同步校验后立即返回（busy = 已有活动轮次），补发 pre-roll 与实时
// 送帧由采集任务完成。音频通道不可用时返回 ESP_ERR_NOT_SUPPORTED。
esp_err_t terminal_capture_start(uint32_t turn_id);
esp_err_t terminal_capture_stop(uint32_t turn_id);

// 本机按键触发：开始填 pre-roll 并等 capture.start，超时按 cap_start_timeout 放弃。
void terminal_capture_arm(void);

// true = 有轮次正在进行（armed 或 capturing）。scan 之类需要断开 Wi-Fi 的操作靠它避让。
bool terminal_capture_busy(void);

// true = 正在为某一轮送实时帧（不含 armed）。
bool terminal_capture_capturing(void);

// 从其它任务强制收尾（采集任务自己阻塞时，只有还活着的人能救场）。
void terminal_capture_force_stop(const char *reason);

// get_audio 用：state/ring_ms/underruns/overflows 等排障字段（JSON 片段，不含引号外壳）。
void terminal_capture_get_audio(char *buf, size_t n);

// get_status 用。
void terminal_capture_describe(char *buf, size_t n);