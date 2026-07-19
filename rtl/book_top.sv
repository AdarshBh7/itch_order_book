`timescale 1ns/1ps
// parser feeding the book through a small elastic fifo. the wire side has
// no backpressure, bytes show up when they show up, so ops queue here while
// the book grinds through its fixed cycle update. shortest itch message is
// 21 bytes on the wire and the book worst case is 10 cycles, so the fifo
// never builds depth. fifo_hwm proves that claim after a run.

`default_nettype none

module book_top #(
    parameter TICKS_LOG2 = 12,
    parameter SETS_LOG2  = 11,
    parameter FIFO_LOG2  = 5
) (
    input  wire        clk,
    input  wire        rst,

    input  wire [15:0] cfg_locate,
    input  wire [31:0] cfg_base,

    input  wire        in_valid,
    input  wire [7:0]  in_data,

    output wire                  bbo_valid,
    output wire                  bid_present,
    output wire [TICKS_LOG2-1:0] bid_tick,
    output wire [31:0]           bid_shares,
    output wire                  ask_present,
    output wire [TICKS_LOG2-1:0] ask_tick,
    output wire [31:0]           ask_shares,

    output wire [31:0] cnt_filtered,
    output wire [31:0] cnt_outlier,
    output wire [31:0] cnt_miss,
    output wire [31:0] cnt_overflow,
    output reg  [31:0] fifo_hwm,
    output reg         fifo_overflow,     // must never rise
    output wire        framing_err,

    input  wire [TICKS_LOG2:0] dbg_addr,
    output wire [31:0]         dbg_shares
);

    wire        p_valid;
    wire [2:0]  p_code;
    wire [15:0] p_locate;
    wire [63:0] p_ref;
    wire        p_side;
    wire [31:0] p_shares;
    wire [31:0] p_price;
    wire [63:0] p_ref2;

    itch_parser parser (
        .clk(clk), .rst(rst),
        .in_valid(in_valid), .in_data(in_data),
        .op_valid(p_valid), .op_code(p_code), .op_locate(p_locate),
        .op_ref(p_ref), .op_side(p_side), .op_shares(p_shares),
        .op_price(p_price), .op_ref2(p_ref2), .op_ts(),
        .msg_valid(), .msg_type(),
        .framing_err(framing_err)
    );

    localparam OPW = 3 + 16 + 64 + 1 + 32 + 32 + 64;
    localparam DEPTH = 1 << FIFO_LOG2;

    reg [OPW-1:0] fifo [0:DEPTH-1];
    reg [FIFO_LOG2:0] wptr, rptr;
    wire [FIFO_LOG2:0] fill = wptr - rptr;
    wire full  = (fill == DEPTH[FIFO_LOG2:0]);
    wire empty = (fill == 0);
    wire [31:0] fill32 = {{(31-FIFO_LOG2){1'b0}}, fill};

    wire        b_ready;
    wire        b_valid = !empty;
    reg [OPW-1:0] rdata;

    wire pop = b_valid && b_ready;

    always @(posedge clk) begin
        if (rst) begin
            wptr <= 0;
            rptr <= 0;
            fifo_hwm <= 32'd0;
            fifo_overflow <= 1'b0;
        end else begin
            if (p_valid) begin
                if (full) begin
                    fifo_overflow <= 1'b1;
                end else begin
                    fifo[wptr[FIFO_LOG2-1:0]] <=
                        {p_code, p_locate, p_ref, p_side, p_shares, p_price, p_ref2};
                    wptr <= wptr + 1;
                end
            end
            if (pop)
                rptr <= rptr + 1;
            if (fill32 > fifo_hwm)
                fifo_hwm <= fill32;
        end
    end

    // first word fall through so a lone op does not sit in the queue
    always @(*)
        rdata = fifo[rptr[FIFO_LOG2-1:0]];

    order_book #(
        .TICKS_LOG2(TICKS_LOG2),
        .SETS_LOG2(SETS_LOG2)
    ) book (
        .clk(clk), .rst(rst),
        .cfg_locate(cfg_locate), .cfg_base(cfg_base),
        .op_valid(b_valid), .op_ready(b_ready),
        .op_code(rdata[OPW-1 -: 3]),
        .op_locate(rdata[OPW-4 -: 16]),
        .op_ref(rdata[OPW-20 -: 64]),
        .op_side(rdata[128]),
        .op_shares(rdata[127:96]),
        .op_price(rdata[95:64]),
        .op_ref2(rdata[63:0]),
        .bbo_valid(bbo_valid),
        .bid_present(bid_present), .bid_tick(bid_tick), .bid_shares(bid_shares),
        .ask_present(ask_present), .ask_tick(ask_tick), .ask_shares(ask_shares),
        .cnt_filtered(cnt_filtered), .cnt_outlier(cnt_outlier),
        .cnt_miss(cnt_miss), .cnt_overflow(cnt_overflow),
        .dbg_addr(dbg_addr), .dbg_shares(dbg_shares)
    );

endmodule

`default_nettype wire
