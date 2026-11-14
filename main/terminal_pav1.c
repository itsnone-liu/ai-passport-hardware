// main/terminal_pav1.c — PAV1 帧编解码 + 音频通道客户端（协议 §3.3 / §5）。
#include "terminal_pav1.h"

#include "terminal_auth.h"
#include "terminal_capture.h"
#include "terminal_link.h"
#include "terminal_playback.h"

#include "cJSON.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *TAG = "pav1";

#define HANDSHAKE_TIMEOUT_MS 3000
#define RECV_TIMEOUT_S 1  // 短一点：空闲时也要能及时响应"该发帧了"这类本地请求
#define IDLE_DROP_S 180
#define RX_BUF 4096  // 一次 recv 尽量多取：1024 B 只能装 1.6 帧，收得再快也补不回窗口
#define UPLINK_STALL_MS 2000  // 连续这么久没有一帧送出去 = 对端不读，收轮并重建通道

static const uint8_t k_magic[4] = {'P', 'A', 'V', '1'};

static uint32_t rd32(const uint8_t *b) {
    return (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
}

static void wr32(uint8_t *b, uint32_t v) {
    b[0] = (uint8_t)(v & 0xff);
    b[1] = (uint8_t)((v >> 8) & 0xff);
    b[2] = (uint8_t)((v >> 16) & 0xff);
    b[3] = (uint8_t)((v >> 24) & 0xff);
}

const char *pav1_err_str(pav1_err_t e) {
    switch (e) {
        case PAV1_E_NONE: return "none";
        case PAV1_E_BAD_MAGIC: return "bad_magic";
        case PAV1_E_OVERSIZE: return "oversize";
        case PAV1_E_UNKNOWN_KIND: return "unknown_kind";
        case PAV1_E_BAD_FRAME: return "bad_frame";
        case PAV1_E_INCOMPLETE_FRAME: return "incomplete_frame";
        default: return "?";
    }
}

size_t pav1_encode(uint8_t *out, pav1_kind_t kind, uint32_t turn_id, const void *payload,
                   uint32_t n) {
    memcpy(out, k_magic, 4);
    out[4] = (uint8_t)kind;
    out[5] = 0;  // flags：v1 全 0
    out[6] = 0;  // reserved
    out[7] = 0;
    wr32(out + 8, turn_id);
    wr32(out + 12, n);
    if (n > 0 && payload) memcpy(out + PAV1_HDR_LEN, payload, n);
    return PAV1_HDR_LEN + n;
}

// ---- 解析器 ----------------------------------------------------------------

struct pav1_parser {
    uint8_t hdr[PAV1_HDR_LEN];
    size_t hdr_have;
    uint8_t *payload;  // 4096，堆上分配；不允许"先按 payload_len 再分配"
    size_t payload_have;
    uint32_t need;
    pav1_kind_t kind;
    uint32_t turn_id;
};

pav1_parser_t *pav1_parser_new(void) {
    pav1_parser_t *p = calloc(1, sizeof *p);
    if (!p) return NULL;
    p->payload = malloc(PAV1_MAX_PAYLOAD);
    if (!p->payload) {
        free(p);
        return NULL;
    }
    return p;
}

void pav1_parser_free(pav1_parser_t *p) {
    if (!p) return;
    free(p->payload);
    free(p);
}

bool pav1_parser_pending(const pav1_parser_t *p) {
    return p && (p->hdr_have > 0 || p->payload_have > 0);
}

static void parser_reset(pav1_parser_t *p) {
    p->hdr_have = 0;
    p->payload_have = 0;
    p->need = 0;
}

pav1_err_t pav1_parser_feed(pav1_parser_t *p, const uint8_t *data, size_t n, pav1_frame_cb cb,
                            void *ctx) {
    if (!p || !data) return PAV1_E_NONE;
    size_t i = 0;
    while (i < n) {
        if (p->hdr_have < PAV1_HDR_LEN) {
            p->hdr[p->hdr_have++] = data[i++];
            if (p->hdr_have < PAV1_HDR_LEN) continue;

            // §5.1：magic 不对就关通道，绝不扫描重同步——半帧会被当成帧头。
            if (memcmp(p->hdr, k_magic, 4) != 0) {
                parser_reset(p);
                return PAV1_E_BAD_MAGIC;
            }
            const uint32_t len = rd32(p->hdr + 12);
            const uint32_t turn = rd32(p->hdr + 8);
            const uint8_t kind = p->hdr[4];
            if (len > PAV1_MAX_PAYLOAD) {
                parser_reset(p);
                return PAV1_E_OVERSIZE;
            }
            if (kind < PAV1_K_PCM || kind > PAV1_K_AUTH) {
                parser_reset(p);
                return PAV1_E_UNKNOWN_KIND;
            }
            if (kind == PAV1_K_PCM && len == 0) {
                parser_reset(p);
                return PAV1_E_BAD_FRAME;
            }
            if (kind == PAV1_K_END && len != 0) {
                parser_reset(p);
                return PAV1_E_BAD_FRAME;
            }
            p->kind = (pav1_kind_t)kind;
            p->turn_id = turn;
            p->need = len;
            p->payload_have = 0;
            if (len == 0) {
                if (cb) cb(ctx, p->kind, p->turn_id, p->payload, 0);
                parser_reset(p);
            }
            continue;
        }

        const size_t want = p->need - p->payload_have;
        const size_t take = (n - i < want) ? (n - i) : want;
        memcpy(p->payload + p->payload_have, data + i, take);
        p->payload_have += take;
        i += take;
        if (p->payload_have == p->need) {
            if (cb) cb(ctx, p->kind, p->turn_id, p->payload, p->need);
            parser_reset(p);
        }
    }
    return PAV1_E_NONE;
}

// ---- 通道 ------------------------------------------------------------------

typedef struct {
    bool got;
    pav1_kind_t kind;
    uint32_t turn_id;
    const uint8_t *payload;  // 指向解析器内部缓冲，只在本次 recv 处理期间有效
    size_t n;
} rx_frame_t;

static int s_sock = -1;
static volatile bool s_abort_close;  // 下次 close_sock 用 RST 而不是 FIN（上行已死时）
static SemaphoreHandle_t s_tx_mu;
static TaskHandle_t s_task;
static SemaphoreHandle_t s_wake;  // 单值信号量：start() 叫醒任务
static pav1_parser_t *s_parser;
static volatile bool s_should_run;
static volatile bool s_ready;
static char s_host[64];
static int s_port;
static uint32_t s_rx_frames, s_rx_bytes, s_rx_end, s_tx_frames, s_tx_bytes;
static volatile bool s_rx_only;  // 隔离实验期间：只收不播
static volatile uint32_t s_rx_bad;
static pav1_err_t s_last_err;
static volatile bool s_ul_req;
static uint32_t s_ul_frames, s_ul_tone;
static volatile int s_last_sock_errno;

static bool send_all(const void *buf, size_t n) {
    const uint8_t *p = buf;
    while (n > 0) {
        const int w = send(s_sock, p, n, 0);
        if (w <= 0) {
            // 记下 errno：ENOBUFS（pbuf 用光）和 EAGAIN/ETIMEDOUT（窗口关/对端不读）
            // 是两种完全不同的病因，混在一起就只能靠猜。
            s_last_sock_errno = errno;
            return false;
        }
        p += w;
        n -= (size_t)w;
    }
    return true;
}

// 有界写：budget_ms=0 时纯非阻塞（立刻返回"此刻写不进去"），>0 时最多等这么久。
// 返回 1 = 写完全部；0 = 此刻写不进去（可重试）；-1 = 真错误。
// 存在的理由：原来的 send_all 让采集任务以 250 ms 超时阻塞在写上，
// 对端不读时 finish_turn 会卡 3.5 秒、期间堆只进不出，整轮机制被一条 socket 拖死。
//
// ⚠ 不要把 socket 整体设成 O_NONBLOCK：pav1 的读循环靠阻塞 recv 过日子，
//   一改非阻塞，recv 立刻返回 EAGAIN，那个"等到 deadline"的循环就变成死自旋，
//   实测直接把 IDLE 任务饿到 task_wdt 报警。非阻塞只用在这一次 send 上。
static int send_all_bounded(const void *buf, size_t n, int budget_ms, int flags) {
    const uint8_t *p = buf;
    const int64_t deadline = esp_timer_get_time() + (int64_t)budget_ms * 1000;
    while (n > 0) {
        const int w = send(s_sock, p, n, flags);
        if (w > 0) {
            p += w;
            n -= (size_t)w;
            continue;
        }
        s_last_sock_errno = errno;
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (esp_timer_get_time() >= deadline) return 0;
            vTaskDelay(pdMS_TO_TICKS(2));
            continue;
        }
        return -1;
    }
    return 1;
}

// PCM 上行的暂存区：一帧必须整帧送出才算送出。
// 如果 send 只写出去一半就 EAGAIN，必须把剩下的写到同一帧里，
// 否则下次重试会把帧头再发一遍——对端看到的就是坏流。
// 只按一帧 PCM 开（640 B）：静态缓冲吃的是 RAM，4 KB 的按最大负载开的版本
// 在这里纯属浪费（实测那样多占 3.5 KB 堆，等于把 4 个帧的余量白白丢掉）。
#define NB_STAGE_PCM_MAX 640
static uint8_t s_nb_stage[PAV1_HDR_LEN + NB_STAGE_PCM_MAX];
static size_t s_nb_len;
static size_t s_nb_off;

static int send_frame_budget(pav1_kind_t kind, uint32_t turn_id, const void *payload,
                             uint32_t n, int budget_ms) {
    if (s_sock < 0) return -1;
    uint8_t hdr[PAV1_HDR_LEN];
    pav1_encode(hdr, kind, turn_id, NULL, n);  // 只借它填头
    int r = -1;
    if (s_tx_mu) xSemaphoreTake(s_tx_mu, portMAX_DELAY);
    if (s_sock >= 0) {
        r = send_all_bounded(hdr, sizeof hdr, budget_ms, 0);
        if (r == 1 && n > 0) r = send_all_bounded(payload, n, budget_ms, 0);
    }
    if (s_tx_mu) xSemaphoreGive(s_tx_mu);
    return s_sock < 0 ? -1 : r;
}

static bool send_frame(pav1_kind_t kind, uint32_t turn_id, const void *payload, uint32_t n) {
    return send_frame_budget(kind, turn_id, payload, n, 50) == 1;
}

static void rx_hook(void *ctx, pav1_kind_t kind, uint32_t turn_id, const uint8_t *payload,
                    size_t n) {
    rx_frame_t *f = ctx;
    if (f->got) return;  // 只取第一帧；其余留到下一次 recv
    f->got = true;
    f->kind = kind;
    f->turn_id = turn_id;
    f->payload = payload;
    f->n = n;
}

static void emit_error(pav1_err_t e) {
    char ev[128];
    snprintf(ev, sizeof ev, "{\"event\":\"error\",\"link\":\"audio\",\"err\":\"%s\"}\n",
             pav1_err_str(e));
    terminal_link_send_src(LINK_SRC_TCP, ev);
}

// 读一帧（握手用）。返回 false = 超时/io 错误/解析致命错误。
static bool read_frame(rx_frame_t *f, int timeout_ms, pav1_err_t *err) {
    static uint8_t rx[RX_BUF];
    const int64_t deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    f->got = false;
    while (esp_timer_get_time() < deadline) {
        if (s_sock < 0) return false;
        const int n = recv(s_sock, rx, sizeof rx, 0);
        if (n > 0) {
            const pav1_err_t e = pav1_parser_feed(s_parser, rx, (size_t)n, rx_hook, f);
            if (e != PAV1_E_NONE) {
                if (err) *err = e;
                return false;
            }
            if (f->got) return true;
            continue;
        }
        if (n == 0) return false;
        if (errno != EAGAIN && errno != EWOULDBLOCK) return false;
        // 非阻塞 socket：此刻没数据不是错误，但必须让出 CPU。
        // 少了这一句，这个"等到 deadline"的循环就变成死自旋，
        // IDLE 任务饿死 → task_wdt 报警（实测发生过）。
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    return false;
}

static int dial(const char *host, int port) {
    struct addrinfo hints;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    char portstr[8];
    snprintf(portstr, sizeof portstr, "%d", port);
    struct addrinfo *res = NULL;
    if (getaddrinfo(host, portstr, &hints, &res) != 0 || !res) return -1;
    const int fd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd < 0) {
        freeaddrinfo(res);
        return -1;
    }
    // 音频数据的发送超时必须短。一帧只有 20 ms，如果 250 ms 还塞不进协议栈，说明对端
    // 已经不读或者链路断了；此时继续等 5 秒，等于把"对端不读"变成"设备内存耗尽"——
    // 实测过：缓冲区装到 28 帧后每帧阻塞 5 s，堆从 28 KB 掉到 1.8 KB，连 capture.stop
    // 都进不来。短超时让采集循环能及时回到 s_req_stop 的判断上。
    const struct timeval snd = {.tv_sec = 0, .tv_usec = 250000};
    const struct timeval rcv = {.tv_sec = RECV_TIMEOUT_S, .tv_usec = 0};
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &snd, sizeof snd);
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &rcv, sizeof rcv);
    const int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);  // 音频要小延迟，不要 Nagle
    // lwip 默认发送缓冲只有几 KB，32 KB/s 的上行一旦 RTT 稍大就会被自己的缓冲卡住。
    int sndbuf = 4096;
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof sndbuf);
    // 下行同理：205 ms RTT 下 5760 B 窗口只够 28 KB/s，低于实时所需的 31.2 KB/s。
    // 窗口是这一端"允许对端在途多少字节"，太小就是在自己身上设了个带宽上限。
    int rcvbuf = 16384;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof rcvbuf);
    if (connect(fd, res->ai_addr, res->ai_addrlen) != 0) {
        close(fd);
        freeaddrinfo(res);
        return -1;
    }
    freeaddrinfo(res);
    // 音频 socket 设成非阻塞。⚠ 只做这一步会出事：pav1 的读循环原本靠阻塞 recv
    // 过日子，一改非阻塞 recv 立刻返回 EAGAIN，那个"等到 deadline"的循环就变成
    // 死自旋，实测把 IDLE 饿到 task_wdt 报警。所以 read_frame 里必须对 EAGAIN 退让。
    // 反过来，只给 send 打 MSG_DONTWAIT 也没用：lwIP 不看这个标记，仍按
    // SO_SNDTIMEO 阻塞（实测每拍被拖成 250 ms，2 秒只送出 8 帧）。
    const int fl = fcntl(fd, F_GETFL, 0);
    if (fl >= 0) fcntl(fd, F_SETFL, fl | O_NONBLOCK);
    return fd;
}

static void close_sock(void) {
    if (s_tx_mu) xSemaphoreTake(s_tx_mu, portMAX_DELAY);
    if (s_sock >= 0) {
        if (s_abort_close) {
            // 对端不读的时候不能优雅关闭：socket 里那十几 KB 未确认数据会连同 pcb 一起
            // 挂在 FIN_WAIT，等不到 ACK 就一直占着内存。实测轮次结束后堆停在 4.2 KB、
            // 音频通道再也连不回来。SO_LINGER{1,0} 让 close 直接发 RST，把 pcb 和
            // 重传队列当场释放——音频是"过期即废"的数据，不值得为它保序重传。
            const struct linger lg = {.l_onoff = 1, .l_linger = 0};
            setsockopt(s_sock, SOL_SOCKET, SO_LINGER, &lg, sizeof lg);
            ESP_LOGW(TAG, "RST 关闭音频通道（heap=%u）", (unsigned)esp_get_free_heap_size());
        }
        close(s_sock);
        s_sock = -1;
        // 半帧暂存必须清掉：留着会在下一条通道上接着写上一轮的残帧。
        s_nb_len = s_nb_off = 0;
    }
    s_abort_close = false;
    if (s_tx_mu) xSemaphoreGive(s_tx_mu);
    s_ready = false;
}

static bool json_ok(const cJSON *root, const cJSON **err_out) {
    const cJSON *ok = cJSON_GetObjectItemCaseSensitive(root, "ok");
    if (err_out) *err_out = cJSON_GetObjectItemCaseSensitive(root, "err");
    return cJSON_IsTrue(ok);
}

// 音频通道握手（§3.3）：HELLO → server_nonce → AUTH → HELLO{"ok":true}
static bool do_handshake(void) {
    s_sock = dial(s_host, s_port);
    if (s_sock < 0) {
        ESP_LOGW(TAG, "音频通道连不上 %s:%d", s_host, s_port);
        return false;
    }
    pav1_err_t perr = PAV1_E_NONE;

    char nonce[33];
    terminal_auth_random_hex(nonce, 16);
    char hello[256];
    snprintf(hello, sizeof hello,
             "{\"device_id\":\"%s\",\"proto\":\"v1\",\"session_id\":\"%s\",\"nonce\":\"%s\"}",
             terminal_auth_device_id(), terminal_auth_session_id(), nonce);
    if (!send_frame(PAV1_K_HELLO, 0, hello, (uint32_t)strlen(hello))) {
        ESP_LOGW(TAG, "音频通道 HELLO 发送失败");
        close_sock();
        return false;
    }

    rx_frame_t f;
    if (!read_frame(&f, HANDSHAKE_TIMEOUT_MS, &perr) || f.kind != PAV1_K_HELLO) {
        ESP_LOGW(TAG, "音频通道等 HELLO 应答失败%s%s", perr ? "：" : "",
                 perr ? pav1_err_str(perr) : "");
        if (perr) emit_error(perr);
        close_sock();
        return false;
    }
    cJSON *root = cJSON_ParseWithLength((const char *)f.payload, f.n);
    if (!root) {
        ESP_LOGW(TAG, "HELLO 应答不是 JSON");
        close_sock();
        return false;
    }
    const cJSON *err = NULL;
    if (!json_ok(root, &err)) {
        ESP_LOGW(TAG, "音频通道 HELLO 被拒：%s",
                 cJSON_IsString(err) ? err->valuestring : "?");
        cJSON_Delete(root);
        close_sock();
        return false;
    }
    const cJSON *sn = cJSON_GetObjectItemCaseSensitive(root, "server_nonce");
    if (!cJSON_IsString(sn)) {
        // 没有 server_nonce 的 ok 只可能是 AUTH 的 ack，顺序不对就按失败处理。
        ESP_LOGW(TAG, "HELLO 应答缺 server_nonce");
        cJSON_Delete(root);
        close_sock();
        return false;
    }
    char srv_nonce[40];
    snprintf(srv_nonce, sizeof srv_nonce, "%s", sn->valuestring);
    cJSON_Delete(root);

    char mac[65];
    // nonce 明文上线，打出来便于离线对齐（MAC 永不落日志）。
    ESP_LOGI(TAG, "nonce dev=%s srv=%s", nonce, srv_nonce);
    if (!terminal_auth_audio_mac(nonce, srv_nonce, mac)) {
        ESP_LOGW(TAG, "无密钥，无法完成音频通道认证");
        close_sock();
        return false;
    }
    char auth[96];
    snprintf(auth, sizeof auth, "{\"mac\":\"%s\"}", mac);
    if (!send_frame(PAV1_K_AUTH, 0, auth, (uint32_t)strlen(auth))) {
        ESP_LOGW(TAG, "音频通道 AUTH 发送失败");
        close_sock();
        return false;
    }

    if (!read_frame(&f, HANDSHAKE_TIMEOUT_MS, &perr) || f.kind != PAV1_K_HELLO) {
        ESP_LOGW(TAG, "音频通道等 AUTH 应答失败");
        close_sock();
        return false;
    }
    root = cJSON_ParseWithLength((const char *)f.payload, f.n);
    if (!root) {
        ESP_LOGW(TAG, "AUTH 应答不是 JSON");
        close_sock();
        return false;
    }
    const bool ok = json_ok(root, &err);
    if (!ok) ESP_LOGW(TAG, "音频通道认证被拒：%s", cJSON_IsString(err) ? err->valuestring : "?");
    cJSON_Delete(root);
    if (!ok) {
        close_sock();
        return false;
    }
    s_ready = true;
    ESP_LOGI(TAG, "音频通道就绪 %s:%d（session=%s）", s_host, s_port,
             terminal_auth_session_id());
    return true;
}

static void serve_frame(void *ctx, pav1_kind_t kind, uint32_t turn_id, const uint8_t *payload,
                        size_t n) {
    (void)ctx;
    (void)payload;
    if (kind == PAV1_K_PCM) {
        s_rx_frames++;
        s_rx_bytes += (uint32_t)n;
        if (s_rx_only) return;  // 隔离实验：只读不播，量读循环本身的上限
        // 收到的每一帧都交给播放环：轮次对不对、缓冲满不满由它判定并计数。
        terminal_playback_push(turn_id, payload, n);
    } else if (kind == PAV1_K_END) {
        s_rx_end++;
        ESP_LOGI(TAG, "收到 END turn=%u", (unsigned)turn_id);
        if (!s_rx_only) terminal_playback_mark_end(turn_id);  // 环里剩的放完再收轮
    } else {
        ESP_LOGW(TAG, "运行期收到非音频帧 kind=%d turn=%u", (int)kind, (unsigned)turn_id);
    }
}

static void do_uplink_test(void) {
    s_ul_req = false;
    const uint32_t frames = s_ul_frames ? s_ul_frames : 25;
    const uint32_t tone = s_ul_tone ? s_ul_tone : 1000;
    static int16_t pcm[PAV1_FRAME_BYTES / 2];
    uint32_t sent = 0;
    s_last_sock_errno = 0;
    const int64_t t0 = esp_timer_get_time();
    for (uint32_t i = 0; i < frames && s_ready; i++) {
        for (int j = 0; j < (int)(PAV1_FRAME_BYTES / 2); j++) {
            const double t = (double)(i * (PAV1_FRAME_BYTES / 2) + (uint32_t)j) / 16000.0;
            pcm[j] = (int16_t)(sin(2.0 * M_PI * (double)tone * t) * 8000.0);
        }
        if (terminal_pav1_send_pcm(7, pcm, sizeof pcm) == (int)sizeof pcm) sent++;
        else break;
        vTaskDelay(pdMS_TO_TICKS(20));  // 按 20 ms 节拍推，不突发灌爆对端
    }
    const int elapsed_ms = (int)((esp_timer_get_time() - t0) / 1000);
    char ev[256];
    snprintf(ev, sizeof ev,
             "{\"event\":\"uplink_test\",\"ok\":%s,\"frames\":%u,\"sent\":%u,\"bytes\":%u,"
             "\"elapsed_ms\":%d,\"tone_hz\":%u,\"frame_bytes\":%u,\"sock_errno\":%d}\n",
             (sent == frames) ? "true" : "false", (unsigned)frames, (unsigned)sent,
             (unsigned)(sent * PAV1_FRAME_BYTES), elapsed_ms, (unsigned)tone,
             (unsigned)PAV1_FRAME_BYTES, s_last_sock_errno);
    terminal_link_send_src(LINK_SRC_TCP, ev);
    ESP_LOGI(TAG, "上行自测：%u/%u 帧，%d ms，errno=%d", (unsigned)sent, (unsigned)frames, elapsed_ms,
             s_last_sock_errno);
}

static void serve(void) {
    static uint8_t rx[RX_BUF];
    int64_t last_rx = esp_timer_get_time();
    // 上行看门狗：对端不读时，发送会一路堵死，采集任务自己卡在 send 里，什么检查都跑不到。
    // 这个循环至少每秒醒一次（recv 超时 1 s），是全设备唯一还在动的观察点。
    // 判据用"已发出帧数是否还在涨"，不用丢帧计数——后者取决于每次 send 阻塞多久。
    uint32_t wd_ref = s_tx_frames;
    int64_t wd_since = esp_timer_get_time();
    while (s_should_run) {
        if (terminal_capture_capturing()) {
            const int64_t wd_now = esp_timer_get_time();
            if (s_tx_frames != wd_ref) {
                wd_ref = s_tx_frames;
                wd_since = wd_now;
            } else if ((wd_now - wd_since) / 1000 > UPLINK_STALL_MS) {
                ESP_LOGW(TAG, "上行 %d ms 没有一帧送出，收轮并重建音频通道", UPLINK_STALL_MS);
                terminal_capture_force_stop("uplink_stall");
                // 必须关连接：只收轮不关，socket 里那十几 KB 未确认数据不会还回来，
                // 实测轮次结束后堆停在 1.4 KB、block 896，设备再也恢复不了。
                // 而且必须用 RST 关（见 close_sock）：对端不 ACK 时优雅关闭会把
                // pcb 和重传队列一起挂住。
                s_abort_close = true;
                return;
            }
        } else {
            wd_ref = s_tx_frames;
            wd_since = esp_timer_get_time();
        }
        if (s_ul_req) do_uplink_test();
        if (s_sock < 0) return;
        const int n = recv(s_sock, rx, sizeof rx, 0);
        if (n > 0) {
            last_rx = esp_timer_get_time();
            const pav1_err_t e = pav1_parser_feed(s_parser, rx, (size_t)n, serve_frame, NULL);
            if (e != PAV1_E_NONE) {
                // 致命帧错误：报 error 事件并关通道（§5.1）
                s_last_err = e;
                s_rx_bad++;
                ESP_LOGE(TAG, "帧解析失败（%s），关闭音频通道", pav1_err_str(e));
                emit_error(e);
                return;
            }
        } else if (n == 0) {
            ESP_LOGW(TAG, "服务端关闭音频通道");
            return;
        } else if (errno == EAGAIN || errno == EWOULDBLOCK) {
            if (esp_timer_get_time() - last_rx > (int64_t)IDLE_DROP_S * 1000000) {
                ESP_LOGW(TAG, "音频通道静默 %d s，主动放弃", IDLE_DROP_S);
                return;
            }
        } else {
            ESP_LOGW(TAG, "音频通道 recv 错误 errno=%d", errno);
            return;
        }
        // 非阻塞 socket：没有数据时必须让出 CPU，否则 serve 循环变成死自旋，
        // 把 IDLE 任务饿到 task_wdt 报警（实测发生过一次）。
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

static void pav1_task(void *arg) {
    (void)arg;
    for (;;) {
        // 没有会话就睡到被叫醒：不做"常开无限重试"的守护循环（§3.3）。
        if (!s_should_run) {
            xSemaphoreTake(s_wake, portMAX_DELAY);
            continue;
        }
        int fails = 0;
        while (s_should_run && fails < 3) {
            if (s_parser && do_handshake()) {
                fails = 0;
                serve();
                if (pav1_parser_pending(s_parser) && s_last_err == PAV1_E_NONE) {
                    s_last_err = PAV1_E_INCOMPLETE_FRAME;
                    emit_error(PAV1_E_INCOMPLETE_FRAME);
                    ESP_LOGW(TAG, "连接关闭时残留半帧");
                }
                pav1_parser_free(s_parser);
                s_parser = pav1_parser_new();
            } else {
                fails++;
                ESP_LOGW(TAG, "音频通道第 %d 次失败", fails);
            }
            close_sock();
            if (s_should_run && fails < 3) vTaskDelay(pdMS_TO_TICKS(2400));
        }
        if (fails >= 3) {
            ESP_LOGW(TAG, "音频通道连续失败，退回无音频状态（控制通道仍可用）");
            s_should_run = false;
        }
    }
}

void terminal_pav1_start(const char *host, int port) {
    if (!host || port <= 0 || port > 65535) {
        ESP_LOGW(TAG, "音频通道参数无效（%s:%d）", host ? host : "-", port);
        return;
    }
    if (!terminal_auth_has_secret()) {
        ESP_LOGI(TAG, "未 provision，暂不打开音频通道");
        return;
    }
    snprintf(s_host, sizeof s_host, "%s", host);
    s_port = port;
    s_last_err = PAV1_E_NONE;
    s_should_run = true;
    if (s_wake) xSemaphoreGive(s_wake);
}

void terminal_pav1_stop(void) {
    if (!s_should_run && !s_ready) return;
    ESP_LOGW(TAG, "停音频通道（heap=%u）", (unsigned)esp_get_free_heap_size());
    s_should_run = false;
    s_ready = false;
    // 跨任务只做 shutdown：它能叫醒阻塞中的 recv，而 close 留给持有者做。
    xSemaphoreTake(s_tx_mu, portMAX_DELAY);
    if (s_sock >= 0) shutdown(s_sock, SHUT_RDWR);
    xSemaphoreGive(s_tx_mu);
    ESP_LOGI(TAG, "音频通道随控制会话关闭");
}

bool terminal_pav1_ready(void) { return s_ready; }

uint32_t terminal_pav1_rx_frames(void) { return s_rx_frames; }
uint32_t terminal_pav1_rx_end(void) { return s_rx_end; }
uint32_t terminal_pav1_tx_frames(void) { return s_tx_frames; }

// ---- 隔离实验：只从音频 socket 读、解析、计数，不推环、不播放 ----
// 目的：把"读循环的上限"和"播放路径的开销"分开量。若这里也只有 15–25 帧/秒，
// 瓶颈在读循环/链路；若这里能到 50+ 帧/秒，说明是播放路径把读拖慢了。
//
// 同步返回统计值而不是发一个 event：异步 event 需要调用方再做一次 get_audio
// 差分，而差分一旦遇到设备重启就整段失效（对方已经因此误判过一次）。
esp_err_t terminal_pav1_rx_rate(int ms, uint32_t *frames, uint32_t *bytes, int *elapsed_ms) {
    if (ms <= 0 || ms > 10000) return ESP_ERR_INVALID_ARG;
    if (!s_ready || s_sock < 0) return ESP_ERR_INVALID_STATE;
    const uint32_t f0 = s_rx_frames, b0 = s_rx_bytes;
    const int64_t t0 = esp_timer_get_time();
    s_rx_only = true;
    vTaskDelay(pdMS_TO_TICKS(ms));
    s_rx_only = false;
    const int64_t dt = esp_timer_get_time() - t0;
    if (frames) *frames = s_rx_frames - f0;
    if (bytes) *bytes = s_rx_bytes - b0;
    if (elapsed_ms) *elapsed_ms = (int)(dt / 1000);
    ESP_LOGI(TAG, "rx_rate: %u 帧 / %u B / %d ms", (unsigned)(s_rx_frames - f0),
             (unsigned)(s_rx_bytes - b0), (int)(dt / 1000));
    return ESP_OK;
}

bool terminal_pav1_starting(void) { return s_should_run && !s_ready; }

int terminal_pav1_send_pcm(uint32_t turn_id, const void *pcm, size_t n) {
    if (!s_ready || !pcm || n == 0 || n > PAV1_MAX_PAYLOAD) return 0;
    if (!send_frame(PAV1_K_PCM, turn_id, pcm, (uint32_t)n)) return 0;
    s_tx_frames++;
    s_tx_bytes += (uint32_t)n;
    return (int)n;
}

// 非阻塞版：返回 >0 = 这一帧已完整送出；0 = 此刻写不进去（可重试，不是错误）；
// -1 = 通道不可用/真错误。采集侧靠这个"0"把帧留在自己的有界环里，
// 而不是让 send 阻塞几百毫秒、把堆和整轮状态一起拖死。
//
// 关键语义（旧实现有 bug，这里说清楚）：暂存区只放一帧。若上一次调用只写出半帧，
// 这次调用会**先把那半帧写完，再把本次的新帧装进去发**，只有当"本次的新帧"完整
// 写出时才返回 n 并计数。旧实现在这种情况下直接返回 n，把新帧丢了还谎报成功。
static int pav1_send_nowait_locked(pav1_kind_t kind, uint32_t turn_id, const uint8_t *payload,
                                   size_t n) {
    for (int pass = 0; pass < 2; pass++) {
        const bool is_new = (s_nb_len == 0);
        if (is_new) {
            pav1_encode(s_nb_stage, kind, turn_id, NULL, (uint32_t)n);
            memcpy(s_nb_stage + PAV1_HDR_LEN, payload, n);
            s_nb_len = PAV1_HDR_LEN + n;
            s_nb_off = 0;
        }
        while (s_nb_off < s_nb_len) {
            const size_t left = s_nb_len - s_nb_off;
            const int w = send(s_sock, s_nb_stage + s_nb_off, left, MSG_DONTWAIT);
            if (w > 0) {
                s_nb_off += (size_t)w;
                continue;
            }
            s_last_sock_errno = errno;
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                return 0;   // 留着，下次接着写同一帧
            }
            s_nb_len = s_nb_off = 0;
            return -1;
        }
        s_nb_len = s_nb_off = 0;
        if (is_new) {
            s_tx_frames++;
            s_tx_bytes += (uint32_t)n;
            return (int)n;
        }
    }
    return 0;
}

static int pav1_send_nowait(pav1_kind_t kind, uint32_t turn_id, const void *payload, size_t n) {
    if (!s_ready || !payload || n == 0 || n > NB_STAGE_PCM_MAX) return -1;
    if (s_tx_mu) xSemaphoreTake(s_tx_mu, portMAX_DELAY);
    const int ret = (s_sock < 0) ? (s_nb_len = s_nb_off = 0, -1)
                                 : pav1_send_nowait_locked(kind, turn_id, payload, n);
    if (s_tx_mu) xSemaphoreGive(s_tx_mu);
    return ret;
}

int terminal_pav1_send_pcm_nowait(uint32_t turn_id, const void *pcm, size_t n) {
    return pav1_send_nowait(PAV1_K_PCM, turn_id, pcm, n);
}

// 上行 ADPCM 块：payload = [u32 seq][164 B IMA 块]。seq 让服务端能算丢帧/乱序，
// 第 3 段换 UDP 时同一份载荷可以直接用，不需要再改格式。
int terminal_pav1_send_adpcm_nowait(uint32_t turn_id, uint32_t seq, const void *blk, size_t n) {
    if (n > NB_STAGE_PCM_MAX - 4) return -1;
    uint8_t payload[NB_STAGE_PCM_MAX];
    payload[0] = (uint8_t)(seq & 0xff);
    payload[1] = (uint8_t)((seq >> 8) & 0xff);
    payload[2] = (uint8_t)((seq >> 16) & 0xff);
    payload[3] = (uint8_t)((seq >> 24) & 0xff);
    memcpy(payload + 4, blk, n);
    return pav1_send_nowait(PAV1_K_ADPCM, turn_id, payload, n + 4);
}

bool terminal_pav1_send_end(uint32_t turn_id) {
    if (!s_ready) return false;
    return send_frame(PAV1_K_END, turn_id, NULL, 0);
}

bool terminal_pav1_request_uplink_test(uint32_t frames, uint32_t tone_hz) {
    if (!s_ready) return false;
    s_ul_frames = frames;
    s_ul_tone = tone_hz;
    s_ul_req = true;
    return true;
}

void terminal_pav1_describe(char *buf, size_t n) {
    snprintf(buf, n,
             "\"audio_ready\":%s,\"audio_rx_frames\":%u,\"audio_rx_end\":%u,"
             "\"audio_tx_frames\":%u,\"audio_err\":\"%s\"",
             s_ready ? "true" : "false", (unsigned)s_rx_frames, (unsigned)s_rx_end,
             (unsigned)s_tx_frames, pav1_err_str(s_last_err));
}

void terminal_pav1_init(void) {
    if (!s_tx_mu) s_tx_mu = xSemaphoreCreateMutex();
    if (!s_wake) s_wake = xSemaphoreCreateBinary();
    if (!s_parser) s_parser = pav1_parser_new();
    if (!s_task) {
        xTaskCreate(pav1_task, "pav1", 4096, NULL, 5, &s_task);
    }
}