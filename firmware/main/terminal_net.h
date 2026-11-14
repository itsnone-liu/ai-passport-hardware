// main/terminal_net.h — Wi-Fi STA + 原生 TCP 载体（M3 链路）。
//
// 设备永远做客户端：先连 Wi-Fi，拿到 IP 后主动 connect 服务器。不监听任何端口，因此
// 不需要端口转发，也不存在入站攻击面。
//
// 网络参数默认取自 menuconfig，首次启动写入 NVS；之后可由 USB 控制通道用 set_net
// 现场修改，换 Wi-Fi/换服务器不必重刷固件。
#pragma once

#include "esp_err.h"
#include <stdbool.h>
#include <stddef.h>

esp_err_t terminal_net_start(void);

// TCP 是否已连上服务器（UI 用它决定显示 USB 还是 NET）。
bool terminal_net_ready(void);

// Wi-Fi 是否已连上并拿到 IP（服务器还不一定通）。
bool terminal_net_ip_ready(void);

// 更新并持久化网络参数，然后断开重连。任一参数传 NULL 表示保持原值；port <= 0 同理。
esp_err_t terminal_net_apply(const char *ssid, const char *password, const char *host, int port);

// 把当前配置写成 JSON 片段（不含密码），形如 "ssid":"..","host":"..","port":8080
void terminal_net_describe(char *buf, size_t n);

// 扫描周边 AP，写成 JSON 数组（最多 12 个，按信号排序）。成功返回 ESP_OK。
// 注意：扫描会短暂断开当前连接，之后由重连逻辑自动恢复。
esp_err_t terminal_net_scan(char *buf, size_t n);