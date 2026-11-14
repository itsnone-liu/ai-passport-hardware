#pragma once

#include <stdbool.h>

// 第 1 段：Opus 编码器冒烟测试。
// 异步：起一个 12 KB 栈的专用任务跑（libopus 不能在链路任务的小栈上跑），
// 跑完用 event=opus_bench 从控制通道回报。
bool terminal_opus_bench_start(void);