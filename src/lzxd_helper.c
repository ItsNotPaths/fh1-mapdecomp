/*
 * lzxd_helper — decompress raw LZX streams (FH1 / Xbox-360 flavour).
 *
 * Two invocation modes:
 *
 *   ONE-SHOT (legacy)
 *     lzxd_helper MODE wbits reset outlen [chunk_usize]
 *       MODE=single        — stdin is one contiguous LZX stream.
 *       MODE=chunked_be    — stdin is [u16 BE csize, csize_bytes LZX data]+.
 *     Reads compressed bytes from stdin, writes decompressed bytes to stdout.
 *
 *   DAEMON (preferred)
 *     lzxd_helper daemon
 *
 *     Wire protocol on stdin (per request):
 *         u32 BE outlen
 *         u32 BE inlen
 *         u8[inlen]
 *     Response on stdout (per request):
 *         u32 BE status        (0 = ok, non-zero = error)
 *         u32 BE payload_len
 *         u8[payload_len]      decompressed bytes (status=0)
 *                              or ASCII error message (status!=0)
 *
 *     Daemon reads requests until EOF on stdin. wbits/reset/mode are
 *     fixed at single + 17 / 0; the only mode the FH1 pipeline calls.
 *     Saves one fork+exec (~3 ms) per LZX entry — the main cost driver
 *     when extracting tens of thousands of bin.zip entries.
 *
 * Built against the internal libmspack lzxd_* API; see release.sh.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <stdint.h>
#include <unistd.h>
#include <sys/types.h>

#include <system.h>
#include <lzx.h>

/* ---- in-memory mspack_file ---- */

typedef struct {
    const unsigned char *data;
    size_t len, pos;
} mem_in;

typedef struct {
    unsigned char *data;
    size_t cap, pos;
} mem_out;

typedef struct {
    FILE *fp;
} file_out;

static int sys_read(struct mspack_file *f, void *buf, int bytes) {
    mem_in *m = (mem_in *)f;
    size_t n = m->len - m->pos;
    if ((int)n > bytes) n = (size_t)bytes;
    memcpy(buf, m->data + m->pos, n);
    m->pos += n;
    return (int)n;
}
static int sys_write_file(struct mspack_file *f, void *buf, int bytes) {
    file_out *o = (file_out *)f;
    size_t r = fwrite(buf, 1, (size_t)bytes, o->fp);
    return (r == (size_t)bytes) ? bytes : -1;
}
static int sys_write_mem(struct mspack_file *f, void *buf, int bytes) {
    mem_out *o = (mem_out *)f;
    if (o->pos + (size_t)bytes > o->cap) return -1;
    memcpy(o->data + o->pos, buf, (size_t)bytes);
    o->pos += (size_t)bytes;
    return bytes;
}
static struct mspack_file *sys_open(struct mspack_system *s, const char *n, int m) {
    (void)s; (void)n; (void)m; return NULL;
}
static void sys_close(struct mspack_file *f) { (void)f; }
static int sys_seek(struct mspack_file *f, off_t o, int m) {
    (void)f; (void)o; (void)m; return -1;
}
static off_t sys_tell(struct mspack_file *f) { (void)f; return 0; }
static void sys_message(struct mspack_file *f, const char *fmt, ...) {
    (void)f; va_list ap; va_start(ap, fmt);
    vfprintf(stderr, fmt, ap); fputc('\n', stderr); va_end(ap);
}
static void *sys_alloc(struct mspack_system *s, size_t n) { (void)s; return malloc(n); }
static void sys_free(void *p) { free(p); }
static void sys_copy(void *src, void *dst, size_t n) { memcpy(dst, src, n); }

static struct mspack_system SYS_FILE = {
    sys_open, sys_close, sys_read, sys_write_file, sys_seek, sys_tell,
    sys_message, sys_alloc, sys_free, sys_copy, NULL
};

static struct mspack_system SYS_MEM = {
    sys_open, sys_close, sys_read, sys_write_mem, sys_seek, sys_tell,
    sys_message, sys_alloc, sys_free, sys_copy, NULL
};

static unsigned char *slurp(size_t *out_len) {
    size_t cap = 1 << 15, len = 0;
    unsigned char *buf = malloc(cap);
    if (!buf) return NULL;
    for (;;) {
        if (len == cap) { cap <<= 1; buf = realloc(buf, cap); if (!buf) return NULL; }
        size_t r = fread(buf + len, 1, cap - len, stdin);
        if (r == 0) break;
        len += r;
    }
    *out_len = len;
    return buf;
}

static unsigned char *strip_chunks(const unsigned char *in, size_t in_len,
                                    long long out_total, long long chunk_usize,
                                    size_t *raw_len) {
    unsigned char *raw = malloc(in_len);
    if (!raw) return NULL;
    size_t rpos = 0, ipos = 0;
    long long remaining = out_total;
    while (remaining > 0) {
        if (in_len - ipos < 2) { fprintf(stderr, "truncated chunk header at %zu\n", ipos); free(raw); return NULL; }
        unsigned int csize = ((unsigned)in[ipos] << 8) | in[ipos+1];
        ipos += 2;
        if (in_len - ipos < csize) { fprintf(stderr, "truncated chunk body: csize=%u avail=%zu\n", csize, in_len - ipos); free(raw); return NULL; }
        memcpy(raw + rpos, in + ipos, csize);
        rpos  += csize;
        ipos  += csize;
        long long step = chunk_usize < remaining ? chunk_usize : remaining;
        remaining -= step;
    }
    *raw_len = rpos;
    if (ipos != in_len) {
        fprintf(stderr, "note: %zu trailing bytes in chunked input\n", in_len - ipos);
    }
    return raw;
}

/* ---- daemon helpers ---- */

static int read_full(int fd, void *buf, size_t n) {
    unsigned char *p = (unsigned char *)buf;
    size_t got = 0;
    while (got < n) {
        ssize_t r = read(fd, p + got, n - got);
        if (r == 0) return (got == 0) ? 0 : -1;  /* clean EOF only at boundary */
        if (r < 0) return -1;
        got += (size_t)r;
    }
    return 1;
}

static int write_full(int fd, const void *buf, size_t n) {
    const unsigned char *p = (const unsigned char *)buf;
    size_t sent = 0;
    while (sent < n) {
        ssize_t r = write(fd, p + sent, n - sent);
        if (r <= 0) return -1;
        sent += (size_t)r;
    }
    return 0;
}

static uint32_t read_u32_be(const unsigned char *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static void write_u32_be(unsigned char *p, uint32_t v) {
    p[0] = (v >> 24) & 0xff;
    p[1] = (v >> 16) & 0xff;
    p[2] = (v >> 8) & 0xff;
    p[3] = v & 0xff;
}

static int decompress_into(const unsigned char *in, size_t in_len,
                            unsigned char *out, size_t out_len,
                            int wbits, int reset, char *err, size_t err_cap) {
    mem_in  minp = { in, in_len, 0 };
    mem_out mout = { out, out_len, 0 };
    struct lzxd_stream *lzx = lzxd_init(
        &SYS_MEM,
        (struct mspack_file *)&minp,
        (struct mspack_file *)&mout,
        wbits, reset,
        4096, (off_t)out_len, 0);
    if (!lzx) {
        snprintf(err, err_cap, "lzxd_init failed");
        return -1;
    }
    int rc = lzxd_decompress(lzx, (off_t)out_len);
    lzxd_free(lzx);
    if (rc != MSPACK_ERR_OK) {
        snprintf(err, err_cap, "lzxd_decompress rc=%d", rc);
        return -1;
    }
    if (mout.pos != out_len) {
        snprintf(err, err_cap, "decompressed %zu bytes, expected %zu",
                 mout.pos, out_len);
        return -1;
    }
    return 0;
}

static int run_daemon(int wbits, int reset) {
    unsigned char hdr[8];
    unsigned char *in = NULL, *out = NULL;
    size_t in_cap = 0, out_cap = 0;
    char err[256];

    for (;;) {
        int rs = read_full(0, hdr, sizeof(hdr));
        if (rs == 0) break;            /* clean EOF, exit 0 */
        if (rs < 0) { fprintf(stderr, "daemon: short read on header\n"); return 1; }

        uint32_t out_len = read_u32_be(hdr);
        uint32_t in_len  = read_u32_be(hdr + 4);

        if (in_len > in_cap) {
            unsigned char *p = realloc(in, in_len ? in_len : 1);
            if (!p) { fprintf(stderr, "daemon: oom inbuf %u\n", in_len); return 2; }
            in = p; in_cap = in_len;
        }
        if (out_len > out_cap) {
            unsigned char *p = realloc(out, out_len ? out_len : 1);
            if (!p) { fprintf(stderr, "daemon: oom outbuf %u\n", out_len); return 2; }
            out = p; out_cap = out_len;
        }

        if (in_len > 0 && read_full(0, in, in_len) <= 0) {
            fprintf(stderr, "daemon: short read on body (in_len=%u)\n", in_len);
            return 1;
        }

        unsigned char rhdr[8];
        if (decompress_into(in, in_len, out, out_len, wbits, reset, err, sizeof(err)) == 0) {
            write_u32_be(rhdr, 0);
            write_u32_be(rhdr + 4, out_len);
            if (write_full(1, rhdr, sizeof(rhdr)) < 0) return 1;
            if (out_len > 0 && write_full(1, out, out_len) < 0) return 1;
        } else {
            uint32_t elen = (uint32_t)strlen(err);
            write_u32_be(rhdr, 1);
            write_u32_be(rhdr + 4, elen);
            if (write_full(1, rhdr, sizeof(rhdr)) < 0) return 1;
            if (elen > 0 && write_full(1, (unsigned char *)err, elen) < 0) return 1;
        }
    }
    free(in); free(out);
    return 0;
}

int main(int argc, char **argv) {
    if (argc >= 2 && !strcmp(argv[1], "daemon")) {
        return run_daemon(17, 0);  /* FH1 fixed parameters */
    }

    if (argc < 5) {
        fprintf(stderr, "usage:\n"
                        "  %s daemon\n"
                        "  %s MODE wbits reset outlen [chunk_usize]\n"
                        "    MODE = single | chunked_be\n", argv[0], argv[0]);
        return 2;
    }
    const char *mode = argv[1];
    int wbits = atoi(argv[2]);
    int reset = atoi(argv[3]);
    long long outlen = atoll(argv[4]);

    size_t in_len = 0;
    unsigned char *in = slurp(&in_len);
    if (!in) { fprintf(stderr, "oom reading stdin\n"); return 3; }

    unsigned char *stream = in; size_t stream_len = in_len;
    unsigned char *stripped = NULL;
    if (!strcmp(mode, "chunked_be")) {
        if (argc < 6) { fprintf(stderr, "chunked_be needs chunk_usize\n"); return 2; }
        long long chunk_usize = atoll(argv[5]);
        stripped = strip_chunks(in, in_len, outlen, chunk_usize, &stream_len);
        if (!stripped) return 4;
        stream = stripped;
    } else if (strcmp(mode, "single")) {
        fprintf(stderr, "unknown mode: %s\n", mode);
        return 2;
    }

    mem_in  minp = { stream, stream_len, 0 };
    file_out fout = { stdout };

    struct lzxd_stream *lzx = lzxd_init(
        &SYS_FILE,
        (struct mspack_file *)&minp,
        (struct mspack_file *)&fout,
        wbits, reset,
        4096,
        (off_t)outlen,
        0);
    if (!lzx) { fprintf(stderr, "lzxd_init failed\n"); return 5; }

    int rc = lzxd_decompress(lzx, (off_t)outlen);
    lzxd_free(lzx);
    free(in); free(stripped);

    if (rc != MSPACK_ERR_OK) {
        fprintf(stderr, "lzxd_decompress rc=%d\n", rc);
        return 6;
    }
    fflush(stdout);
    return 0;
}
