// full day replay harness. streams a raw or gzipped itch file into
// book_top one byte per cycle and logs every bbo strobe as a 16 byte
// record, the python side generates the same records from the reference
// model and the two files have to match bit for bit.
//
//   ./sim_book <itch.gz> <out.bin> <locate> <base> [max_msgs]

#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <zlib.h>
#include "Vbook_top.h"
#include "verilated.h"

static Vbook_top *top;
static vluint64_t t = 0;

static inline void tick() {
    top->clk = 0; top->eval();
    top->clk = 1; top->eval();
    t++;
}

struct Rec {
    uint16_t flags, bid_tick, ask_tick, rsvd;
    uint32_t bid_shares, ask_shares;
};

int main(int argc, char **argv) {
    if (argc < 5) {
        fprintf(stderr, "usage: %s itch.gz out.bin locate base [max_msgs]\n", argv[0]);
        return 2;
    }
    const char *in_path = argv[1], *out_path = argv[2];
    int locate = atoi(argv[3]);
    long base = atol(argv[4]);
    long max_msgs = argc > 5 ? atol(argv[5]) : 0;

    Verilated::commandArgs(argc, argv);
    top = new Vbook_top;

    gzFile in = gzopen(in_path, "rb");
    if (!in) { fprintf(stderr, "cannot open %s\n", in_path); return 2; }
    gzbuffer(in, 1 << 20);
    FILE *out = fopen(out_path, "wb");
    setvbuf(out, nullptr, _IOFBF, 1 << 20);

    top->rst = 1; top->in_valid = 0; top->in_data = 0;
    top->cfg_locate = locate; top->cfg_base = base; top->dbg_addr = 0;
    for (int i = 0; i < 5; i++) tick();
    top->rst = 0;
    for (int i = 0; i < 2; i++) tick();

    auto service = [&](void) {
        if (top->bbo_valid) {
            Rec r{};
            r.flags = (top->bid_present ? 1 : 0) | (top->ask_present ? 2 : 0);
            r.bid_tick = top->bid_present ? top->bid_tick : 0;
            r.ask_tick = top->ask_present ? top->ask_tick : 0;
            r.bid_shares = top->bid_present ? top->bid_shares : 0;
            r.ask_shares = top->ask_present ? top->ask_shares : 0;
            fwrite(&r, sizeof r, 1, out);
        }
    };

    long msgs = 0;
    unsigned char hdr[2], body[64];
    while (true) {
        if (gzread(in, hdr, 2) != 2) break;
        int len = (hdr[0] << 8) | hdr[1];
        if (len <= 0 || len > 64) { fprintf(stderr, "bad frame len %d\n", len); break; }
        if (gzread(in, body, len) != len) break;   // truncated tail, done
        top->in_valid = 1;
        top->in_data = hdr[0]; tick(); service();
        top->in_data = hdr[1]; tick(); service();
        for (int i = 0; i < len; i++) { top->in_data = body[i]; tick(); service(); }
        msgs++;
        if ((msgs % 20000000) == 0)
            fprintf(stderr, "  %ldM msgs, %llu cycles\n", msgs / 1000000,
                    (unsigned long long)t);
        if (max_msgs && msgs >= max_msgs) break;
    }
    top->in_valid = 0;
    for (int i = 0; i < 100; i++) { tick(); service(); }

    fclose(out);
    gzclose(in);

    printf("{\"msgs\": %ld, \"cycles\": %llu, "
           "\"filtered\": %u, \"outlier\": %u, \"miss\": %u, \"overflow\": %u, "
           "\"fifo_hwm\": %u, \"fifo_overflow\": %u, \"framing_err\": %u}\n",
           msgs, (unsigned long long)t,
           top->cnt_filtered, top->cnt_outlier, top->cnt_miss, top->cnt_overflow,
           top->fifo_hwm, (unsigned)top->fifo_overflow, (unsigned)top->framing_err);

    delete top;
    return 0;
}
