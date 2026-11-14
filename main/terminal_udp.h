// main/terminal_udp.h — UDP 音频通道（W0-9，第 3 段）
//
// 为什么要有它：这条链路是移动网络，RTT 205 ms、峰值可到 900 ms。TCP 上
// 一次丢包要等重传，20 ms 的音频块等不起；判断过的数字在 terminal_adpcm.h
// 里（在途字节 = 码率 × RTT，ADPCM 8.4 KB/s 在 900 ms 下只要 7.5 KB）。
//
// 关键设计：设备永远主动打洞。先发 HELLO 数据报，服务端从**源地址**学到
// (ip, port) 再回一个 ack，所以不需要端口转发，服务端也不用配设备地址。
// 收不到 ack 就继续走 TCP —— UDP 只有在服务端证明"我听得见你"之后才启用，
// 这样即使服务端没开 UDP，采集也不会瞎丢。
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// 开始打洞。peer_ip 是主机字节序；port 与 TCP 音频通道同一个号。
// 幂等：重复调用只会更新对端地址。
bool terminal_udp_start(uint32_t peer_ip, uint16_t peer_port);
void terminal_udp_stop(void);

// 只有收到服务端 ack 之后才为 true。采集据此决定走 UDP 还是 TCP。
bool terminal_udp_ready(void);

// 发一块 ADPCM：payload = [u32 seq][164 B 块]，外面套 PAV1 16 字节头。
// 返回 true 表示已经交给内核（UDP 没有"对端收到"这回事）。
bool terminal_udp_send_adpcm(uint32_t turn, uint32_t seq, const uint8_t *blk, size_t n);

// JSON 片段，给 get_audio / vendor.udp_diag 看：ready/sent/fail/ack/peer。
void terminal_udp_diag(char *buf, size_t n);