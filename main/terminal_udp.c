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
#include "terminal_pav1.h"

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

static int s_sock = -1;
static struct sockaddr_in s_peer;
static volatile bool s_should_run;
static volatile bool s_ready;      // 服务端 ack 过
static volatile bool s_slow;        // ack 久等不到：降频重试，但不放弃
static uint32_t s_sent, s_fail, s_acks, s_hello;
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

// kind=2：HELLO / ack。payload 里带设备 id，服务端据此把源地址绑到设备。
static void send_hello(void) {
    uint8_t buf[UDP_HDR_LEN + 64];
    const char *id = terminal_auth_device_id();
    const size_t idlen = id ? strlen(id) : 0;
    put_hdr(buf, PAV1_K_HELLO, 0, (uint32_t)idlen);
    if (idlen) memcpy(buf + UDP_HDR_LEN, id, idlen);
    const int w = raw_send(buf, UDP_HDR_LEN + idlen);
    if (w > 0) s_hello++;
}

// 收数据报：只在 ack 之前有用（等 kind=2），之后服务端不会再发。
static void poll_ack(void) {
    uint8_t buf[256];
    for (;;) {
        const int r = recv(s_sock, buf, sizeof buf, MSG_DONTWAIT);
        if (r < UDP_HDR_LEN) return;
        if (memcmp(buf, "PAV1", 4) != 0) continue;
        if (buf[4] != PAV1_K_HELLO) continue;
        s_acks++;
        if (!s_ready) {
            s_ready = true;
            s_slow = false;   // 通了就不再是"慢速重试"模式
            s_ack_ms = esp_timer_get_time() / 1000;
            ESP_LOGI(TAG, "UDP 打洞完成：服务端听得到我们了（%u 次 HELLO 后 ack）",
                     (unsigned)s_hello);
        }
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
        poll_ack();
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
    const int w = raw_send(buf, UDP_HDR_LEN + 4 + n);
    if (w > 0) {
        s_sent++;
        return true;
    }
    s_fail++;
    return false;
}

void terminal_udp_diag(char *buf, size_t n) {
    snprintf(buf, n,
             "\"udp_ready\":%s,\"udp_sent\":%u,\"udp_fail\":%u,\"udp_ack\":%u,"
             "\"udp_hello\":%u,\"udp_slow\":%s,\"udp_errno\":%d,\"udp_ack_ms\":%lld",
             terminal_udp_ready() ? "true" : "false", (unsigned)s_sent, (unsigned)s_fail,
             (unsigned)s_acks, (unsigned)s_hello, s_slow ? "true" : "false",
             s_last_errno, (long long)s_ack_ms);
}