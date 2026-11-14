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

// 只把 NVS 里的网络参数读进内存（初始化 NVS，不碰 Wi-Fi）。必须在 USB 通道开始受理
// 命令之前调用，否则开机瞬间的 get_net 会答出一串空字段。
void terminal_net_preload(void);

// TCP 是否已连上服务器（UI 用它决定显示 USB 还是 NET）。
bool terminal_net_ready(void);

// Wi-Fi 是否已连上并拿到 IP（服务器还不一定通）。
bool terminal_net_ip_ready(void);

// 更新并持久化网络参数，然后断开重连。任一参数传 NULL 表示保持原值；port <= 0 同理。
esp_err_t terminal_net_apply(const char *ssid, const char *password, const char *host, int port);

// 只更新并持久化参数，不动现有链路；配合 terminal_net_reconnect() 使用。
// 这样调用方可以先发回执、再断链，避免回执排在断开之后永远送不出去。
esp_err_t terminal_net_set(const char *ssid, const char *password, const char *host, int port);

// 按当前参数断开并重新连接（Wi-Fi 未初始化时为空操作）。
void terminal_net_reconnect(void);

// 让 net_task 主动弃掉当前会话（认证失败/被拒次数超限）。不在调用方上下文里关 fd。
void terminal_net_kill(const char *why);

// 把当前配置写成 JSON 片段（不含密码），形如 "ssid":"..","host":"..","port":8080
void terminal_net_describe(char *buf, size_t n);

// 音频通道要用同一台服务器，所以这两个只读 getter 必须存在（端口由 hello 应答另给）。
const char *terminal_net_host(void);
int terminal_net_port(void);

// 扫描周边 AP，写成 JSON 数组（最多 12 个，按信号排序）。成功返回 ESP_OK。
// 注意：扫描会短暂断开当前连接，之后由重连逻辑自动恢复。
esp_err_t terminal_net_scan(char *buf, size_t n);