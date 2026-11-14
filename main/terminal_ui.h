// main/terminal_ui.h — dsh 终端的显示层（自绘 UI，不复用 demo 菜单外壳）。
// 所有函数必须在持有 LVGL 锁（bsp_lvgl_lock）的前提下调用。
#pragma once

#include <stdbool.h>
#include <stdint.h>

// 链路状态：0 = 无载体，1 = 仅 USB，2 = Wi-Fi 已连但服务器未通，3 = TCP 已连
#define UI_LINK_NONE 0
#define UI_LINK_USB 1
#define UI_LINK_WIFI 2
#define UI_LINK_NET 3

typedef struct {
    int battery_pct;      // -1 = 电量计不可用
    int battery_mv;       // -1 = 读失败
    int volume;           // 0..100
    int brightness;       // 0..100
    int64_t uptime_s;
    int link_mode;        // UI_LINK_*
} terminal_status_t;

void terminal_ui_create(void);
void terminal_ui_set_message(const char *text);
void terminal_ui_refresh(const terminal_status_t *st);

// 本地立即生效的显示（不等服务器回执），返回实际生效值。
int terminal_ui_step_volume(int delta);
int terminal_ui_step_brightness(int delta);
int terminal_ui_volume(void);
int terminal_ui_brightness(void);
void terminal_ui_set_volume(int value);
void terminal_ui_set_brightness(int value);