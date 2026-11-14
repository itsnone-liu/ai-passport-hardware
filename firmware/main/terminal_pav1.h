// main/terminal_pav1.h — PAV1 音频通道：16 字节帧头 + 严格解析器 + 通道客户端。
//
// 帧格式见协议 §5，解析规则见 §5.1。两条硬约束：只认精确字节数（绝不扫描重同步），
// 解析器同一时刻只持有一个未完成帧（无 PSRAM，宁可报错也不预分配）。
#pragma once

#include "esp_err.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PAV1_HDR_LEN 16
#define PAV1_MAX_PAYLOAD 4096
#define PAV1_FRAME_BYTES 640  // 20 ms @ 16 kHz mono s16

typedef enum {
    PAV1_K_PCM = 1,
    PAV1_K_HELLO = 2,
    PAV1_K_END = 3,
    PAV1_K_AUTH = 4,
} pav1_kind_t;

// 错误码字符串与 §6.5 逐字一致，直接进 error 事件。
typedef enum {
    PAV1_E_NONE = 0,
    PAV1_E_BAD_MAGIC,
    PAV1_E_OVERSIZE,
    PAV1_E_UNKNOWN_KIND,
    PAV1_E_BAD_FRAME,
    PAV1_E_INCOMPLETE_FRAME,
} pav1_err_t;

const char *pav1_err_str(pav1_err_t e);

// 组一帧，返回写入字节数（16 + n）。调用方保证 out 至少 16 + n 字节。
size_t pav1_encode(uint8_t *out, pav1_kind_t kind, uint32_t turn_id, const void *payload,
                   uint32_t n);

typedef struct pav1_parser pav1_parser_t;
typedef void (*pav1_frame_cb)(void *ctx, pav1_kind_t kind, uint32_t turn_id,
                              const uint8_t *payload, size_t n);

pav1_parser_t *pav1_parser_new(void);
void pav1_parser_free(pav1_parser_t *p);
// 0 = 需要更多字节；非 0 = 致命错误，解析器已复位，调用方必须关通道。
pav1_err_t pav1_parser_feed(pav1_parser_t *p, const uint8_t *data, size_t n, pav1_frame_cb cb,
                            void *ctx);
// 是否还有半截帧（连接关闭时用它判 INCOMPLETE_FRAME）。
bool pav1_parser_pending(const pav1_parser_t *p);

// 只建任务与缓冲，不连网（由 start 触发）。
void terminal_pav1_init(void);

// 控制通道认证通过后立即打开音频通道（§3.3：不在按键时再连）。
void terminal_pav1_start(const char *host, int port);
// 控制通道断开 = 会话作废，音频通道必须一起关（session_id 不再有效）。
void terminal_pav1_stop(void);
bool terminal_pav1_ready(void);

// 音频通道累计收发计数（get_audio 排障用）。
uint32_t terminal_pav1_rx_frames(void);
uint32_t terminal_pav1_rx_end(void);
uint32_t terminal_pav1_tx_frames(void);

// 隔离实验：ms 毫秒内只读音频 socket 并计数（不推环、不播放）。
// 同步返回帧数/字节数/实际耗时，调用方拿到的就是本次窗口的统计，无需再做差分。
esp_err_t terminal_pav1_rx_rate(int ms, uint32_t *frames, uint32_t *bytes, int *elapsed_ms);

// 音频通道累计收发计数（get_audio 排障用）：
// rx = 从服务端收到的 PCM 帧数（不含 END），tx = 已发出的 PCM 帧数。
uint32_t terminal_pav1_rx_frames(void);
uint32_t terminal_pav1_rx_end(void);
uint32_t terminal_pav1_tx_frames(void);

// 正在建立/重建音频通道（连接中，尚不可送帧）。
// 与 ready 区分开：服务端据此知道"等一会儿再试"而不是"设备不支持"。
bool terminal_pav1_starting(void);

// 上行一帧 PCM（turn_id 由调用方给）。返回实际发出的字节数，0 = 通道不可用。
int terminal_pav1_send_pcm(uint32_t turn_id, const void *pcm, size_t n);

// 非阻塞版本：>0 已送出字节数；0 此刻写不进去（可重试）；-1 通道不可用/错误。
// 采集任务用这个：写不进去就把帧留在自己那个有界环里，下一拍再试。
int terminal_pav1_send_pcm_nowait(uint32_t turn_id, const void *pcm, size_t n);

// 上行 END 帧（零载荷），标志该轮上传结束（§5）。
bool terminal_pav1_send_end(uint32_t turn_id);

// 上行自测：由通道任务发若干帧 1 kHz 正弦，用于验证对端帧边界是否正确。
bool terminal_pav1_request_uplink_test(uint32_t frames, uint32_t tone_hz);

// 给 get_status 用的运行计数（不含密钥、不含 PCM 内容）。
void terminal_pav1_describe(char *buf, size_t n);