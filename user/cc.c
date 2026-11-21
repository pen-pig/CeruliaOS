/*
  Obfuscated Tiny C Compiler with ELF output

  Copyright (C) 2001-2003 Fabrice Bellard

  This software is provided 'as-is', without any express or implied
  warranty.  In no event will the authors be held liable for any damages
  arising from the use of this software.

  Permission is granted to anyone to use this software for any purpose,
  including commercial applications, and to alter it and redistribute it
  freely, subject to the following restrictions:

  1. The origin of this software must not be misrepresented; you must not
     claim that you wrote the original software. If you use this software
     in a product, an acknowledgment in the product and its documentation
     *is* required.
  2. Altered source versions must be plainly marked as such, and must not be
     misrepresented as being the original software.
  3. This notice may not be removed or altered from any source distribution.
*/
/* Cerulia port: static ELF output and freestanding user-space support.
 * Original: https://bellard.org/otcc/otccelfn.c (Fabrice Bellard).
 * This is an altered version; no Linux dynamic linker is required.
 */
#include "cc_compat.h"

/* vars: value of variables
   loc : local variable index
   glo : global variable ptr
   data: base of data segment
   ind : output code ptr
   prog: output code
   rsym: return symbol
   sym_stk: symbol stack
   dstk: symbol stack pointer

   * 'vars' format:
   For each character TAG_TOK at offset 'i' before a
    symbol in sym_stk, we have:
    v = (int *)(vars + 8 * i + TOK_IDENT)[0]
    p = (int *)(vars + 8 * i + TOK_IDENT)[1]

    v = 0    : undefined symbol, p = list of use points.
    v < LOCAL: offset on stack, p = 0.
    otherwise: symbol with value 'v', p = list of use points.

   * 'sym_stk' format:
   TAG_TOK sym1 TAG_TOK sym2 .... symN '\0'
   'dstk' points to the last '\0'.
*/
static int tok, tokc, tokl, ch, vars, rsym, prog, ind, loc, glo, sym_stk, dstk, data, data_offset;

/* Symbol offsets depend on the initial keyword string. */
#define TOK_STR_SIZE 48
#define TOK_IDENT    0x100
#define TOK_INT      0x100
#define TOK_IF       0x120
#define TOK_ELSE     0x138
#define TOK_WHILE    0x160
#define TOK_BREAK    0x190
#define TOK_RETURN   0x1c0
#define TOK_FOR      0x1f8
#define TOK_LAST_KEYWORD 0x218 /* Keeps upstream symbol offsets stable. */
#define TOK_MAIN     0x250

#define TOK_DUMMY   1
#define TOK_NUM     2
#define TOK_VOID    3

#define LOCAL   0x200

#define TAG_TOK ' '

static void expr(void);
static void decl(int local);
static void unary(int assignment);
static void block(int *break_chain);

static int local_symbols[128];
static int previous_values[128];
static unsigned int locals;
static unsigned int parse_depth;

static void enter_parser(void) {
    if (++parse_depth > 64) error("source is nested too deeply");
}

static void bind_local(int symbol, int value) {
    if (locals == sizeof(local_symbols) / sizeof(local_symbols[0]))
        error("too many local variables");
    local_symbols[locals] = symbol;
    previous_values[locals++] = *(int *)symbol;
    *(int *)symbol = value;
}

static void unbind_locals(unsigned int mark) {
    while (locals > mark) {
        locals--;
        *(int *)local_symbols[locals] = previous_values[locals];
    }
}

static int is_symbol(int token) {
    return token >= vars + TOK_MAIN && token < vars + ALLOC_SIZE * 8;
}

static void pdef(int t)
{
    if (dstk - sym_stk >= ALLOC_SIZE - 32) error("symbol table full");
    *(char *)dstk++ = t;
}

static void inp(void) {
    ch = source[source_pos] ? (unsigned char)source[source_pos++] : -1;
}

static void getq(void) {
    if (ch != '\\') return;
    inp();
    if (ch == 'n') ch = '\n';
    else if (ch == 't') ch = '\t';
    else if (ch == 'r') ch = '\r';
    else if (ch == '0') ch = 0;
    else if (ch != '\\' && ch != '\'' && ch != '"')
        error("unsupported escape sequence");
}

static void next(void) {
    const char *op;
    int first, second;
    int start, offset;

    for (;;) {
        while (isspace(ch)) inp();
        if (ch == '/' && source[source_pos] == '/') {
            while (ch != '\n' && ch != -1) inp();
        } else if (ch == '/' && source[source_pos] == '*') {
            inp();
            inp();
            while (ch != '*' || source[source_pos] != '/') {
                if (ch == -1) error("unterminated comment");
                inp();
            }
            inp();
            inp();
        } else {
            break;
        }
    }

    tokl = 0;
    tok = ch;
    if (ch == -1) return;

    if (isalnum(ch)) {
        if (isdigit(ch)) {
            tokc = parse_number(source + source_pos - 1);
            do { inp(); } while (isalnum(ch));
            tok = TOK_NUM;
            return;
        }
        start = dstk;
        pdef(TAG_TOK);
        do { pdef(ch); inp(); } while (isalnum(ch));
        *(char *)dstk = TAG_TOK;
        *(char *)(dstk + 1) = 0;
        if (dstk - start == 5 && !strncmp((char *)start + 1, "void", 4)) {
            tok = TOK_VOID;
        } else {
            offset = strstr((char *)sym_stk, (char *)start) - (char *)sym_stk;
            tok = offset * 8 + TOK_IDENT;
            if (tok > TOK_LAST_KEYWORD) tok += vars;
            if (offset == start - sym_stk) {
                *(char *)dstk = 0;
                return;
            }
        }
        dstk = start;
        *(char *)dstk = 0;
        return;
    }

    inp();
    if (tok == '\'') {
        if (ch < 0 || ch == '\n' || ch == '\'') error("invalid character literal");
        getq();
        tokc = ch;
        tok = TOK_NUM;
        inp();
        if (ch != '\'') error("invalid character literal");
        inp();
        return;
    }
    if (tok == '#') error("preprocessor directives are unsupported");

    /* OTCC's packed table encodes matching tokens, precedence and x86 opcodes. */
    op = "++#m--%am*@R<^1c/@%[_[H3c%@%[_[H3c+@.B#d-@%:_^BKd<<Z/03e>>`/03e<=0f>=/f<@.f>@1f==&g!=\'g&&k||#l&@.BCh^@.BSi|@.B+j~@/%Yd!@&d*@b";
    while ((first = *op++) != 0) {
        second = *op++;
        tokc = 0;
        while ((tokl = *op++ - 'b') < 0)
            tokc = tokc * 64 + tokl + 64;
        if (first == tok && (second == ch || second == '@')) {
            if (second == ch) {
                inp();
                tok = TOK_DUMMY;
            }
            return;
        }
    }
    tokl = 0;
}

static void skip(int c)
{
    if (tok != c) error("unexpected token");
    next();
}

/* from 0 to 4 bytes */
static void o(int n)
{
    /* cannot use unsigned, so we must do a hack */
    while (n && n != -1) {
        if (ind - prog >= ALLOC_SIZE - 16) error("code buffer full");
        *(char *)ind++ = n;
        n = n >> 8;
    }
}

/* put a 32 bit little endian word 'n' at unaligned address 't' */
static void put32(int t, int n)
{
    *(char *)t++ = n;
    *(char *)t++ = n >> 8;
    *(char *)t++ = n >> 16;
    *(char *)t++ = n >> 24;
}

/* get a 32 bit little endian word at unaligned address 't' */
static int get32(int t)
{
    return  (*(char *)t & 0xff) |
        (*(char *)(t + 1) & 0xff) << 8 |
        (*(char *)(t + 2) & 0xff) << 16 |
        (*(char *)(t + 3) & 0xff) << 24;
}


/* output a symbol and patch all references to it */
static void gsym1(int t, int b)
{
    int n;
    while (t) {
        n = get32(t); /* next value */
        /* patch absolute reference (always mov/lea before) */
        if (*(char *)(t - 1) == 0x05) {
            /* XXX: incorrect if data < 0 */
            if (b >= data && b < glo)
                put32(t, b + data_offset);
            else
                put32(t, b - prog + 0x401000);
        } else {
            put32(t, b - t - 4);
        }
        t = n;
    }
}

static void gsym(int t)
{
    gsym1(t, ind);
}

/* psym is used to put an instruction with a data field which is a
   reference to a symbol. It is in fact the same as oad ! */
#define psym oad

/* instruction + address */
static int oad(int n, int t)
{
    o(n);
    put32(ind, t);
    t = ind;
    ind = ind + 4;
    return t;
}

/* load immediate value */
static void li(int t)
{
    oad(0xb8, t); /* mov $xx, %eax */
}

static int gjmp(int t)
{
    return psym(0xe9, t);
}

/* l = 0: je, l == 1: jne */
static int gtst(int l, int t)
{
    o(0x0fc085); /* test %eax, %eax, je/jne xxx */
    return psym(0x84 + l, t);
}

static void gcmp(int t)
{
    o(0xc139); /* cmp %eax,%ecx */
    li(0);
    o(0x0f); /* setxx %al */
    o(t + 0x90);
    o(0xc0);
}

static void gmov(int l, int t)
{
    int n;
    if (!is_symbol(t)) error("identifier expected");
    n = *(int *)t;
    if (!n) error("undeclared variable");
    o(l + 0x83);
    if (n < LOCAL)
        oad(0x85, n);
    else {
        t = t + 4;
        *(int *)t = psym(0x05, *(int *)t);
    }
}

/* l is one if '=' parsing wanted (quick hack) */
static void unary(int l)
{
    int n, t, a, c;

    enter_parser();
    n = 1; /* type of expression 0 = forward, 1 = value, other =
              lvalue */
    if (tok == '\"') {
        li(glo + data_offset);
        while (ch != '\"') {
            if (ch == -1 || ch == '\n') error("unterminated string");
            getq();
            if (glo - data >= ALLOC_SIZE - 16) error("data buffer full");
            *(char *)glo++ = ch;
            inp();
        }
        *(char *)glo = 0;
        glo = (glo + 4) & -4;
        inp();
        next();
    } else {
        c = tokl;
        a = tokc;
        t = tok;
        next();
        if (t == TOK_NUM) {
            li(a);
        } else if (c == 2) {
            /* -, +, !, ~ */
            unary(0);
            oad(0xb9, 0); /* movl $0, %ecx */
            if (t == '!')
                gcmp(a);
            else
                o(a);
        } else if (t == '(') {
            expr();
            skip(')');
        } else if (t == '*') {
            /* parse cast */
            skip('(');
            t = tok; /* get type */
            next(); /* skip int/char/void */
            next(); /* skip '*' or '(' */
            if (tok == '*') {
                /* function type */
                skip('*');
                skip(')');
                skip('(');
                skip(')');
                t = 0;
            }
            skip(')');
            unary(0);
            if (tok == '=') {
                next();
                o(0x50); /* push %eax */
                expr();
                o(0x59); /* pop %ecx */
                o(0x0188 + (t == TOK_INT)); /* movl %eax/%al, (%ecx) */
            } else if (t) {
                if (t == TOK_INT)
                    o(0x8b); /* mov (%eax), %eax */
                else
                    o(0xbe0f); /* movsbl (%eax), %eax */
                ind++; /* add zero in code */
            }
        } else if (t == '&') {
            gmov(10, tok); /* leal EA, %eax */
            next();
        } else {
            if (!is_symbol(t)) error("expression expected");
            n = 0;
            if (tok == '=' && l) {
                /* assignment */
                next();
                expr();
                gmov(6, t); /* mov %eax, EA */
            } else if (tok != '(') {
                /* variable */
                gmov(8, t); /* mov EA, %eax */
                if (tokl == 11) {
                    gmov(0, t);
                    o(tokc);
                    next();
                }
            }
        }
    }

    /* function call */
    if (tok == '(') {
        if (n)
            o(0x50); /* push %eax */

        /* push args and invert order */
        a = oad(0xec81, 0); /* sub $xxx, %esp */
        next();
        l = 0;
        while(tok != ')') {
            expr();
            oad(0x248489, l); /* movl %eax, xxx(%esp) */
            if (tok == ',') next();
            else if (tok != ')') error("comma expected");
            l += 4;
            if (l > 128) error("too many arguments");
        }
        put32(a, l);
        next();
        if (n) {
            oad(0x2494ff, l); /* call *xxx(%esp) */
            l = l + 4;
        } else {
            /* forward reference */
            t = t + 4;
            *(int *)t = psym(0xe8, *(int *)t);
        }
        if (l)
            oad(0xc481, l); /* add $xxx, %esp */
    }
    parse_depth--;
}

static void sum(int l)
{
    int t, n, a;

    if (l-- == 1)
        unary(1);
    else {
        sum(l);
        a = 0;
        while (l == tokl) {
            n = tok;
            t = tokc;
            next();

            if (l > 8) {
                a = gtst(t, a); /* && and || output code generation */
                sum(l);
            } else {
                o(0x50); /* push %eax */
                sum(l);
                o(0x59); /* pop %ecx */

                if (l == 4 || l == 5) {
                    gcmp(t);
                } else {
                    o(t);
                    if (n == '%')
                        o(0x92); /* xchg %edx, %eax */
                }
            }
        }
        /* && and || output code generation */
        if (a && l > 8) {
            a = gtst(t, a);
            li(t ^ 1);
            gjmp(5); /* jmp $ + 5 */
            gsym(a);
            li(t);
        }
    }
}

static void expr(void)
{
    sum(11);
}


static int test_expr(void)
{
    expr();
    return gtst(0, 0);
}

static void block(int *l)
{
    int a, n, t;
    unsigned int mark;
    enter_parser();

    if (tok == TOK_IF) {
        next();
        skip('(');
        a = test_expr();
        skip(')');
        block(l);
        if (tok == TOK_ELSE) {
            next();
            n = gjmp(0); /* jmp */
            gsym(a);
            block(l);
            gsym(n); /* patch else jmp */
        } else {
            gsym(a); /* patch if test */
        }
    } else if (tok == TOK_WHILE || tok == TOK_FOR) {
        t = tok;
        next();
        skip('(');
        if (t == TOK_WHILE) {
            n = ind;
            a = test_expr();
        } else {
            if (tok != ';')
                expr();
            skip(';');
            n = ind;
            a = 0;
            if (tok != ';')
                a = test_expr();
            skip(';');
            if (tok != ')') {
                t = gjmp(0);
                expr();
                gjmp(n - ind - 5);
                gsym(t);
                n = t + 4;
            }
        }
        skip(')');
        block(&a);
        gjmp(n - ind - 5); /* jmp */
        gsym(a);
    } else if (tok == '{') {
        next();
        mark = locals;
        decl(1);
        while(tok != '}') {
            if (tok == -1) error("unterminated block");
            block(l);
        }
        next();
        unbind_locals(mark);
    } else {
        if (tok == TOK_RETURN) {
            next();
            if (tok != ';')
                expr();
            rsym = gjmp(rsym); /* jmp */
        } else if (tok == TOK_BREAK) {
            next();
            if (!l) error("break outside loop");
            *l = gjmp(*l);
        } else if (tok != ';')
            expr();
        skip(';');
    }
    parse_depth--;
}

/* int variables and int/implicit-int function definitions. */
static void decl(int l)
{
    int a, symbol, typed;
    unsigned int mark;
    while (tok == TOK_INT || (tok != -1 && !l)) {
        typed = tok == TOK_INT;
        if (typed) next();
        symbol = tok;
        if (!is_symbol(symbol))
            error("identifier expected");
        next();
        if (!l && tok == '(') {
            if (*(int *)symbol) error("duplicate function definition");
            *(int *)symbol = ind;
            next();
            if (tok == TOK_VOID) { next(); if (tok != ')') error("expected ) after void"); }
            mark = locals;
            a = 8;
            while (tok != ')') {
                if (tok == TOK_INT) next();
                if (!is_symbol(tok)) error("parameter name expected");
                if (a >= 136) error("too many parameters");
                bind_local(tok, a);
                a += 4;
                next();
                if (tok == ',') next();
                else if (tok != ')') error("comma expected");
            }
            next();
            rsym = loc = 0;
            o(0xe58955);
            a = oad(0xec81, 0);
            if (tok != '{') error("function body expected");
            block(0);
            li(0);
            gsym(rsym);
            o(0xc3c9);
            put32(a, loc);
            unbind_locals(mark);
        } else {
            if (!typed) error("int declaration expected");
            for (;;) {
                if (l) {
                    loc += 4;
                    bind_local(symbol, -loc);
                    if (tok == '=') { next(); expr(); gmov(6, symbol); }
                } else {
                    if (*(int *)symbol) error("duplicate global declaration");
                    if (glo - data >= ALLOC_SIZE - 16) error("data buffer full");
                    *(int *)symbol = glo; glo += 4;
                }
                if (tok != ',') break;
                next();
                symbol = tok;
                if (!is_symbol(symbol))
                    error("identifier expected");
                next();
            }
            skip(';');
        }
    }
}


/* Cerulia's static ELF32 writer. Runtime RX, compiled text RX, globals RW.
 * The compiler runs entirely in ring3. No host compiler or binutils is called.
 */
static int runtime_symbol(const char *name, int len) {
    unsigned int i;
    for (i = 0; i < sizeof(runtime_names) / sizeof(runtime_names[0]); i++)
        if ((int)strlen(runtime_names[i]) == len &&
            !strncmp(name, runtime_names[i], (unsigned int)len))
            return 0x400000 + 32 + 5 * (int)i;
    return 0;
}

static void resolve_symbols(void) {
    int p = sym_stk, start, slot, val, refs, target, next_ref;
    while (p < dstk) {
        p++;
        start = p;
        while (p < dstk && *(char *)p != TAG_TOK) p++;
        if (p == dstk) break;
        slot = vars + (start - sym_stk) * 8 + TOK_IDENT - 8;
        val = *(int *)slot;
        refs = *(int *)(slot + 4);
        if (!refs) continue;
        if (val) { gsym1(refs, val); continue; }
        target = runtime_symbol((char *)start, p - start);
        if (!target) error("undefined external symbol");
        while (refs) {
            next_ref = get32(refs);
            if (*(unsigned char *)(refs - 1) == 0x05) put32(refs, target);
            else put32(refs, target - (refs - prog + 0x401000) - 4);
            refs = next_ref;
        }
    }
}

static void word(unsigned char *p, unsigned int n) {
    p[0] = n; p[1] = n >> 8; p[2] = n >> 16; p[3] = n >> 24;
}

static void segment(unsigned char *p, unsigned int off, unsigned int va,
                    unsigned int size, unsigned int flags) {
    word(p, 1); word(p + 4, off); word(p + 8, va); word(p + 12, va);
    word(p + 16, size); word(p + 20, size); word(p + 24, flags);
    word(p + 28, 4096);
}

static void elf_out(const char *name) {
    unsigned char *out;
    unsigned int code_size = (unsigned int)(ind - prog);
    unsigned int data_size = (unsigned int)(glo - data);
    unsigned int data_off = (8192 + code_size + 4095) & ~4095u;
    unsigned int size = data_off + data_size;
    int main_addr = *(int *)(vars + TOK_MAIN);
    int fd;
    if (main_addr < prog || main_addr >= ind) error("main function required");
    if (size > 65536) error("output exceeds ELF loader's 64KB limit");
    resolve_symbols();
    out = calloc(1, (int)size);
    word(out, 0x464c457f); word(out + 4, 0x00010101);
    word(out + 16, 0x00030002); word(out + 20, 1);
    word(out + 24, 0x400000); word(out + 28, 52);
    out[40] = 52; out[42] = 32; out[44] = data_size ? 2 : 1;
    segment(out + 52, 4096, 0x400000, 4096 + code_size, 5);
    if (data_size) segment(out + 84, data_off, 0x500000, data_size, 6);
    memcpy(out + 4096, cc_runtime, sizeof(cc_runtime));
    word(out + 4096 + 6, main_addr - prog + 0x401000 - 0x40000a);
    memcpy(out + 8192, (void *)prog, code_size);
    memcpy(out + data_off, (void *)data, data_size);
    fd = open(name, O_WRONLY | O_CREAT | O_TRUNC);
    if (fd < 0) error("cannot open output file");
    if (write(fd, out, size) != (int)size) { close(fd); error("output write failed"); }
    if (close(fd) < 0) error("output save failed");
    printf("cc: saved %s (%u bytes)\n", name, size);
}

int umain(int argc, char **argv) {
    int fd, n, i;
    unsigned int src_len, out_len;
    if (argc != 3) {
        puts("Cerulia cc: integer C subset -> native static ELF32\n"
             "usage: exec cc source.c output.elf\n");
        return 1;
    }
    src_len = strlen(argv[1]);
    out_len = strlen(argv[2]);
    if (src_len < 3 || argv[1][src_len - 2] != '.' ||
        (argv[1][src_len - 1] | 32) != 'c' ||
        out_len < 5 || argv[2][out_len - 4] != '.' ||
        (argv[2][out_len - 3] | 32) != 'e' ||
        (argv[2][out_len - 2] | 32) != 'l' || (argv[2][out_len - 1] | 32) != 'f')
        error("expected .c source and .elf output");
    fd = open(argv[1], O_RDONLY);
    if (fd < 0) { puts("cc: cannot open source\n"); return 1; }
    n = read(fd, source, ALLOC_SIZE);
    if (n <= 0 || n >= ALLOC_SIZE) { close(fd); puts("cc: source empty or too large\n"); return 1; }
    close(fd);
    for (i = 0; i < n; i++) if (!source[i]) error("NUL byte in source");
    source[n] = 0;
    sym_stk = (int)calloc(1, ALLOC_SIZE);
    memcpy((void *)sym_stk, " int if else while break return for define main ", TOK_STR_SIZE);
    dstk = sym_stk + TOK_STR_SIZE;
    data = glo = (int)calloc(1, ALLOC_SIZE);
    prog = ind = (int)calloc(1, ALLOC_SIZE);
    vars = (int)calloc(1, ALLOC_SIZE * 8 + 1024);
    data_offset = 0x500000 - data;
    inp(); next(); decl(0);
    pdef(TAG_TOK); /* Terminate the last symbol for relocation. */
    elf_out(argv[2]);
    return 0;
}
