// main/terminal_auth.h — 控制通道握手与设备密钥（协议 §3.1/§3.2）。
//
// 密钥只在 USB 侧写入（provision），落在独立命名空间 pavauth，任何日志、get_net、
// 串口输出都不得回显。握手本身不加密链路：它解决的是"谁在发命令"，不是"谁在偷听"。
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct cJSON;

// 读出已 provision 的密钥、缓存 device_id。必须在 Wi-Fi 起来之前也能安全调用。
void terminal_auth_init(void);

bool terminal_auth_has_secret(void);
const char *terminal_auth_device_id(void);   // "psp-" + MAC 后 6 hex
const char *terminal_auth_session_id(void);   // 本会话服务端分配的 16 hex，未分配为 ""
int terminal_auth_audio_port(void);           // hello 应答里的 audio_port，未知为 0

// USB provision：secret 支持两种写法——纯十六进制（偶数长度，解码成字节）或原文
// （按 UTF-8 字节直接用）。*enc 回填实际采用的解释方式，便于对端核对。
// 成功返回密钥字节数，失败返回 -1。
int terminal_auth_provision(const char *secret, const char *device_id_override,
                            const char **enc);

// USB 救援：清掉已 provision 的密钥（回到兼容模式）。仅 USB 通道可用。
bool terminal_auth_clear(void);

// 音频通道用：新的随机 nonce（bytes 字节 → bytes*2 字符小写 hex）。
void terminal_auth_random_hex(char *out, size_t bytes);

// 音频通道的 audio_mac = HMAC-SHA256(secret, "PAV1AUTH|audio|<device_id>|<dev>|<srv>")。
// 密钥不离开本文件；调用方只拿到十六进制摘要。
bool terminal_auth_audio_mac(const char *device_nonce, const char *server_nonce, char out[65]);

// UDP 逐包 MAC：对任意字节算 HMAC-SHA256，调用方取前 N 字节截断用。
// UDP 没有握手，而设备号是公开的（`psp-xxxxxx` 会出现在日志和仓库里），
// 所以"这一包确实出自知道密钥的人"只能靠这个摘要来证明。
bool terminal_auth_raw_hmac(const void *data, size_t n, uint8_t out[32]);

// 开始一次会话握手：生成 device_nonce 并把 hello 行写进 out。
bool terminal_auth_begin(char *out, size_t cap);

typedef enum {
    AUTH_ACT_NONE = 0,  // 不是握手应答，交给普通命令分发
    AUTH_ACT_SEND,      // out 里是要发出的下一行（auth 请求）
    AUTH_ACT_READY,     // 会话可用了（认证通过，或未 provision 的兼容模式）
    AUTH_ACT_FAILED,    // 认证失败，本次会话作废（服务端随后会关连接）
} auth_act_t;

auth_act_t terminal_auth_on_reply(const struct cJSON *root, char *out, size_t cap);

// 本会话是否已可执行业务命令。USB 不受此限。
bool terminal_auth_authorized(void);
bool terminal_auth_handshaking(void);
bool terminal_auth_failed(void);

// 握手总时限 5 s（§3.2）。由 net_task 周期调用。
// 返回值：NONE 继续；READY 会话可用（认证通过 / 退化兼容）；FAILED 作废本次会话。
auth_act_t terminal_auth_tick_step(void);

// 连接断开时清会话状态（session_id 作废）。
void terminal_auth_reset(void);