// main/terminal_link.h — 传输无关的协议核心。
//
// 一条消息 = 一行 JSON。USB CDC 与 WebSocket 都只是字节/文本流的"载体"，各自注册一个
// sink 把发出去的字节写走；收到的行一律交给 terminal_link_dispatch() 处理。这样 M3 换
// 传输不需要改命令分发逻辑。
#pragma once

#include <stddef.h>

// sink 负责把一行（含结尾 '\n'）写到自己的载体上。必须可重入调用、不阻塞过久。
typedef void (*link_sink_t)(const char *line, size_t len);

void terminal_link_init(void);
void terminal_link_register(link_sink_t sink);

// 广播给所有已注册的载体。
void terminal_link_send(const char *line);

// 解析并执行一行（调用方负责去掉行尾）。解析失败会回 {"ok":false,...}。
void terminal_link_dispatch(char *line);

// 按键事件上报。
void terminal_link_emit_key(int btn_index, int ev_index);