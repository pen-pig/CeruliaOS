#include "tty.h"
#include "port.h"        // outb/inb：光标寄存器要用端口 I/O 设置
#include "serial.h"     // H1a：串口是真机诊断主通道，控制台输出同步镜像 COM1
#include "paging.h"
#include "vga_font.h"

#define VGA_WIDTH  80
#define VGA_HEIGHT 25

static uint16_t* const VGA_MEMORY = (uint16_t*) 0xB8000;

static size_t terminal_row;
static size_t terminal_column;
static uint8_t terminal_color;
static char *g_capture_buf = NULL;
static int g_capture_len = 0;
static int g_capture_max = 0;
static int g_capture_overflow = 0;
static void (*g_gfx_hook)(const char *) = NULL;

void terminal_set_gfx_hook(void (*fn)(const char *)) {
    g_gfx_hook = fn;
}

void terminal_begin_capture(char *buf, int max) {
    g_capture_buf = buf;
    g_capture_len = 0;
    g_capture_overflow = 0;
    g_capture_max = max;
}

int terminal_end_capture(void) {
    int n = g_capture_len;
    /* NUL-terminate so callers may treat buffer as a string */
    if (g_capture_buf && g_capture_len < g_capture_max) g_capture_buf[g_capture_len] = '\0';
    g_capture_buf = NULL;
    g_capture_max = 0;
    return g_capture_overflow ? -1 : n;
}
static uint16_t* terminal_buffer;
static uint16_t gop_cells[VGA_WIDTH * VGA_HEIGHT];
static volatile uint8_t *gop_fb;
static uint32_t gop_stride, gop_width, gop_height;
static uint8_t gop_bpp, gop_fmt;
static int gop_live;
static size_t gop_cursor;
static int view_offset;

static uint32_t gop_color(uint8_t index) {
    static const uint32_t rgb[16] = {
        0x000000, 0x0000aa, 0x00aa00, 0x00aaaa, 0xaa0000, 0xaa00aa, 0xaa5500, 0xaaaaaa,
        0x555555, 0x5555ff, 0x55ff55, 0x55ffff, 0xff5555, 0xff55ff, 0xffff55, 0xffffff,
    };
    uint32_t c = rgb[index & 15u];
    if (gop_bpp == 2)
        return ((c >> 8) & 0xf800u) | ((c >> 5) & 0x7e0u) | ((c >> 3) & 0x1fu);
    if (gop_fmt == 0)
        return ((c & 0xffu) << 16) | (c & 0xff00u) | (c >> 16);
    return c;
}

static void gop_draw(size_t i) {
    uint16_t cell = terminal_buffer[i];
    const unsigned char *glyph = vga_font_8x16 + (uint8_t)cell * 16u;
    uint32_t fg = gop_color((uint8_t)(cell >> 8)), bg = gop_color(cell >> 12);
    uint32_t x = (i % VGA_WIDTH) * 8u, y = (i / VGA_WIDTH) * 16u;
    for (uint32_t row = 0; row < 16; row++) {
        volatile uint8_t *dst = gop_fb + ((y + row) * gop_stride + x) * gop_bpp;
        uint8_t bits = (i == gop_cursor && row >= 14 && view_offset == 0) ? 0xffu : glyph[row];
        for (uint32_t col = 0; col < 8; col++) {
            uint32_t pix = (bits & (0x80u >> col)) ? fg : bg;
            if (gop_bpp == 4) ((volatile uint32_t *)dst)[col] = pix;
            else ((volatile uint16_t *)dst)[col] = (uint16_t)pix;
        }
    }
}

static void gop_redraw(void) {
    if (!gop_live) return;
    for (size_t i = 0; i < VGA_WIDTH * VGA_HEIGHT; i++) gop_draw(i);
}

int terminal_try_gop(void) {
    if (*(volatile uint32_t *)0x5010u != 0x55454649u || !paging_lfb_ready()) return 0;
    outw(0x1ce, 0);                   /* keep working VGA text on Bochs/std VGA */
    if ((inw(0x1cf) & 0xfff0u) == 0xb0c0u) return 0;
    uint32_t fb = *(volatile uint32_t *)0x5000u;
    uint32_t w = *(volatile uint16_t *)0x5004u, h = *(volatile uint16_t *)0x5006u;
    uint8_t bpp = *(volatile uint8_t *)0x5008u, fmt = *(volatile uint8_t *)0x5009u;
    if (w < VGA_WIDTH * 8u || h < VGA_HEIGHT * 16u || (fb & 3u) ||
        !((bpp == 32 && fmt < 2) || (bpp == 16 && fmt == 2))) return 0;
    gop_fb = (volatile uint8_t *)fb;
    gop_width = w;
    gop_height = h;
    gop_stride = *(volatile uint32_t *)0x500cu;
    gop_bpp = bpp / 8u;
    gop_fmt = fmt;
    terminal_initialize();
    return 1;
}

void terminal_suspend_gop(void) { gop_live = 0; }

#define SCROLLBACK_LINES 100
static uint16_t scrollback[SCROLLBACK_LINES][VGA_WIDTH]
    __attribute__((section(".bss.buff")));
static int sb_start = 0;
static int sb_count = 0;
static uint16_t screen_backup[VGA_HEIGHT][VGA_WIDTH]
    __attribute__((section(".bss.buff")));
static int backup_valid = 0;

enum vga_color {
    VGA_COLOR_BLACK = 0,
    VGA_COLOR_BLUE = 1,
    VGA_COLOR_GREEN = 2,
    VGA_COLOR_CYAN = 3,
    VGA_COLOR_RED = 4,
    VGA_COLOR_MAGENTA = 5,
    VGA_COLOR_BROWN = 6,
    VGA_COLOR_LIGHT_GREY = 7,
    VGA_COLOR_DARK_GREY = 8,
    VGA_COLOR_LIGHT_BLUE = 9,
    VGA_COLOR_LIGHT_GREEN = 10,
    VGA_COLOR_LIGHT_CYAN = 11,
    VGA_COLOR_LIGHT_RED = 12,
    VGA_COLOR_LIGHT_MAGENTA = 13,
    VGA_COLOR_LIGHT_BROWN = 14,
    VGA_COLOR_WHITE = 15,
};

static inline uint8_t vga_entry_color(enum vga_color fg, enum vga_color bg) {
    return (uint8_t)(fg | (bg << 4));
}

static inline uint16_t vga_entry(unsigned char uc, uint8_t color) {
    return (uint16_t)uc | ((uint16_t)color << 8);
}

void terminal_clear(void) {
    for (size_t y = 0; y < VGA_HEIGHT; y++) {
        for (size_t x = 0; x < VGA_WIDTH; x++) {
            const size_t index = y * VGA_WIDTH + x;
            terminal_buffer[index] = vga_entry(' ', terminal_color);
        }
    }
    gop_cursor = 0;
    gop_redraw();
    terminal_set_cursor(0, 0);
}

void terminal_initialize(void) {
    terminal_color = vga_entry_color(VGA_COLOR_LIGHT_GREY, VGA_COLOR_BLACK);
    gop_live = gop_fb != NULL;
    terminal_buffer = gop_live ? gop_cells : VGA_MEMORY;
    if (gop_live) {
        for (uint32_t y = 0; y < gop_height; y++) {
            volatile uint8_t *row = gop_fb + y * gop_stride * gop_bpp;
            for (uint32_t i = 0; i < gop_width; i++) {
                if (gop_bpp == 4) ((volatile uint32_t *)row)[i] = 0;
                else ((volatile uint16_t *)row)[i] = 0;
            }
        }
    }
    terminal_clear();
}

void terminal_reset_session(void) {
    sb_start = sb_count = view_offset = backup_valid = 0;
    terminal_initialize();
}

void terminal_setcolor(uint8_t color) {
    terminal_color = color;
}

void terminal_putentryat(char c, uint8_t color, size_t x, size_t y) {
    const size_t index = y * VGA_WIDTH + x;
    terminal_buffer[index] = vga_entry(c, color);
    if (gop_live) gop_draw(index);
}

void terminal_scroll(void) {
    // 滚屏前先把被顶掉的那一行存进 scrollback
    for (size_t x = 0; x < VGA_WIDTH; x++) {
        scrollback[sb_start][x] = terminal_buffer[x];
    }
    sb_start = (sb_start + 1) % SCROLLBACK_LINES;
    if (sb_count < SCROLLBACK_LINES) sb_count++;

    for (size_t y = 0; y < VGA_HEIGHT - 1; y++) {
        for (size_t x = 0; x < VGA_WIDTH; x++) {
            terminal_buffer[y * VGA_WIDTH + x] = terminal_buffer[(y + 1) * VGA_WIDTH + x];
        }
    }
    for (size_t x = 0; x < VGA_WIDTH; x++) {
        terminal_buffer[(VGA_HEIGHT - 1) * VGA_WIDTH + x] = vga_entry(' ', terminal_color);
    }
    gop_redraw();
}

void terminal_putchar(char c) {
    /* 捕获模式：只写缓冲区，不写屏幕 */
    if (g_capture_buf) {
        if (g_capture_len < g_capture_max - 1) {
            g_capture_buf[g_capture_len++] = c;
        } else g_capture_overflow = 1;
        return;
    }
    /* H1a：控制台输出同步镜像到 COM1——真机无显存时仍能看到全部交互，
     *      也是 AHCI 等驱动的 E2E 测试在无屏 QEMU 下读取结果的通道。
     *      未初始化/自检失败时 serial_putc 为无害 no-op，不影响 VGA 渲染。 */
    if (serial_ready()) serial_putc(c);
    /* 图形模式：putchar 的输出也统一喂给 GUI 终端窗口 hook（单字符转串）。
     * 此前 hook 只挂在 terminal_writestring——help 的逐字符输出、
     * print_highlighted、sysinfo、所有用户进程 stdout（fd.c 的 STDOUT 走
     * terminal_write→putchar）在图形模式下全被旁路丢失。 */
    if (g_gfx_hook) {
        char b[2];
        b[0] = c;
        b[1] = 0;
        g_gfx_hook(b);
        return;
    }
    if (c == '\n') {
        terminal_column = 0;
        if (++terminal_row == VGA_HEIGHT) {
            terminal_scroll();
            terminal_row = VGA_HEIGHT - 1;
        }
        terminal_set_cursor(terminal_row, terminal_column);
        return;
    }
    /* 回车（'\r'）= 回到行首。不处理的话 0x0D 会被当普通字符
     * 画进文本缓冲（VGA 字体里是个奇怪的符号）：串口日志看着正常，
     * 屏幕上却一行行乱码。 */
    if (c == '\r') {
        terminal_column = 0;
        terminal_set_cursor(terminal_row, terminal_column);
        return;
    }
    if (c == '\t') {
        terminal_column = (terminal_column + 4) & ~3;
        if (terminal_column >= VGA_WIDTH) {
            terminal_column = 0;
            if (++terminal_row == VGA_HEIGHT) {
                terminal_scroll();
                terminal_row = VGA_HEIGHT - 1;
            }
        }
        terminal_set_cursor(terminal_row, terminal_column);
        return;
    }
    if (c == '\b') {
        if (terminal_column > 0) {
            terminal_column--;
            terminal_putentryat(' ', terminal_color, terminal_column, terminal_row);
            terminal_set_cursor(terminal_row, terminal_column);
        }
        return;
    }

    terminal_putentryat(c, terminal_color, terminal_column, terminal_row);
    if (++terminal_column == VGA_WIDTH) {
        terminal_column = 0;
        if (++terminal_row == VGA_HEIGHT) {
            terminal_scroll();
            terminal_row = VGA_HEIGHT - 1;
        }
    }
    terminal_set_cursor(terminal_row, terminal_column);
}

void terminal_write(const char* data, size_t size) {
    for (size_t i = 0; i < size; i++)
        terminal_putchar(data[i]);
}

void terminal_writestring(const char* data) {
    if (g_gfx_hook && !g_capture_buf) {
        g_gfx_hook(data);
        return;
    }
    for (size_t i = 0; data[i] != '\0'; i++) {
        terminal_putchar(data[i]);
    }
}

void terminal_set_cursor(size_t row, size_t col) {
    if (row >= VGA_HEIGHT) row = VGA_HEIGHT - 1;
    if (col >= VGA_WIDTH) col = VGA_WIDTH - 1;
    uint16_t pos = (uint16_t)(row * VGA_WIDTH + col);
    if (gop_live) {
        size_t old = gop_cursor;
        gop_cursor = pos;
        if (old != pos) gop_draw(old);
        gop_draw(pos);
    } else if (!gop_fb) {
        outb(0x3D4, 0x0F);
        outb(0x3D5, (uint8_t)(pos & 0xFF));
        outb(0x3D4, 0x0E);
        outb(0x3D5, (uint8_t)((pos >> 8) & 0xFF));
    }
    // 同步内部的行列状态
    terminal_row = row;
    terminal_column = col;
}

void terminal_clear_line(size_t row) {
    /* row 越界会写穿 VGA 缓冲。shell 折行重绘按"当前行 + 偏移"算行数，
     * 算错就直接越界写——宁可什么都不做也不能写出去。 */
    if (row >= VGA_HEIGHT) return;
    for (size_t col = 0; col < VGA_WIDTH; col++) {
        terminal_putentryat(' ', terminal_color, col, row);
    }
    terminal_set_cursor(row, 0);
}

size_t terminal_get_row(void) {
    return terminal_row;
}

size_t terminal_get_column(void) {
    return terminal_column;
}

void terminal_set_row(size_t row) {
    if (row >= VGA_HEIGHT) row = VGA_HEIGHT - 1;
    terminal_row = row;
    terminal_column = 0;
}

void terminal_scroll_n(uint32_t n) {
    for (uint32_t i = 0; i < n; i++) terminal_scroll();
}

// 从 scrollback 里取倒数第 k 行开始的整屏内容，重新渲染到屏幕
static void render_scrollback(int k) {
    for (int y = 0; y < VGA_HEIGHT; y++) {
        int seq = sb_count - k + y;
        if (seq < sb_count) {
            int idx = (sb_start - sb_count + seq) % SCROLLBACK_LINES;
            if (idx < 0) idx += SCROLLBACK_LINES;
            for (int x = 0; x < VGA_WIDTH; x++) {
                terminal_buffer[y * VGA_WIDTH + x] = scrollback[idx][x];
            }
        } else {
            int src_y = y - k;
            if (src_y >= 0 && src_y < VGA_HEIGHT && backup_valid) {
                for (int x = 0; x < VGA_WIDTH; x++) {
                    terminal_buffer[y * VGA_WIDTH + x] = screen_backup[src_y][x];
                }
            } else {
                for (int x = 0; x < VGA_WIDTH; x++) {
                    terminal_buffer[y * VGA_WIDTH + x] = vga_entry(' ', terminal_color);
                }
            }
        }
    }
    gop_redraw();
}

void terminal_scroll_reset(void) {
    if (backup_valid) {
        for (int y = 0; y < VGA_HEIGHT; y++) {
            for (int x = 0; x < VGA_WIDTH; x++) {
                terminal_buffer[y * VGA_WIDTH + x] = screen_backup[y][x];
            }
        }
        backup_valid = 0;
    }
    view_offset = 0;
    gop_redraw();
}

void terminal_scroll_up(void) {
    if (sb_count == 0) return;
    if (view_offset == 0) {
        for (int y = 0; y < VGA_HEIGHT; y++) {
            for (int x = 0; x < VGA_WIDTH; x++) {
                screen_backup[y][x] = terminal_buffer[y * VGA_WIDTH + x];
            }
        }
        backup_valid = 1;
    }
    if (view_offset < sb_count) {
        view_offset += 1;
    }
    render_scrollback(view_offset);
}

void terminal_scroll_down(void) {
    if (view_offset > 0) {
        view_offset -= 1;
        if (view_offset == 0) {
            terminal_scroll_reset();
        } else {
            render_scrollback(view_offset);
        }
    } else {
        terminal_scroll_reset();
    }
}

int terminal_in_scrollback(void) {
    return view_offset > 0;
}
