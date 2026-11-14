// main/terminal_proto.h — USB-Serial/JTAG 载体（M1 链路）。
#pragma once

#include "esp_err.h"
#include <stdbool.h>

// 安装 USB-Serial/JTAG 驱动、注册发送 sink、启动接收任务。驱动不可用时返回错误，
// 但其它载体（网络）与界面照常运行。
esp_err_t terminal_proto_start(void);

// USB 载体的接收任务是否已就绪。
bool terminal_proto_ready(void);