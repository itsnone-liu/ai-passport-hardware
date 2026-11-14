// main/terminal_link.h — 传输无关的协议核心。
//
// 一条消息 = 一行 JSON。USB CDC 与 WebSocket 都只是字节/文本流的"载体"，各自注册一个
// sink 把发出去的字节写走；收到的行一律交给 terminal_link_dispatch() 处理。这样 M3 换
// 传输不需要改命令分发逻辑。
#pragma once

#include <stddef.h>

// sink 负责把一行（含结尾 '\n'）写到自己的载体上。必须可重入调用、不阻塞过久。
typedef void (*link_sink_t)(const char *line, size_t len);

// 请求来源。协议 §6.2：应答只回原连接，事件才广播。
// 来源还决定权限：provision 只允许 USB，网络来的请求一律拒绝。
typedef enum {
    LINK_SRC_UNKNOWN = 0,
    LINK_SRC_USB,  // USB-Serial/JTAG（开发/救援/诊断通道）
    LINK_SRC_TCP,  // 主动拨出的网络会话
} link_src_t;

typedef struct {
    link_src_t src;
    link_sink_t sink;  // 应答回这条路（不能为 NULL）
} link_reply_t;

void terminal_link_init(void);
void terminal_link_register(link_sink_t sink);

// 广播给所有已注册的载体：只用于 ready/key/error 这类事件。
void terminal_link_send(const char *line);

// 解析并执行一行（调用方负责去掉行尾）。应答只回 reply->sink，不广播——USB 与 TCP
// 同时在线时广播会让两边都收到对方的应答，id 还会撞车。
// 解析失败会回 {"ok":false,"err":"bad_json"}（同样只回原连接）。
void terminal_link_dispatch(char *line, const link_reply_t *reply);

// 按键事件上报。
void terminal_link_emit_key(int btn_index, int ev_index);