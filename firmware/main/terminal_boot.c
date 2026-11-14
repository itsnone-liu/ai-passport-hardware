// main/terminal_boot.c —— 复位取证：重启了几次、为什么、上次运行的最小空闲堆。
//
// 为什么需要这个：两端协作时，"设备重启了"是最容易被误判的现象。一方的测试脚本
// 拉一下 DTR 就是一次复位，看起来却像固件崩了；反过来，真崩了也可能被当成工具
// 干扰。没有凭证时，双方只能互相猜。这里把三类事实钉在 RTC 内存里（软复位/
// WDT/panic 都保留，只有彻底断电才丢）：
//   1. 本次复位原因 —— esp_reset_reason()，区分 panic / 看门狗 / 外部复位；
//   2. 启动计数 —— 与串口日志对得上，能判断"中间有没有偷偷重启"；
//   3. 上次运行末尾的最小空闲堆 —— panic 前撑到多低，直接指向 OOM。
#include "terminal_boot.h"

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "boot";

// RTC_NOINIT：.rtc_noinit 段不会被启动代码清零，复位后原值还在。
// 用魔数区分"这是复位后继承的值"和"这是掉电后的一堆随机数"。
#define BOOT_MAGIC 0x50415631u  // "PAV1"
RTC_NOINIT_ATTR static uint32_t s_magic;
RTC_NOINIT_ATTR static uint32_t s_boots;
RTC_NOINIT_ATTR static uint32_t s_prev_heap_min;
RTC_NOINIT_ATTR static uint32_t s_prev_heap_block;
RTC_NOINIT_ATTR static uint32_t s_prev_uptime_s;

static uint32_t s_this_boot;
static uint32_t s_prev_min;
static uint32_t s_prev_block;
static uint32_t s_prev_uptime;
static const char *s_reason_name = "unknown";
static int s_reason_code = -1;

static const char *reason_str(esp_reset_reason_t r) {
    switch (r) {
    case ESP_RST_POWERON: return "poweron";
    case ESP_RST_EXT: return "ext_pin";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT: return "wdt";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    // 下面几个在部分 IDF 版本里没有枚举名，用数值写死；不写出来就会一律显示 unknown，
    // 而 11 恰好是"从 USB 侧复位"，也就是刷机——这正是最容易被误当成崩溃的一种。
    case 11: return "usb";
    case 12: return "jtag";
    case 13: return "efuse";
    case 14: return "pwr_glitch";
    case 15: return "cpu_lockup";
    default: return "unknown";
    }
}

// 每 2 s 把当前最低堆写进 RTC 变量：崩溃发生时不给我们机会保存，
// 所以只能持续"预写"。
static void boot_keepalive(void *arg) {
    (void)arg;
    for (;;) {
        s_prev_heap_min = esp_get_minimum_free_heap_size();
        multi_heap_info_t info;
        heap_caps_get_info(&info, MALLOC_CAP_8BIT);
        s_prev_heap_block = (uint32_t)info.largest_free_block;
        s_prev_uptime = (uint32_t)(esp_timer_get_time() / 1000000);
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}

void terminal_boot_record(void) {
    const esp_reset_reason_t r = esp_reset_reason();
    s_reason_name = reason_str(r);
    s_reason_code = (int)r;
    if (s_magic == BOOT_MAGIC) {
        s_boots++;
        s_prev_min = s_prev_heap_min;
        s_prev_block = s_prev_heap_block;
        s_prev_uptime = s_prev_uptime;
    } else {
        // 首次上电（或掉电过）：RTC 内存里的东西不可信，从 1 开始。
        s_magic = BOOT_MAGIC;
        s_boots = 1;
        s_prev_min = 0;
        s_prev_block = 0;
        s_prev_uptime = 0;
    }
    s_this_boot = s_boots;
    s_prev_heap_min = esp_get_minimum_free_heap_size();  // 本次启动立刻预写一次
    ESP_LOGI(TAG, "复位原因码=%d", s_reason_code);
    ESP_LOGI(TAG, "第 %u 次启动，复位原因=%s，上次结束时 heap_min=%u block=%u uptime=%us",
             (unsigned)s_this_boot, s_reason_name, (unsigned)s_prev_min,
             (unsigned)s_prev_block, (unsigned)s_prev_uptime);
    xTaskCreate(boot_keepalive, "boot_ka", 2560, NULL, 1, NULL);
}

uint32_t terminal_boot_count(void) { return s_this_boot; }
const char *terminal_boot_reason(void) { return s_reason_name; }
uint32_t terminal_boot_prev_heap_min(void) { return s_prev_min; }
uint32_t terminal_boot_prev_heap_block(void) { return s_prev_block; }
uint32_t terminal_boot_prev_uptime_s(void) { return s_prev_uptime; }
int terminal_boot_reason_code(void) { return s_reason_code; }
