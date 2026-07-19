`timescale 1ns/1ps
// hardware limit order book, one symbol, top of book in fixed cycles.
//
// storage plan:
//   orders  - 4 way hashed table in bram, keyed by the 64 bit itch ref
//   levels  - shares per price tick in bram, one array per side
//   bitmaps - 4096 bit occupancy per side in ffs, so finding the next
//             best price after a level empties is a priority encode,
//             not a scan. that is what keeps update time constant.
//
// prices map to ticks as (price - cfg_base) / 100, penny ticks inside a
// window around where the stock trades. off tick or out of window orders
// get counted and skipped, the stats tell you how rare that is. real
// books shard per symbol and bound their price range exactly like this.
//
// cfg_locate and cfg_base are static configuration, set them before the
// first op and leave them alone. resting orders store ticks not prices,
// so moving the base mid session would silently reprice the whole book.
//
// every op takes a fixed 6 cycles accept to bbo strobe and the book state
// itself lands in 4. replace is 10 since it runs the fsm twice, cancel
// the old ref then add the new one.

`default_nettype none

module order_book #(
    parameter TICKS_LOG2 = 12,             // 4096 ticks = a $40.96 window
    parameter SETS_LOG2  = 11              // 4 ways x 2048 sets = 8192 orders
) (
    input  wire        clk,
    input  wire        rst,

    input  wire [15:0] cfg_locate,         // which symbol we build
    input  wire [31:0] cfg_base,           // window base, raw itch price units

    input  wire        op_valid,
    output wire        op_ready,
    input  wire [2:0]  op_code,
    input  wire [15:0] op_locate,
    input  wire [63:0] op_ref,
    input  wire        op_side,
    input  wire [31:0] op_shares,
    input  wire [31:0] op_price,
    input  wire [63:0] op_ref2,

    // best bid / ask, one strobe per applied op
    output reg                   bbo_valid,
    output reg                   bid_present,
    output reg  [TICKS_LOG2-1:0] bid_tick,
    output reg  [31:0]           bid_shares,
    output reg                   ask_present,
    output reg  [TICKS_LOG2-1:0] ask_tick,
    output reg  [31:0]           ask_shares,

    // things we consciously did not apply
    output reg  [31:0] cnt_filtered,       // other symbols
    output reg  [31:0] cnt_outlier,        // off tick or outside the window
    output reg  [31:0] cnt_miss,           // op for a ref we never stored
    output reg  [31:0] cnt_overflow,       // hash set full, should stay 0

    // debug port for depth snapshots, not the hot path
    input  wire [TICKS_LOG2:0]   dbg_addr, // msb picks side, 1 = bid
    output reg  [31:0]           dbg_shares
);

    localparam OP_ADD     = 3'd0;
    localparam OP_EXEC    = 3'd1;
    localparam OP_CANCEL  = 3'd2;
    localparam OP_DELETE  = 3'd3;
    localparam OP_REPLACE = 3'd4;

    localparam TICKS = 1 << TICKS_LOG2;
    localparam SETS  = 1 << SETS_LOG2;
    localparam [31:0] WINDOW = TICKS * 100;

    // order table entry: {valid, ref[63:0], side, tick, shares[31:0]}
    localparam EW = 1 + 64 + 1 + TICKS_LOG2 + 32;

    reg [EW-1:0] way0 [0:SETS-1];
    reg [EW-1:0] way1 [0:SETS-1];
    reg [EW-1:0] way2 [0:SETS-1];
    reg [EW-1:0] way3 [0:SETS-1];
    reg [31:0]   bid_levels [0:TICKS-1];
    reg [31:0]   ask_levels [0:TICKS-1];
    reg [TICKS-1:0] bid_map;
    reg [TICKS-1:0] ask_map;

    // memories come up zeroed from the bitstream. a live design would
    // sweep an init fsm through them on reset instead.
    initial begin
        // the find functions and tick math are sized for these exact
        // widths, the parameters exist for the resource sweep scripts
        if (TICKS_LOG2 != 12)
            $fatal(1, "TICKS_LOG2 is fixed at 12");
        if (SETS_LOG2 > 21)
            $fatal(1, "hash fold needs 3*SETS_LOG2 <= 64");
    end

    integer ii;
    initial begin
        for (ii = 0; ii < SETS; ii = ii + 1) begin
            way0[ii] = {EW{1'b0}};
            way1[ii] = {EW{1'b0}};
            way2[ii] = {EW{1'b0}};
            way3[ii] = {EW{1'b0}};
        end
        for (ii = 0; ii < TICKS; ii = ii + 1) begin
            bid_levels[ii] = 32'd0;
            ask_levels[ii] = 32'd0;
        end
    end

    localparam S_IDLE    = 3'd0;
    localparam S_LOOKUP  = 3'd1;
    localparam S_RESOLVE = 3'd2;
    localparam S_LEVEL   = 3'd3;
    localparam S_FIND    = 3'd4;
    localparam S_SHARES  = 3'd5;

    reg [2:0] state;

    reg [2:0]  c_code;
    reg [63:0] c_ref;
    reg        c_side;
    reg [31:0] c_shares;
    reg [31:0] c_price;
    reg [63:0] c_ref2;
    reg        c_rep_pend;  // between the two trips of a replace
    reg        c_was_rep;   // this op started life as a replace

    assign op_ready = (state == S_IDLE) && !c_rep_pend;

    // ref hash, low bits xor folded. refs are near sequential so low bits
    // alone would do, the fold is cheap insurance against patterns.
    wire [SETS_LOG2-1:0] hash_ref  = c_ref[SETS_LOG2-1:0]
                                   ^ c_ref[2*SETS_LOG2-1:SETS_LOG2]
                                   ^ c_ref[3*SETS_LOG2-1:2*SETS_LOG2];

    // price to tick. (diff * 42949673) >> 32 is an exact divide by 100
    // for anything under the window bound, which gets checked first.
    wire [31:0] diff   = c_price - cfg_base;
    wire        under  = (c_price < cfg_base);
    reg  [18:0] diff_q;
    reg         px_bad;
    wire [50:0] mult   = diff_q * 32'd42949673;
    wire [18:0] q      = mult[50:32];
    wire [31:0] q_back = {13'd0, q} * 32'd100;

    reg [EW-1:0]         w0_rd, w1_rd, w2_rd, w3_rd;
    reg [SETS_LOG2-1:0]  idx_r;
    reg [TICKS_LOG2-1:0] tick_r;
    reg                  side_r;
    reg                  hit0, hit1, hit2, hit3, add_tick_ok;
    reg [31:0]           bl_rd, al_rd;
    reg [31:0]           ord_shares_r;
    reg [TICKS_LOG2-1:0] fb_tick, fa_tick;
    reg                  fb_ok, fa_ok;

    wire e0_v = w0_rd[EW-1];
    wire e1_v = w1_rd[EW-1];
    wire e2_v = w2_rd[EW-1];
    wire e3_v = w3_rd[EW-1];
    wire m0 = e0_v && (w0_rd[EW-2 -: 64] == c_ref);
    wire m1 = e1_v && (w1_rd[EW-2 -: 64] == c_ref);
    wire m2 = e2_v && (w2_rd[EW-2 -: 64] == c_ref);
    wire m3 = e3_v && (w3_rd[EW-2 -: 64] == c_ref);

    // whichever way matched, straight off the read regs for resolve
    wire [EW-1:0] eraw = m0 ? w0_rd : m1 ? w1_rd : m2 ? w2_rd : w3_rd;
    wire                  eraw_side   = eraw[32+TICKS_LOG2];
    wire [TICKS_LOG2-1:0] eraw_tick   = eraw[32 +: TICKS_LOG2];
    wire [31:0]           eraw_shares = eraw[31:0];

    // insert picks the lowest numbered free way, full set means overflow
    wire any_free = !(e0_v && e1_v && e2_v && e3_v);
    wire [1:0] free_way = !e0_v ? 2'd0 : !e1_v ? 2'd1 : !e2_v ? 2'd2 : 2'd3;
    wire any_hit_r = hit0 | hit1 | hit2 | hit3;

    // find best: highest occupied tick for bids, lowest for asks.
    // two level encode, 64 groups of 64, keeps the logic shallow.
    function [TICKS_LOG2-1:0] find_msb(input [TICKS-1:0] map);
        integer g, b;
        reg [63:0] grp;
        reg [5:0] gi, bi;
        begin
            gi = 6'd0; bi = 6'd0;
            for (g = 0; g < 64; g = g + 1)
                if (|map[g*64 +: 64]) gi = g[5:0];
            grp = map[gi*64 +: 64];
            for (b = 0; b < 64; b = b + 1)
                if (grp[b]) bi = b[5:0];
            find_msb = {gi, bi};
        end
    endfunction

    function [TICKS_LOG2-1:0] find_lsb(input [TICKS-1:0] map);
        integer g, b;
        reg [63:0] grp;
        reg [5:0] gi, bi;
        begin
            gi = 6'd0; bi = 6'd0;
            for (g = 63; g >= 0; g = g - 1)
                if (|map[g*64 +: 64]) gi = g[5:0];
            grp = map[gi*64 +: 64];
            for (b = 63; b >= 0; b = b - 1)
                if (grp[b]) bi = b[5:0];
            find_lsb = {gi, bi};
        end
    endfunction

    wire [TICKS_LOG2-1:0] fmsb = find_msb(bid_map);
    wire [TICKS_LOG2-1:0] flsb = find_lsb(ask_map);

    // one muxed read port per level ram so it maps to real dual port bram,
    // resolve wants the touched tick, find wants the next best
    wire [TICKS_LOG2-1:0] touch_tick = (c_code == OP_ADD) ? q[TICKS_LOG2-1:0]
                                                          : eraw_tick;
    wire [TICKS_LOG2-1:0] bl_addr = (state == S_FIND) ? fmsb : touch_tick;
    wire [TICKS_LOG2-1:0] al_addr = (state == S_FIND) ? flsb : touch_tick;

    always @(posedge clk)
        if (state == S_RESOLVE)
            ord_shares_r <= eraw_shares;

    // level math for the op being applied. totals are 32 bit like the itch
    // shares field itself, nasdaq's per order size cap keeps real level
    // sums a long way from wrapping
    wire lvl_was_live = side_r ? bid_map[tick_r] : ask_map[tick_r];
    wire [31:0] lvl_cur = side_r ? bl_rd : al_rd;
    wire [31:0] lvl_add = (lvl_was_live ? lvl_cur : 32'd0) + c_shares;
    wire [31:0] ord_shares_taken =
        (c_code == OP_DELETE || c_code == OP_REPLACE)
            ? ord_shares_r
            : (c_shares > ord_shares_r ? ord_shares_r : c_shares);
    wire [31:0] lvl_sub = lvl_cur - ord_shares_taken;

    // level rams get one read write port each (plus the debug port), write
    // wins on the cycle it happens, reads run every other cycle
    wire lvl_do_write = (state == S_LEVEL) &&
        ((c_code == OP_ADD) ? (add_tick_ok && !any_hit_r && any_free)
                            : any_hit_r);
    wire [31:0] lvl_new = (c_code == OP_ADD) ? lvl_add : lvl_sub;

    always @(posedge clk) begin
        if (lvl_do_write && side_r) bid_levels[tick_r] <= lvl_new;
        else                        bl_rd <= bid_levels[bl_addr];
    end
    always @(posedge clk) begin
        if (lvl_do_write && !side_r) ask_levels[tick_r] <= lvl_new;
        else                         al_rd <= ask_levels[al_addr];
    end

    // full word rewrite of whichever way matched, partial bit writes do
    // not map onto bram
    wire ord_keep = !(c_code == OP_DELETE || c_code == OP_REPLACE
                      || ord_shares_taken == ord_shares_r);
    wire [EW-1:0] took_entry = {ord_keep, c_ref, side_r, tick_r,
                                ord_keep ? ord_shares_r - ord_shares_taken
                                         : 32'd0};

    always @(posedge clk) begin
        if (rst) begin
            state        <= S_IDLE;
            bid_map      <= {TICKS{1'b0}};
            ask_map      <= {TICKS{1'b0}};
            bbo_valid    <= 1'b0;
            bid_present  <= 1'b0;
            ask_present  <= 1'b0;
            cnt_filtered <= 32'd0;
            cnt_outlier  <= 32'd0;
            cnt_miss     <= 32'd0;
            cnt_overflow <= 32'd0;
            c_rep_pend   <= 1'b0;
        end else begin
            bbo_valid <= 1'b0;

            case (state)

            S_IDLE: begin
                if (c_rep_pend) begin
                    // second trip of a replace, becomes a plain add
                    c_code     <= OP_ADD;
                    c_ref      <= c_ref2;
                    c_rep_pend <= 1'b0;
                    state      <= S_LOOKUP;
                end else if (op_valid) begin
                    if (op_locate != cfg_locate) begin
                        cnt_filtered <= cnt_filtered + 1;
                    end else begin
                        c_code   <= op_code;
                        c_ref    <= op_ref;
                        c_side   <= op_side;
                        c_shares <= op_shares;
                        c_price  <= op_price;
                        c_ref2   <= op_ref2;
                        c_was_rep <= (op_code == OP_REPLACE);
                        state    <= S_LOOKUP;
                    end
                end
            end

            S_LOOKUP: begin
                w0_rd  <= way0[hash_ref];
                w1_rd  <= way1[hash_ref];
                w2_rd  <= way2[hash_ref];
                w3_rd  <= way3[hash_ref];
                idx_r  <= hash_ref;
                diff_q <= diff[18:0];
                px_bad <= under | (diff >= WINDOW);
                state  <= S_RESOLVE;
            end

            S_RESOLVE: begin
                hit0 <= m0;
                hit1 <= m1;
                hit2 <= m2;
                hit3 <= m3;
                add_tick_ok <= !px_bad && (q_back == {13'd0, diff_q});
                tick_r <= touch_tick;
                side_r <= (c_code == OP_ADD) ? c_side : eraw_side;
                state  <= S_LEVEL;
            end

            S_LEVEL: begin
                if (c_code == OP_ADD) begin
                    // a dropped add half of a replace still refreshes the
                    // bbo, the delete half already moved the book
                    if (!add_tick_ok) begin
                        cnt_outlier <= cnt_outlier + 1;
                        state <= c_was_rep ? S_FIND : S_IDLE;
                    end else if (any_hit_r) begin
                        cnt_miss <= cnt_miss + 1;   // dup ref, never happens
                        state <= c_was_rep ? S_FIND : S_IDLE;
                    end else if (!any_free) begin
                        cnt_overflow <= cnt_overflow + 1;
                        state <= c_was_rep ? S_FIND : S_IDLE;
                    end else begin
                        case (free_way)
                            2'd0: way0[idx_r] <= {1'b1, c_ref, c_side, tick_r, c_shares};
                            2'd1: way1[idx_r] <= {1'b1, c_ref, c_side, tick_r, c_shares};
                            2'd2: way2[idx_r] <= {1'b1, c_ref, c_side, tick_r, c_shares};
                            2'd3: way3[idx_r] <= {1'b1, c_ref, c_side, tick_r, c_shares};
                        endcase
                        if (side_r) bid_map[tick_r] <= 1'b1;
                        else        ask_map[tick_r] <= 1'b1;
                        state <= S_FIND;
                    end
                end else begin
                    if (!any_hit_r) begin
                        cnt_miss <= cnt_miss + 1;   // ref we never stored,
                        state    <= S_IDLE;         // usually out of window
                    end else begin
                        // shares come off the level in the shared port
                        // block, here we just retire the bitmap bit and
                        // rewrite the matched way in full
                        if (side_r) begin
                            if (lvl_sub == 32'd0) bid_map[tick_r] <= 1'b0;
                        end else begin
                            if (lvl_sub == 32'd0) ask_map[tick_r] <= 1'b0;
                        end
                        if (hit0) way0[idx_r] <= took_entry;
                        if (hit1) way1[idx_r] <= took_entry;
                        if (hit2) way2[idx_r] <= took_entry;
                        if (hit3) way3[idx_r] <= took_entry;
                        if (c_code == OP_REPLACE) begin
                            c_rep_pend <= 1'b1;
                            c_side     <= side_r;   // add half keeps the side
                            state      <= S_IDLE;
                        end else begin
                            state <= S_FIND;
                        end
                    end
                end
            end

            S_FIND: begin
                fb_ok   <= |bid_map;
                fa_ok   <= |ask_map;
                fb_tick <= fmsb;
                fa_tick <= flsb;
                state   <= S_SHARES;
            end

            S_SHARES: begin
                bid_present <= fb_ok;
                ask_present <= fa_ok;
                bid_tick    <= fb_tick;
                ask_tick    <= fa_tick;
                bid_shares  <= fb_ok ? bl_rd : 32'd0;
                ask_shares  <= fa_ok ? al_rd : 32'd0;
                bbo_valid   <= 1'b1;
                state       <= S_IDLE;
            end

            default: state <= S_IDLE;
            endcase
        end
    end

    always @(posedge clk)
        dbg_shares <= dbg_addr[TICKS_LOG2]
                        ? bid_levels[dbg_addr[TICKS_LOG2-1:0]]
                        : ask_levels[dbg_addr[TICKS_LOG2-1:0]];

    // keep xes out of the replace path before first use
    initial c_rep_pend = 1'b0;

endmodule

`default_nettype wire
