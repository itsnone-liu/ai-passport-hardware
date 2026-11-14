// main/terminal_ui.c — dsh 终端的界面实现。
//
// 布局（240x320 竖屏，圆角蒙版 30px 会切掉四角，内容保持在 x[10..230]/y[8..312] 内）：
//   顶栏   : 品牌 "dsh" + 链路状态徽标
//   消息区 : 服务器下发的文本，自动换行
//   底栏   : 电量 / 音量 / 运行时长
//
// 视觉是自定的深色终端风格，与 BSP demo 的像素卡片菜单无关。
#include "terminal_ui.h"

#include "bsp_display.h"
#include "lvgl.h"
#include "esp_log.h"

static const char *TAG = "ui";

#define COL_BG      0x0B0F14
#define COL_PANEL   0x121A22
#define COL_BORDER  0x1E2A36
#define COL_ACCENT  0x108AEF
#define COL_TEXT    0xE6EDF3
#define COL_DIM     0x8B98A5
#define COL_WARN    0xF0A020

static lv_obj_t *s_msg;
static lv_obj_t *s_lbl_batt;
static lv_obj_t *s_lbl_vol;
static lv_obj_t *s_lbl_up;
static lv_obj_t *s_lbl_link;

static int s_volume = 60;
static int s_brightness = 80;

static lv_obj_t *panel(lv_obj_t *parent, int x, int y, int w, int h, uint32_t color, uint32_t border) {
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_pos(o, x, y);
    lv_obj_set_size(o, w, h);
    lv_obj_set_style_bg_color(o, lv_color_hex(color), 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(o, 8, 0);
    lv_obj_set_style_border_width(o, border ? 1 : 0, 0);
    lv_obj_set_style_border_color(o, lv_color_hex(COL_BORDER), 0);
    lv_obj_set_style_pad_all(o, 0, 0);
    return o;
}

static lv_obj_t *label(lv_obj_t *parent, const char *text, const lv_font_t *font, uint32_t color) {
    lv_obj_t *l = lv_label_create(parent);
    lv_label_set_text(l, text);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    return l;
}

void terminal_ui_create(void) {
    lv_obj_t *scr = lv_obj_create(NULL);
    lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(scr, lv_color_hex(COL_BG), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    // ---- 顶栏 ----
    lv_obj_t *top = panel(scr, 10, 8, 220, 36, COL_ACCENT, 0);
    lv_obj_t *title = label(top, "dsh", &lv_font_montserrat_20, 0xFFFFFF);
    lv_obj_align(title, LV_ALIGN_LEFT_MID, 12, 0);
    s_lbl_link = label(top, "USB --", &lv_font_montserrat_14, 0xFFFFFF);
    lv_obj_align(s_lbl_link, LV_ALIGN_RIGHT_MID, -12, 0);

    // ---- 消息区 ----
    lv_obj_t *body = panel(scr, 10, 52, 220, 200, COL_PANEL, 1);
    s_msg = label(body, "Hello dsh", &lv_font_montserrat_14, COL_TEXT);
    lv_label_set_long_mode(s_msg, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(s_msg, 192);
    lv_obj_set_style_text_align(s_msg, LV_TEXT_ALIGN_LEFT, 0);
    lv_obj_align(s_msg, LV_ALIGN_TOP_LEFT, 12, 12);

    // ---- 底栏三格 ----
    lv_obj_t *b1 = panel(scr, 10, 260, 68, 40, COL_PANEL, 1);
    lv_obj_t *c1 = label(b1, "BAT", &lv_font_montserrat_14, COL_DIM);
    lv_obj_align(c1, LV_ALIGN_TOP_MID, 0, 4);
    s_lbl_batt = label(b1, "--", &lv_font_montserrat_14, COL_TEXT);
    lv_obj_align(s_lbl_batt, LV_ALIGN_BOTTOM_MID, 0, -4);

    lv_obj_t *b2 = panel(scr, 86, 260, 68, 40, COL_PANEL, 1);
    lv_obj_t *c2 = label(b2, "VOL", &lv_font_montserrat_14, COL_DIM);
    lv_obj_align(c2, LV_ALIGN_TOP_MID, 0, 4);
    s_lbl_vol = label(b2, "60", &lv_font_montserrat_14, COL_TEXT);
    lv_obj_align(s_lbl_vol, LV_ALIGN_BOTTOM_MID, 0, -4);

    lv_obj_t *b3 = panel(scr, 162, 260, 68, 40, COL_PANEL, 1);
    lv_obj_t *c3 = label(b3, "UP", &lv_font_montserrat_14, COL_DIM);
    lv_obj_align(c3, LV_ALIGN_TOP_MID, 0, 4);
    s_lbl_up = label(b3, "0s", &lv_font_montserrat_14, COL_TEXT);
    lv_obj_align(s_lbl_up, LV_ALIGN_BOTTOM_MID, 0, -4);

    lv_screen_load(scr);
    ESP_LOGI(TAG, "终端界面已建立");
}

void terminal_ui_set_message(const char *text) {
    if (!s_msg) return;
    lv_label_set_text(s_msg, (text && *text) ? text : " ");
}

void terminal_ui_refresh(const terminal_status_t *st) {
    if (!st) return;
    if (s_lbl_batt) {
        char buf[24];
        if (st->battery_pct < 0) {
            snprintf(buf, sizeof buf, "--");
            lv_obj_set_style_text_color(s_lbl_batt, lv_color_hex(COL_DIM), 0);
        } else {
            snprintf(buf, sizeof buf, "%d%%", st->battery_pct);
            lv_obj_set_style_text_color(s_lbl_batt,
                lv_color_hex(st->battery_pct <= 20 ? COL_WARN : COL_TEXT), 0);
        }
        lv_label_set_text(s_lbl_batt, buf);
    }
    if (s_lbl_vol) {
        char buf[16];
        snprintf(buf, sizeof buf, "%d", st->volume);
        lv_label_set_text(s_lbl_vol, buf);
    }
    if (s_lbl_up) {
        char buf[24];
        if (st->uptime_s < 100) snprintf(buf, sizeof buf, "%ds", (int)st->uptime_s);
        else if (st->uptime_s < 6000) snprintf(buf, sizeof buf, "%dm", (int)(st->uptime_s / 60));
        else snprintf(buf, sizeof buf, "%dh", (int)(st->uptime_s / 3600));
        lv_label_set_text(s_lbl_up, buf);
    }
    if (s_lbl_link) {
        // 四态：分不清"Wi-Fi 没连上"和"Wi-Fi 通了但服务器不通"是最常见的现场困惑
        const char *txt = "--";
        if (st->link_mode >= UI_LINK_NET) txt = "NET OK";
        else if (st->link_mode == UI_LINK_WIFI) txt = "WIFI";
        else if (st->link_mode == UI_LINK_USB) txt = "USB OK";
        lv_label_set_text(s_lbl_link, txt);
    }
}

int terminal_ui_volume(void) { return s_volume; }
int terminal_ui_brightness(void) { return s_brightness; }

void terminal_ui_set_volume(int value) {
    if (value < 0) value = 0;
    if (value > 100) value = 100;
    s_volume = value;
    if (s_lbl_vol) {
        char buf[16];
        snprintf(buf, sizeof buf, "%d", s_volume);
        lv_label_set_text(s_lbl_vol, buf);
    }
}

void terminal_ui_set_brightness(int value) {
    if (value < 0) value = 0;
    if (value > 100) value = 100;
    s_brightness = value;
}

int terminal_ui_step_volume(int delta) {
    terminal_ui_set_volume(s_volume + delta);
    return s_volume;
}

int terminal_ui_step_brightness(int delta) {
    terminal_ui_set_brightness(s_brightness + delta);
    return s_brightness;
}