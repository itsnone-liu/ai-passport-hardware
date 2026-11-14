// main/terminal_udp.c — UDP 音频通道（W0-9，第 3 段）
//
// 每个 20 ms 块一个数据报：PAV1 16 B 头 + [u32 seq][164 B ADPCM] = 188 B。
// 50 包/秒、8.4 KB/s，比 TCP 版多 16 B/块的头部开销（约 9%），换来的是
// 丢包不排队：一块丢了就丢了，不阻塞后面 20 ms。
//
// 打洞流程（设备发起）：
//   1. 发 kind=2 的 HELLO 数据报，带设备 id 和本 turn 之前的 seq 空间；
//   2. 服务端从源地址学到我们，回一个 kind=2 的 ack；
//   3. 收到 ack 才把 s_ready 置位，采集才开始走 UDP。
// 没有 ack 就一直发 HELLO（每 500 ms 一次），并且采集继续走 TCP。

#include "terminal_udp.h"

#include "terminal_auth.h"
#include "terminal_adpcm.h"
#include "terminal_pav1.h"
#include "terminal_playback.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>

static const char *TAG = "udp";

#define UDP_HDR_LEN 16
#define UDP_MAX_PAYLOAD (4 + 164)
#define UDP_BUF_LEN (UDP_HDR_LEN + UDP_MAX_PAYLOAD)
#define KEEPALIVE_MS 2000
#define ACK_TIMEOUT_MS 500
#define SLOW_RETRY_MS 3000   // 打洞久无回音后的重试间隔（不放弃）
#define MAC_HELLO 8          // HELLO/ack 的截断 MAC 长度：伪造一次要 2^64
#define MAC_AUDIO 4          // 音频块：4 字节 = 188 B 包里多 2%，伪造一块要 2^32

static int s_sock = -1;
static struct sockaddr_in s_peer;
static volatile bool s_should_run;
static volatile bool s_ready;      // 服务端 ack 过
static volatile bool s_slow;        // ack 久等不到：降频重试，但不放弃
static uint32_t s_sent, s_fail, s_acks, s_hello;
static uint32_t s_down, s_bad, s_badmac;  // 下行收到的音频块 / 坏包
static volatile int s_last_errno;
static int64_t s_ack_ms;           // 首次 ack 的时刻（用于报告打洞耗时）
static TaskHandle_t s_task;

static uint32_t rd32(const uint8_t *b) {
    return (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
}

static void wr32(uint8_t *b, uint32_t v) {
    b[0] = (uint8_t)(v & 0xFF);
    b[1] = (uint8_t)((v >> 8) & 0xFF);
    b[2] = (uint8_t)((v >> 16) & 0xFF);
    b[3] = (uint8_t)((v >> 24) & 0xFF);
}

static void put_hdr(uint8_t *b, uint8_t kind, uint32_t turn, uint32_t len) {
    b[0] = 'P';
    b[1] = 'A';
    b[2] = 'V';
    b[3] = '1';
    b[4] = kind;
    b[5] = 0;   // flags
    b[6] = 0;   // reserved
    b[7] = 0;
    wr32(b + 8, turn);
    wr32(b + 12, len);
}

static int raw_send(const uint8_t *buf, size_t n) {
    if (s_sock < 0) return -ENOTCONN;
    const int w = sendto(s_sock, buf, n, 0, (const struct sockaddr *)&s_peer, sizeof s_peer);
    if (w < 0) {
        s_last_errno = errno;
        return -errno;
    }
    return w;
}

// 逐包 MAC。UDP 没有握手，而设备号是公开的（日志、仓库里都有），所以"这包确实
// 出自知道密钥的人"只能靠摘要来证明。签名范围 = 16 字节头 + 应用载荷，头里含
// kind/turn/payload_len，因此改头也会验签失败。截断是有意的：188 B 的包再加
// 32 字节 MAC 就是 17% 开销，而攻击者只需要一次成功就能灌一个 20 ms 块。
static bool sign_and_send(uint8_t *buf, size_t n, size_t maclen) {
    uint8_t mac[32];
    if (!terminal_auth_raw_hmac(buf, n, mac)) return false;   // 没 provision 就不发
    memcpy(buf + n, mac, maclen);
    return raw_send(buf, n + maclen) > 0;
}

static bool verify_mac(const uint8_t *buf, size_t n, size_t maclen) {
    uint8_t mac[32];
    if (!terminal_auth_raw_hmac(buf, n, mac)) return false;
    // 定长比较，不看第一个字节就返回——MAC 比较不能有早退
    uint8_t diff = 0;
    for (size_t i = 0; i < maclen; i++) diff |= (uint8_t)(mac[i] ^ buf[n + i]);
    return diff == 0;
}

// kind=2：HELLO / ack。payload 里带设备 id，服务端据此把源地址绑到设备。
static void send_hello(void) {
    uint8_t buf[UDP_HDR_LEN + 64 + MAC_HELLO];
    const char *id = terminal_auth_device_id();
    const size_t idlen = id ? strlen(id) : 0;
    put_hdr(buf, PAV1_K_HELLO, 0, (uint32_t)idlen);
    if (idlen) memcpy(buf + UDP_HDR_LEN, id, idlen);
    if (!sign_and_send(buf, UDP_HDR_LEN + idlen, MAC_HELLO)) {
        s_fail++;
        return;
    }
    s_hello++;
}

// 下行重排窗：4 块 = 80 ms。上行实测 142 块里有 1 块乱序，按到达顺序播会把
// 相邻两个 20 ms 块调个位置——不是静音也不是爆音，是音节内部错位，很难查。
// 窗口小是有意的：等太久会吃掉播放环的余量，80 ms 足够盖住这条链路的抖动。
static void win_push(uint32_t turn, uint32_t seq, const uint8_t *blk);

// 下行前向纠错：3 块一组补 1 块奇偶（字节级 XOR）。
// 为什么不是 NACK 重传：一个往返 200 ms，等回来那一块早就该播了；而播放环
// 只有几百毫秒余量，重传窗口很窄。XOR 奇偶不需要往返——丢一块，奇偶块到的
// 那一刻就能算出来。代价是 +33% 带宽（8.4 → 11.2 KB/s，链路实测上限 18.7）。
// 丢两块就救不回来，那种情况记 norecover 如实上报。
#define FEC_GROUP 3
#define PAV1_FLAG_FEC 0x01     // 头里的 flags 字节：这一包是奇偶块，不是音频

#define DOWN_WIN 4
static uint8_t s_win_blk[DOWN_WIN][ADPCM_BLOCK_BYTES];
static uint32_t s_win_seq[DOWN_WIN];
static uint32_t s_win_turn[DOWN_WIN];
static bool s_win_used[DOWN_WIN];
static bool s_down_started;
static uint32_t s_down_expected;
static uint32_t s_down_turn;
static uint32_t s_late, s_reorder, s_gap;
static uint8_t s_fec_blk[FEC_GROUP][ADPCM_BLOCK_BYTES];
static uint32_t s_fec_seq[FEC_GROUP];
static bool s_fec_used[FEC_GROUP];
static uint32_t s_parity, s_recovered, s_norecover;
static uint32_t s_fec_turn;

// 收到的音频块顺手存进组缓冲，等奇偶块来判丢。
static void fec_remember(uint32_t turn, uint32_t seq, const uint8_t *blk) {
    const int slot = (int)(seq % FEC_GROUP);
    // 换组就丢弃上一组：等不到的奇偶块说明那组本来就没打算纠错。
    if (s_fec_used[slot] && s_fec_seq[slot] != seq &&
        (seq % FEC_GROUP) == 0) {
        for (int i = 0; i < FEC_GROUP; i++) s_fec_used[i] = false;
    }
    memcpy(s_fec_blk[slot], blk, ADPCM_BLOCK_BYTES);
    s_fec_seq[slot] = seq;
    s_fec_used[slot] = true;
    s_fec_turn = turn;
}

// 奇偶块到了：组里正好缺一块就重建它，按原 seq 塞回重排窗。
static void fec_recover(uint32_t turn, uint32_t base, const uint8_t *parity) {
    s_parity++;
    uint8_t acc[ADPCM_BLOCK_BYTES];
    memcpy(acc, parity, ADPCM_BLOCK_BYTES);
    int missing = -1;
    int present = 0;
    for (int i = 0; i < FEC_GROUP; i++) {
        const uint32_t want = base + (uint32_t)i;
        if (s_fec_used[i] && s_fec_seq[i] == want) {
            present++;
            for (int k = 0; k < ADPCM_BLOCK_BYTES; k++) acc[k] ^= s_fec_blk[i][k];
        } else {
            missing = i;
        }
    }
    if (present == FEC_GROUP) return;          // 齐了，奇偶块白来一趟（正常）
    if (present != FEC_GROUP - 1 || missing < 0) {
        s_norecover++;                          // 一次丢两块，救不回来
        return;
    }
    (void)turn;
    win_push(s_fec_turn, base + (uint32_t)missing, acc);
    s_recovered++;
}

static void win_clear(void) {
    for (int i = 0; i < DOWN_WIN; i++) s_win_used[i] = false;
}

// 按 seq 投递，遇洞先等；窗口被更靠前的块占满就认定洞里那块丢了，跳过去。
static void deliver(uint32_t seq) {
    const int slot = (int)(seq % DOWN_WIN);
    int16_t pcm[ADPCM_SAMPLES];
    adpcm_dec_block(s_win_blk[slot], pcm);
    if (terminal_playback_push(s_win_turn[slot], pcm, sizeof pcm)) s_down++;
    s_win_used[slot] = false;
    s_down_expected = seq + 1;
}

static void drain_in_order(void) {
    while (s_win_used[s_down_expected % DOWN_WIN] &&
           s_win_seq[s_down_expected % DOWN_WIN] == s_down_expected) {
        deliver(s_down_expected);
    }
}

static void win_push(uint32_t turn, uint32_t seq, const uint8_t *blk) {
    if (!s_down_started || turn != s_down_turn) {
        // 换轮：上一轮压着的块一律作废，别把上一句话的尾巴接到这一句上。
        s_down_started = true;
        s_down_turn = turn;
        s_down_expected = seq;
        win_clear();
    }
    if ((int32_t)(seq - s_down_expected) < 0) {   // 迟到或重复：丢掉，不重复出声
        s_late++;
        return;
    }
    const int slot = (int)(seq % DOWN_WIN);
    if (s_win_used[slot]) {
        if (s_win_seq[slot] == seq) {      // 重复包
            s_late++;
            return;
        }
        s_reorder++;                       // 这一格被更靠前的块顶掉，等于丢了一块
    } else if (seq != s_down_expected) {
        s_reorder++;                       // 乱序到达：先存着
    }
    memcpy(s_win_blk[slot], blk, ADPCM_BLOCK_BYTES);
    s_win_seq[slot] = seq;
    s_win_turn[slot] = turn;
    s_win_used[slot] = true;
    drain_in_order();
    // 洞口大过窗口：再等也等不回来，跳到窗口里最小的那块继续吐，
    // 否则播放环会被这一个洞卡到饿死（那是几百毫秒的静音）。
    if ((uint32_t)(seq - s_down_expected) >= DOWN_WIN) {
        uint32_t best = 0;
        bool any = false;
        for (int i = 0; i < DOWN_WIN; i++) {
            if (!s_win_used[i]) continue;
            if (!any || (int32_t)(s_win_seq[i] - best) < 0) {
                best = s_win_seq[i];
                any = true;
            }
        }
        if (any && (int32_t)(best - s_down_expected) > 0) {
            s_gap++;
            s_down_expected = best;
            drain_in_order();
        }
    }
}

// 收数据报：kind=2 是打洞 ack，kind=5 是下行音频块（服务端→设备）。
// 下行也走同一条洞，所以这里两头都要认：只认 ack 的话，打通了也照样没声音。
static void poll_rx(void) {
    uint8_t buf[256];
    for (;;) {
        const int r = recv(s_sock, buf, sizeof buf, MSG_DONTWAIT);
        if (r < UDP_HDR_LEN) return;
        if (memcmp(buf, "PAV1", 4) != 0) continue;
        const uint32_t dlen = rd32(buf + 12);
        const size_t maclen = (buf[4] == PAV1_K_HELLO) ? MAC_HELLO : MAC_AUDIO;
        // 长度必须精确：头里写的载荷长度 + MAC 就是整个数据报，多一个字节都不认。
        if ((size_t)r != UDP_HDR_LEN + (size_t)dlen + maclen) continue;
        // 先验签再解释内容。被扔掉的水包/伪造包一个 bit 都不会进播放环。
        if (!verify_mac(buf, UDP_HDR_LEN + dlen, maclen)) {
            s_badmac++;
            continue;
        }
        if (buf[4] == PAV1_K_HELLO) {
            s_acks++;
            if (!s_ready) {
                s_ready = true;
                s_slow = false;   // 通了就不再是"慢速重试"模式
                s_ack_ms = esp_timer_get_time() / 1000;
                ESP_LOGI(TAG, "UDP 打洞完成：服务端听得到我们了（%u 次 HELLO 后 ack）",
                         (unsigned)s_hello);
            }
            continue;
        }
        if (buf[4] != PAV1_K_ADPCM) continue;
        // 载荷 = [u32 seq][164 B 块]；块自带预测器和步长，丢一块只损失 20 ms。
        if (dlen != UDP_MAX_PAYLOAD) {
            s_bad++;
            continue;
        }
        const uint32_t turn = rd32(buf + 8);
        const uint8_t *p = buf + UDP_HDR_LEN;
        if (buf[5] & PAV1_FLAG_FEC) {
            // 奇偶块：base 是这一组的第一块，载荷是组内三块的字节级 XOR。
            fec_recover(turn, rd32(p), p + 4);
            continue;
        }
        fec_remember(turn, rd32(p), p + 4);
        win_push(turn, rd32(p), p + 4);
    }
}

static void udp_task(void *arg) {
    (void)arg;
    int64_t last_hello = 0;
    while (s_should_run) {
        const int64_t now = esp_timer_get_time() / 1000;
        // 有 ack 之后 2 s 一个保活包，维持 NAT 映射；没 ack 时先 500 ms 一次
        // 快打，8 秒还没回音就降到 3 s 一次继续试——设备挂在墙上，不重刷固件
        // 也要有机会自己恢复。
        const int64_t gap = s_ready ? KEEPALIVE_MS : (s_slow ? SLOW_RETRY_MS : ACK_TIMEOUT_MS);
        if (now - last_hello >= gap) {
            last_hello = now;
            send_hello();
            if (!s_ready && !s_slow && s_hello > 16) {
                s_slow = true;
                ESP_LOGW(TAG, "UDP 打洞暂未回音（%u 次 HELLO），转 3 s 重试；采集继续走 TCP",
                         (unsigned)s_hello);
            }
        }
        poll_rx();
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    s_task = NULL;
    vTaskDelete(NULL);
}

bool terminal_udp_start(uint32_t peer_ip, uint16_t peer_port) {
    if (peer_ip == 0 || peer_port == 0) return false;
    // 对端地址每次都刷新：音频通道重连后端口/IP 可能变。
    memset(&s_peer, 0, sizeof s_peer);
    s_peer.sin_family = AF_INET;
    s_peer.sin_addr.s_addr = peer_ip;
    s_peer.sin_port = htons(peer_port);

    // 之前打洞失败时任务退出了、socket 还留着；这里必须能把任务重新拉起来，
    // 否则"重连一次就再也打不通"，只能刷固件——这台设备是要挂在墙上的。
    if (s_task) return true;

    if (s_sock < 0) {
        const int fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (fd < 0) {
            ESP_LOGW(TAG, "UDP socket 建不出来 errno=%d", errno);
            return false;
        }
        struct timeval tv = {.tv_sec = 0, .tv_usec = 100 * 1000};
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
        s_sock = fd;
    }
    s_should_run = true;
    s_down_started = false;   // 新通道：下行窗口清空，从服务端下一个块重新对齐
    win_clear();
    if (xTaskCreate(udp_task, "udp", 3072, NULL, 4, &s_task) != pdPASS) {
        close(s_sock);
        s_sock = -1;
        s_should_run = false;
        return false;
    }
    char ip[16];
    snprintf(ip, sizeof ip, "%u.%u.%u.%u", (unsigned)(peer_ip & 0xFF),
             (unsigned)((peer_ip >> 8) & 0xFF), (unsigned)((peer_ip >> 16) & 0xFF),
             (unsigned)((peer_ip >> 24) & 0xFF));
    ESP_LOGI(TAG, "UDP 音频通道启动：对端 %s:%u（等 ack 前仍走 TCP）", ip,
             (unsigned)peer_port);
    return true;
}

void terminal_udp_stop(void) {
    s_should_run = false;
    if (s_sock >= 0) {
        close(s_sock);
        s_sock = -1;
    }
    s_ready = false;
    s_slow = false;
}

bool terminal_udp_ready(void) { return s_sock >= 0 && s_ready; }

bool terminal_udp_send_adpcm(uint32_t turn, uint32_t seq, const uint8_t *blk, size_t n) {
    if (!terminal_udp_ready() || !blk || n + 4 > UDP_MAX_PAYLOAD) return false;
    uint8_t buf[UDP_BUF_LEN];
    put_hdr(buf, PAV1_K_ADPCM, turn, (uint32_t)(n + 4));
    wr32(buf + UDP_HDR_LEN, seq);
    memcpy(buf + UDP_HDR_LEN + 4, blk, n);
    if (!sign_and_send(buf, UDP_HDR_LEN + 4 + n, MAC_AUDIO)) {
        s_fail++;
        return false;
    }
    s_sent++;
    return true;
}

int terminal_udp_diag(char *buf, size_t n) {
    return snprintf(buf, n,
             "\"udp_ready\":%s,\"udp_sent\":%u,\"udp_fail\":%u,\"udp_ack\":%u,"
             "\"udp_hello\":%u,\"udp_down\":%u,\"udp_bad\":%u,\"udp_badmac\":%u,\"udp_late\":%u,"
             "\"udp_reorder\":%u,\"udp_gap\":%u,\"udp_parity\":%u,\"udp_recovered\":%u,\"udp_norecover\":%u,\"udp_slow\":%s,\"udp_errno\":%d,\"udp_ack_ms\":%lld",
             terminal_udp_ready() ? "true" : "false", (unsigned)s_sent, (unsigned)s_fail,
             (unsigned)s_acks, (unsigned)s_hello, (unsigned)s_down, (unsigned)s_bad,
             (unsigned)s_badmac,
             (unsigned)s_late, (unsigned)s_reorder, (unsigned)s_gap, (unsigned)s_parity,
             (unsigned)s_recovered, (unsigned)s_norecover, s_slow ? "true" : "false",
             s_last_errno, (long long)s_ack_ms);
}