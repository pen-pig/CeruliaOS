/* Desktop text editor. The disk representation is the document: display wraps
 * are offsets into it, never extra newlines. Included by gfxwin.c for its
 * private window drawing helpers. */
#define NP_CAP 32768
#define NP_ROW_H 18
enum { NP_IDLE, NP_NEW, NP_OPEN, NP_AS, NP_UNICODE, NP_CLOSE };
static char np_buf[NP_CAP], np_read[NP_CAP], np_path[256], np_input[64];
static uint16_t np_starts[NP_CAP + 1]; /* worst case: a newline per byte */
static int np_len, np_pos, np_top, np_rows, np_width, np_menu, np_mode;
static int np_dirty, np_loaded, np_crlf;
static uint32_t np_mount;
static const char *np_status;

static int np_step(int p)
{
    unsigned char b = np_buf[p];
    if (b == '\r') return 2; /* lone CR rejected by np_open_path */
    return b < 128 ? 1 : b < 224 ? 2 : b < 240 ? 3 : 4;
}

static int np_advance(int p, int x)
{
    if (np_buf[p] == '\t') return 32 - x % 32;
    return (unsigned char)np_buf[p] < 128 ? 8 : gfx_text_width_utf8(np_buf+p, np_step(p));
}

static void np_layout(gw_window_t *w)
{
    int width = w->inner_w - 5;
    if (width < 32) width = 32;
    if (np_width == width) return;
    np_width = width; np_starts[0] = 0; np_rows = 1;
    for (int p = 0, x = 0; p < np_len;) {
        int n = np_step(p);
        if (np_buf[p] == '\n' || np_buf[p] == '\r') {
            p += n; np_starts[np_rows++] = p; x = 0;
        } else {
            int dx = np_advance(p, x);
            if (x && x + dx > width) {
                np_starts[np_rows++] = p; x = 0; dx = np_advance(p, 0);
            }
            p += n; x += dx;
        }
    }
}

static int np_end(int r)
{
    int e = r+1 < np_rows ? np_starts[r+1] : np_len;
    if (e > np_starts[r] && np_buf[e-1] == '\n') {
        e--;
        if (e > np_starts[r] && np_buf[e-1] == '\r') e--;
    }
    return e;
}

static int np_row_at(int p)
{
    int lo = 0, hi = np_rows;
    while (lo+1 < hi) {
        int mid = (lo+hi)/2;
        if (np_starts[mid] <= p) lo = mid; else hi = mid;
    }
    return lo;
}

static int np_x(int row, int pos)
{
    int x = 0;
    for (int p = np_starts[row]; p < pos; p += np_step(p)) x += np_advance(p, x);
    return x;
}

static int np_pick(int r, int target)
{
    int p = np_starts[r], end = np_end(r);
    for (int x = 0; p < end;) {
        int dx = np_advance(p, x), next = p + np_step(p);
        if (target < x + dx/2 || (r+1 < np_rows && next == np_starts[r+1])) break;
        p = next; x += dx;
    }
    return p;
}

static int np_vis(gw_window_t *w)
{
    int n = (w->inner_h - 15) / NP_ROW_H;
    return n > 0 ? n : 1;
}

static void np_follow(gw_window_t *w)
{
    np_layout(w);
    int r = np_row_at(np_pos), vis = np_vis(w);
    if (np_top > r) np_top = r;
    if (r >= np_top + vis) np_top = r - vis + 1;
}

static void np_bind(gw_window_t *w, const char *path)
{
    int i = 0, base = 0;
    while (path[i] && i < 255) {
        np_path[i] = path[i]; if (path[i] == '/') base = i+1; i++;
    }
    np_path[i] = 0; np_mount = fs_mount_generation();
    const char *prefix = "Notepad - ";
    int k = 0;
    while (prefix[k]) { w->title[k] = prefix[k]; k++; }
    while (np_path[base] && k < GW_TITLE_MAX-1) w->title[k++] = np_path[base++];
    w->title[k] = 0;
}

static int np_open_path(gw_window_t *w, const char *path)
{
    uint32_t sz, adv; uint8_t dir;
    if (np_dirty) { np_status = "Save edits first"; return 0; }
    if (!*path || fs_stat(path,&sz,&dir) || dir) { np_status = "Open failed"; return 0; }
    if (sz > NP_CAP) { np_status = "Over 32 KiB"; return 0; }
    if (fs_read_file(path,(uint8_t *)np_read,sz) != (int)sz) { np_status = "Read failed"; return 0; }
    int crlf = -1;
    for (uint32_t p = 0; p < sz;) {
        unsigned char c = np_read[p];
        if (c == '\r' && p+1 < sz && np_read[p+1] == '\n') {
            if (crlf < 0) crlf = 1;
            p += 2; continue;
        }
        if (c == '\n' && crlf < 0) crlf = 0;
        if ((c < 32 && c != '\t' && c != '\n') || c == 127 || (c >= 128 &&
            (c < 194 || !cjk_utf8_decode(np_read+p,sz-p,&adv)))) {
            np_status = "Not UTF-8 text"; return 0;
        }
        p += c < 128 ? 1 : adv;
    }
    for (uint32_t i = 0; i < sz; i++) np_buf[i] = np_read[i];
    np_len = sz; np_crlf = crlf == 1; np_loaded = 1;
    np_pos = np_top = np_width = np_mode = np_menu = 0;
    np_bind(w,path); np_status = 0; return 1;
}

static void np_start(gw_window_t *w)
{
    char path[256] = ""; uint32_t sz; uint8_t dir;
    np_len = np_pos = np_width = np_top = np_loaded = np_dirty = np_crlf = np_mode = np_menu = 0;
    np_status = 0;
    if (fs_absolute_path("NOTE.TXT",path,sizeof(path))) path[0] = 0;
    np_bind(w,path);
    if (*path && !fs_stat(path,&sz,&dir)) np_open_path(w,path);
}

static void np_edit_file(const char *path, uint32_t mount)
{
    gw_launch_app(GW_APP_NOTEPAD);
    gw_window_t *w = gw_focused_wnd;
    if (!w || w->app != GW_APP_NOTEPAD) return;
    np_mode = np_menu = 0;
    if (mount != fs_mount_generation()) np_status = "Volume changed";
    else np_open_path(w,path);
}

static void np_save(gw_window_t *w, const char *path, int as)
{
    uint32_t sz; uint8_t dir;
    if (!as && np_mount != fs_mount_generation()) { np_status = "Volume changed"; return; }
    if (!*path) { np_status = "No file name"; return; }
    if ((as || !np_loaded) && !fs_stat(path,&sz,&dir)) { np_status = "File exists"; return; }
    if (fs_create_file(path,(const uint8_t *)np_buf,np_len)) { np_status = "Save failed"; return; }
    if (as) np_bind(w,path);
    np_loaded = 1; np_dirty = 0; np_status = "Saved";
}

static void np_insert(const char *s, int n)
{
    if (n > NP_CAP - np_len) { np_status = "Document full"; return; }
    for (int i = np_len; i > np_pos; i--) np_buf[i+n-1] = np_buf[i-1];
    for (int i = 0; i < n; i++) np_buf[np_pos++] = s[i];
    np_len += n; np_width = 0; np_dirty = 1; np_status = 0;
}

static void np_prompt(int mode)
{
    np_mode = mode; np_menu = 0; np_input[0] = 0; np_status = 0;
}

static void np_submit(gw_window_t *w)
{
    char path[256]; int mode = np_mode; np_mode = NP_IDLE;
    if (mode == NP_UNICODE) {
        unsigned cp = 0; int len = gw_strlen(np_input);
        for (int i = 0; i < len; i++) {
            int c = np_input[i];
            int d = c >= '0' && c <= '9' ? c-'0' : (c|32)-'a'+10;
            if (len > 6 || !((c >= '0' && c <= '9') || ((c|32) >= 'a' && (c|32) <= 'f'))) {
                np_status = "Invalid code"; return;
            }
            cp = cp*16+d;
        }
        if (cp < 32 || cp == 127 || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) {
            np_status = "Invalid code"; return;
        }
        char b[4]; int n = cp < 128 ? 1 : cp < 2048 ? 2 : cp < 65536 ? 3 : 4;
        for (int i = n-1; i > 0; i--) { b[i] = 128 | (cp & 63); cp >>= 6; }
        b[0] = cp | (n==1 ? 0 : n==2 ? 192 : n==3 ? 224 : 240);
        np_insert(b,n); return;
    }
    if (fs_absolute_path(np_input,path,sizeof(path))) { np_status = "Invalid name"; return; }
    if (mode == NP_OPEN) { np_open_path(w,path); return; }
    /* New files use the portable 8.3 subset: FAT must never shorten an
     * entered name behind the user's back. Open accepts existing paths. */
    int base = 0, ext = 0, dot = 0;
    for (int i = 0; np_input[i]; i++) {
        int c = np_input[i];
        if (c == '.' && base && !dot) { dot = 1; continue; }
        if (!((c >= '0' && c <= '9') || ((c|32) >= 'a' && (c|32) <= 'z') || c=='-' || c=='_') ||
            (dot ? ++ext > 3 : ++base > 8)) { np_status = "Use an 8.3 name"; return; }
    }
    if (!base || (dot && !ext)) { np_status = "Use an 8.3 name"; return; }
    if (mode == NP_AS) { np_save(w,path,1); return; }
    uint32_t sz; uint8_t dir;
    if (!fs_stat(path,&sz,&dir)) { np_status = "File exists"; return; }
    np_bind(w,path); np_len = np_pos = np_top = np_width = np_loaded = np_crlf = 0;
    np_dirty = 1; np_status = 0;
}

static void notepad_key(gw_window_t *w, int key)
{
    if (np_mode == NP_CLOSE) {
        if (key == 'y' || key == 'Y') { np_dirty = 0; np_mode = 0; gw_close(w); }
        else { np_mode = 0; np_status = 0; }
        return;
    }
    if (np_mode) {
        int n = gw_strlen(np_input);
        if (key == 27) { np_mode = 0; np_status = 0; }
        else if ((key == 8 || key == 127) && n) np_input[n-1] = 0;
        else if (key == '\n' || key == '\r') np_submit(w);
        else if (key >= 32 && key < 127 && n+1 < (int)sizeof(np_input)) {
            np_input[n] = key; np_input[n+1] = 0;
        }
        return;
    }
    np_menu = 0;
    if (key == KEY_F2) { np_prompt(NP_UNICODE); return; }
    if (key == KEY_F3 || key == 19) { np_save(w,np_path,0); return; }
    if ((key >= 32 && key < 127) || key == '\t') { char c = key; np_insert(&c,1); return; }
    if (key == '\r' || key == '\n') { np_insert(np_crlf ? "\r\n" : "\n",np_crlf ? 2 : 1); return; }
    if (key == KEY_LEFT || key == 8 || key == 127) {
        if (!np_pos) return;
        int old = np_pos--;
        while (np_pos && ((unsigned char)np_buf[np_pos] & 192) == 128) np_pos--;
        if (np_pos && np_buf[np_pos] == '\n' && np_buf[np_pos-1] == '\r') np_pos--;
        if (key != KEY_LEFT) {
            for (int i = old; i < np_len; i++) np_buf[np_pos+i-old] = np_buf[i];
            np_len -= old-np_pos; np_dirty = 1; np_width = 0; np_status = 0;
        }
    } else if (key == KEY_RIGHT) { if (np_pos < np_len) np_pos += np_step(np_pos); }
    else {
        np_layout(w); int r = np_row_at(np_pos);
        if (key == KEY_HOME) np_pos = np_starts[r];
        else if (key == KEY_END) np_pos = np_pick(r,np_width+32);
        else {
            int delta = key == KEY_UP ? -1 : key == KEY_DOWN ? 1 :
                        key == KEY_PGUP ? -np_vis(w) : key == KEY_PGDN ? np_vis(w) : 0;
            if (!delta) return;
            int dest = r + delta;
            if (dest < 0) dest = 0;
            if (dest >= np_rows) dest = np_rows-1;
            np_pos = np_pick(dest,np_x(r,np_pos));
        }
    }
}

static const char *const np_files[] = {"New...", "Open...", "Save (F3)", "Save as..."};
static const char *const np_edits[] = {"Start", "End", "Unicode (F2)"};

static void notepad_draw(gw_window_t *w)
{
    int ox = gw_ox(w), oy = gw_oy(w), iw = w->inner_w;
    np_follow(w);
    gw_fill(ox,oy,iw,14,0xF0F0F0u);
    gfx_draw_text(ox+4,oy+3,"File",0,np_menu==1 ? 14 : 15);
    gfx_draw_text(ox+36,oy+3,"Edit",0,np_menu==2 ? 14 : 15);
    const char *s = np_status ? np_status : np_dirty ? "* Unsaved" : "";
    int room = (iw-68)/8;
    if (room > 0) gfx_draw_text_utf8(ox+iw-4-8*(gw_strlen(s)<room ? gw_strlen(s) : room),oy+3,s,
                                      gw_strlen(s)<room ? gw_strlen(s) : room,0,15);
    gw_fill(ox,oy+15,iw,w->inner_h-15,CLR_EDIT_BG);
    int cur = np_row_at(np_pos);
    for (int i = 0; i < np_vis(w) && np_top+i < np_rows; i++) {
        int r = np_top+i, y = oy+16+i*NP_ROW_H, end = np_end(r);
        if (r == cur) gw_fill(ox,y-1,iw,NP_ROW_H,0xF0F7FDu);
        for (int p = np_starts[r], x = 0; p < end;) {
            int n = np_step(p);
            if (np_buf[p] != '\t') gfx_draw_text_utf8(ox+2+x,y,np_buf+p,n,0,r==cur ? 14 : 15);
            x += np_advance(p,x); p += n;
        }
        if (r == cur) gw_fill(ox+2+np_x(r,np_pos),y,1,8,0x0078D7u);
    }
    if (np_mode) {
        const char *label = np_mode == NP_OPEN ? "Open path:" : np_mode == NP_AS ? "Save as (8.3):" :
            np_mode == NP_NEW ? "New name (8.3):" : np_mode == NP_CLOSE ? "Discard edits? Y/N" : "Unicode hex (U+):";
        gw_fill(ox,oy+15,iw,34,0xF0F0F0u);
        gfx_draw_text(ox+3,oy+17,label,0,15);
        int skip = gw_strlen(np_input) - (iw-8)/8;
        gfx_draw_text(ox+3,oy+32,np_input+(skip>0 ? skip : 0),0,15);
    } else if (np_menu) {
        int x = ox+(np_menu==1 ? 2 : 34), count = np_menu==1 ? 4 : 3;
        gw_fill(x,oy+14,104,count*16+2,0xF0F0F0u);
        for (int i = 0; i < count; i++)
            gfx_draw_text(x+4,oy+18+i*16,np_menu==1 ? np_files[i] : np_edits[i],0,15);
    }
}

static void notepad_click(gw_window_t *w, int lx, int ly)
{
    if (np_mode) return;
    if (ly < 14) {
        int m = lx < 34 ? 1 : lx < 66 ? 2 : 0;
        np_menu = m == np_menu ? 0 : m; return;
    }
    if (np_menu) {
        int m = np_menu, left = m==1 ? 2 : 34, i = (ly-14)/16;
        np_menu = 0;
        if (lx < left || lx >= left+104 || i >= (m==1 ? 4 : 3)) return;
        if (m==2) {
            if (i==2) np_prompt(NP_UNICODE); else np_pos = i==0 ? 0 : np_len;
        } else if (i==2) np_save(w,np_path,0);
        else if (i!=3 && np_dirty) np_status = "Save edits first";
        else np_prompt(i==0 ? NP_NEW : i==1 ? NP_OPEN : NP_AS);
        return;
    }
    if (ly >= 15) {
        np_follow(w);
        int r = np_top + (ly-15)/NP_ROW_H;
        np_pos = r < np_rows ? np_pick(r,lx-2) : np_len;
    }
}
