// main/terminal_boot.h —— 复位取证：启动计数、复位原因、上次运行的最小空闲堆。
#pragma once

#include <stdint.h>

// app_main 最开始调用一次：读 esp_reset_reason()，并把上一次运行的统计从 RTC 内存取出来。
void terminal_boot_record(void);

uint32_t terminal_boot_count(void);          // 本次是第几次启动（掉电后重新从 1 开始）
const char *terminal_boot_reason(void);      // poweron/ext_pin/software/panic/task_wdt/...
uint32_t terminal_boot_prev_heap_min(void);  // 上次运行结束前的最小空闲堆
uint32_t terminal_boot_prev_heap_block(void);// 上次运行结束前的最大空闲块
uint32_t terminal_boot_prev_uptime_s(void);  // 上次运行活了多久
int terminal_boot_reason_code(void);         // esp_reset_reason() 原始值，便于分辨未知枚举
