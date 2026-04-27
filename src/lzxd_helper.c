/*
 * lzxd_helper — decompress a raw LZX stream (FH1 / Xbox-360 flavour).
 *
 * Reads compressed bytes from stdin, writes decompressed bytes to stdout.
 *
 * Usage: lzxd_helper MODE WINDOW_BITS RESET_INTERVAL OUTPUT_LENGTH [CHUNK_USIZE]
 *
 *   MODE=single        — stdin is one contiguous LZX stream.
 *   MODE=chunked_be    — stdin is [u16 BE csize, csize_bytes LZX data]+.
 *                        Each chunk decompresses to CHUNK_USIZE uncompressed bytes,
 *                        except the final chunk which is capped by OUTPUT_LENGTH.
 *                        Chunk headers are stripped; the decoder receives a
 *                        continuous bitstream so state persists across chunks.
 *
 * Built against the internal libmspack lzxd_* API; see fh1-mapdecomp/build.sh.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <sys/types.h>

#include <system.h>
#include <lzx.h>

/* ---- in-memory mspack_file ---- */

typedef struct {
    const unsigned char *data;
    size_t len, pos;
} mem_in;

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
static int sys_write(struct mspack_file *f, void *buf, int bytes) {
    file_out *o = (file_out *)f;
    size_t r = fwrite(buf, 1, (size_t)bytes, o->fp);
    return (r == (size_t)bytes) ? bytes : -1;
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

static struct mspack_system SYS = {
    sys_open, sys_close, sys_read, sys_write, sys_seek, sys_tell,
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

int main(int argc, char **argv) {
    if (argc < 5) {
        fprintf(stderr, "usage: %s MODE wbits reset outlen [chunk_usize]\n"
                        "  MODE = single | chunked_be\n", argv[0]);
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
        &SYS,
        (struct mspack_file *)&minp,
        (struct mspack_file *)&fout,
        wbits, reset,
        4096,                  /* input_buffer_size */
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
