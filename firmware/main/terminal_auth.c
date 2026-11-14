// main/terminal_auth.c — 握手状态机、HMAC-SHA256、设备密钥存储。
#include "terminal_auth.h"

#include "cJSON.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "mbedtls/md.h"
#include "nvs.h"
#include "nvs_flash.h"

#include <stdio.h>
#include <string.h>

static const char *TAG = "auth";

#define NVS_NS "pavauth"
#define NONCE_BYTES 16
#define SECRET_MAX 128
#define HANDSHAKE_TIMEOUT_US (5 * 1000 * 1000)
#define HELLO_FALLBACK_US (2 * 1000 * 1000)  // 未 provision 时 hello 等这么久就退化

typedef enum { ST_IDLE = 0, ST_WAIT_HELLO, ST_WAIT_AUTH, ST_READY, ST_FAILED } auth_state_t;

static uint8_t s_secret[SECRET_MAX];
static size_t s_secret_len;
static char s_dev_id[16];
static char s_dev_nonce[NONCE_BYTES * 2 + 1];
static char s_srv_nonce[NONCE_BYTES * 2 + 1];
static char s_session_id[17];
static int s_audio_port;
static auth_state_t s_state;
static bool s_anon_ok;  // 未 provision 时走 §11 兼容模式（开发用）
static int64_t s_deadline_us;
static int s_req_id = 1;
static int s_unauth_hits;  // 未认证业务命令计数，3 次即关连接（§3.2）

static void hex_encode(const uint8_t *in, size_t n, char *out) {
    static const char *d = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[i * 2] = d[in[i] >> 4];
        out[i * 2 + 1] = d[in[i] & 0x0f];
    }
    out[n * 2] = '\0';
}

static int hex_val(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// 全部是十六进制且长度为偶数才按 hex 解释。这个判据是确定的，并且 provision 的
// 应答会把实际采用的解释方式回给对端，免得两边各猜一种编码、最后只看到 auth_failed。
static bool is_hex_string(const char *s) {
    const size_t n = strlen(s);
    if (n < 32 || (n & 1)) return false;
    for (size_t i = 0; i < n; i++)
        if (hex_val(s[i]) < 0) return false;
    return true;
}

static bool nvs_load_secret(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return false;
    size_t len = sizeof s_secret;
    esp_err_t err = nvs_get_blob(h, "secret", s_secret, &len);
    nvs_close(h);
    if (err != ESP_OK || len == 0) {
        s_secret_len = 0;
        return false;
    }
    s_secret_len = len;
    return true;
}

void terminal_auth_init(void) {
    // device_id 只是标识不是凭据，但必须稳定：MAC 出厂烧写，跨重刷不变。
    uint8_t mac[6] = {0};
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) == ESP_OK) {
        snprintf(s_dev_id, sizeof s_dev_id, "psp-%02x%02x%02x", mac[3], mac[4], mac[5]);
    } else {
        snprintf(s_dev_id, sizeof s_dev_id, "psp-000000");
    }
    nvs_load_secret();
    ESP_LOGI(TAG, "%s %s", s_dev_id, s_secret_len ? "已 provision" : "未 provision（兼容模式）");
}

bool terminal_auth_has_secret(void) { return s_secret_len > 0; }
const char *terminal_auth_device_id(void) { return s_dev_id; }
const char *terminal_auth_session_id(void) { return s_session_id; }
int terminal_auth_audio_port(void) { return s_audio_port; }

static bool hmac_hex(const char *msg, char out[65]) {
    if (s_secret_len == 0) return false;
    uint8_t mac[32];
    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!info) return false;
    if (mbedtls_md_hmac(info, s_secret, s_secret_len, (const uint8_t *)msg, strlen(msg), mac) != 0)
        return false;
    hex_encode(mac, sizeof mac, out);
    return true;
}

static void build_canonical(char *out, size_t cap, const char *role) {
    snprintf(out, cap, "PAV1AUTH|%s|%s|%s|%s", role, s_dev_id, s_dev_nonce, s_srv_nonce);
}

int terminal_auth_provision(const char *secret, const char *device_id_override,
                            const char **enc) {
    if (!secret) return -1;
    const size_t n = strlen(secret);
    uint8_t buf[SECRET_MAX];
    size_t len = 0;

    if (is_hex_string(secret)) {
        for (size_t i = 0; i < n; i += 2)
            buf[len++] = (uint8_t)((hex_val(secret[i]) << 4) | hex_val(secret[i + 1]));
        if (enc) *enc = "hex";
    } else {
        if (n >= sizeof buf) return -1;
        memcpy(buf, secret, n);
        len = n;
        if (enc) *enc = "raw";
    }

    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return -1;
    esp_err_t err = nvs_set_blob(h, "secret", buf, len);
    if (err == ESP_OK && device_id_override && *device_id_override) {
        // 设备名与 id 分开存：id 由 MAC 决定，这里只是让对端可以对号入座。
        nvs_set_str(h, "label", device_id_override);
    }
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    if (err != ESP_OK) return -1;

    memcpy(s_secret, buf, len);
    s_secret_len = len;
    // 密钥材料用完即抹，别在栈上留着。
    memset(buf, 0, sizeof buf);
    return (int)len;
}

bool terminal_auth_clear(void) {
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) return false;
    err = nvs_erase_key(h, "secret");
    if (err == ESP_OK || err == ESP_ERR_NVS_NOT_FOUND) err = nvs_commit(h);
    nvs_close(h);
    memset(s_secret, 0, sizeof s_secret);
    s_secret_len = 0;
    s_state = ST_IDLE;
    ESP_LOGW(TAG, "密钥已清除，回到兼容模式");
    return err == ESP_OK;
}

bool terminal_auth_begin(char *out, size_t cap) {
    uint8_t rnd[NONCE_BYTES];
    esp_fill_random(rnd, sizeof rnd);
    hex_encode(rnd, sizeof rnd, s_dev_nonce);
    s_srv_nonce[0] = '\0';
    s_session_id[0] = '\0';
    s_audio_port = 0;
    s_anon_ok = false;
    s_req_id = 1;
    s_unauth_hits = 0;
    s_state = ST_WAIT_HELLO;
    s_deadline_us = esp_timer_get_time() + HANDSHAKE_TIMEOUT_US;
    snprintf(out, cap,
             "{\"id\":%d,\"cmd\":\"hello\",\"proto\":\"v1\",\"device_id\":\"%s\",\"nonce\":\"%s\"}\n",
             s_req_id, s_dev_id, s_dev_nonce);
    return true;
}

auth_act_t terminal_auth_on_reply(const struct cJSON *root, char *out, size_t cap) {
    const cJSON *cmd = cJSON_GetObjectItemCaseSensitive(root, "cmd");
    if (!cJSON_IsString(cmd)) return AUTH_ACT_NONE;
    const char *c = cmd->valuestring;
    const cJSON *ok = cJSON_GetObjectItemCaseSensitive(root, "ok");
    const bool is_ok = cJSON_IsTrue(ok);

    if (strcmp(c, "hello") == 0 && s_state == ST_WAIT_HELLO) {
        if (!is_ok) {
            const cJSON *err = cJSON_GetObjectItemCaseSensitive(root, "err");
            ESP_LOGW(TAG, "hello 被拒：%s", cJSON_IsString(err) ? err->valuestring : "?");
            s_state = ST_FAILED;
            return AUTH_ACT_FAILED;
        }
        const cJSON *sn = cJSON_GetObjectItemCaseSensitive(root, "server_nonce");
        const cJSON *sid = cJSON_GetObjectItemCaseSensitive(root, "session_id");
        const cJSON *ap = cJSON_GetObjectItemCaseSensitive(root, "audio_port");
        if (!cJSON_IsString(sn) || strlen(sn->valuestring) != NONCE_BYTES * 2) {
            ESP_LOGW(TAG, "hello 应答缺 server_nonce");
            s_state = ST_FAILED;
            return AUTH_ACT_FAILED;
        }
        snprintf(s_srv_nonce, sizeof s_srv_nonce, "%s", sn->valuestring);
        if (cJSON_IsString(sid)) snprintf(s_session_id, sizeof s_session_id, "%s", sid->valuestring);
        if (cJSON_IsNumber(ap)) s_audio_port = ap->valueint;

        if (!terminal_auth_has_secret()) {
            // §11 兼容模式：开发环境允许无认证直连。仍然要 session_id 才能后续对话，
            // 所以 hello 之后就算会话建立，并在日志里明确标注"未认证"。
            s_state = ST_READY;
            s_anon_ok = true;
            ESP_LOGW(TAG, "未 provision，以兼容模式继续（session=%s）", s_session_id);
            return AUTH_ACT_READY;
        }

        char canon[160];
        char mac[65];
        build_canonical(canon, sizeof canon, "control");
        if (!hmac_hex(canon, mac)) {
            s_state = ST_FAILED;
            return AUTH_ACT_FAILED;
        }
        s_req_id++;
        // nonce 本来就是明文上线的东西（hello/auth 都在裸 TCP 上），打出来不算泄密；
        // 而 MAC 本身绝不落日志，否则等于把一次性凭据抄了一份。
        ESP_LOGI(TAG, "nonce dev=%s srv=%s", s_dev_nonce, s_srv_nonce);
        snprintf(out, cap, "{\"id\":%d,\"cmd\":\"auth\",\"mac\":\"%s\"}\n", s_req_id, mac);
        s_state = ST_WAIT_AUTH;
        ESP_LOGI(TAG, "hello 完成，session=%s audio_port=%d", s_session_id, s_audio_port);
        return AUTH_ACT_SEND;
    }

    if (strcmp(c, "auth") == 0 && s_state == ST_WAIT_AUTH) {
        if (!is_ok) {
            const cJSON *err = cJSON_GetObjectItemCaseSensitive(root, "err");
            ESP_LOGE(TAG, "认证失败：%s（本次会话作废）",
                     cJSON_IsString(err) ? err->valuestring : "?");
            s_state = ST_FAILED;
            return AUTH_ACT_FAILED;
        }
        // 双向认证：服务端也要证明它知道密钥，否则谁都能冒充服务端收走音频。
        const cJSON *sm = cJSON_GetObjectItemCaseSensitive(root, "server_mac");
        char canon[160];
        char expect[65];
        build_canonical(canon, sizeof canon, "server");
        if (!cJSON_IsString(sm) || !hmac_hex(canon, expect) ||
            strcasecmp(sm->valuestring, expect) != 0) {
            ESP_LOGE(TAG, "服务端 server_mac 校验失败，本次会话作废");
            s_state = ST_FAILED;
            return AUTH_ACT_FAILED;
        }
        s_state = ST_READY;
        ESP_LOGI(TAG, "认证通过，session=%s", s_session_id);
        return AUTH_ACT_READY;
    }
    return AUTH_ACT_NONE;
}

bool terminal_auth_authorized(void) { return s_state == ST_READY; }
bool terminal_auth_handshaking(void) { return s_state == ST_WAIT_HELLO || s_state == ST_WAIT_AUTH; }
bool terminal_auth_failed(void) { return s_state == ST_FAILED; }

auth_act_t terminal_auth_tick_step(void) {
    if (s_state == ST_WAIT_HELLO && !terminal_auth_has_secret()) {
        // 未 provision 的设备遇到不识 hello 的服务端（§11 开发环境的旧模式）：
        // 等满 5 s 再失败只会把一条本来能用的链路撞成重连循环。2 s 无应答就退化。
        if (esp_timer_get_time() - (s_deadline_us - HANDSHAKE_TIMEOUT_US) >
            HELLO_FALLBACK_US) {
            s_state = ST_READY;
            s_anon_ok = true;
            ESP_LOGW(TAG, "hello 无应答，退化为无认证兼容模式");
            return AUTH_ACT_READY;
        }
        return AUTH_ACT_NONE;
    }
    if (!terminal_auth_handshaking()) return AUTH_ACT_NONE;
    if (esp_timer_get_time() > s_deadline_us) {
        ESP_LOGW(TAG, "握手超时（5 s），本次会话作废");
        s_state = ST_FAILED;
        return AUTH_ACT_FAILED;
    }
    return AUTH_ACT_NONE;
}

void terminal_auth_reset(void) {
    s_state = ST_IDLE;
    s_session_id[0] = '\0';
    s_srv_nonce[0] = '\0';
    s_audio_port = 0;
    s_anon_ok = false;
    s_unauth_hits = 0;
}