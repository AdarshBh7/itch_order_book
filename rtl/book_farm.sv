`timescale 1ns/1ps
// n books off one parser, one symbol each. the farm filters at the fifo
// mouth so a book only ever queues its own symbol's ops, which is also
// why this scales: the parser runs at wire rate no matter what and each
// book only sees its own slice of the flow. per symbol cost is one fifo
// plus one book core, nothing shared but the parser.

`default_nettype none

module book_farm #(
    parameter N_BOOKS    = 4,
    parameter TICKS_LOG2 = 12,
    parameter SETS_LOG2  = 11,
    parameter FIFO_LOG2  = 5
) (
    input  wire        clk,
    input  wire        rst,

    input  wire [N_BOOKS*16-1:0] cfg_locates,
    input  wire [N_BOOKS*32-1:0] cfg_bases,

    input  wire        in_valid,
    input  wire [7:0]  in_data,

    output wire [N_BOOKS-1:0]             bbo_valid,
    output wire [N_BOOKS-1:0]             bid_present,
    output wire [N_BOOKS*TICKS_LOG2-1:0]  bid_tick,
    output wire [N_BOOKS*32-1:0]          bid_shares,
    output wire [N_BOOKS-1:0]             ask_present,
    output wire [N_BOOKS*TICKS_LOG2-1:0]  ask_tick,
    output wire [N_BOOKS*32-1:0]          ask_shares,

    output wire [N_BOOKS*32-1:0] cnt_outlier,
    output wire [N_BOOKS*32-1:0] cnt_miss,
    output wire [N_BOOKS*32-1:0] cnt_overflow,
    output wire [N_BOOKS*32-1:0] fifo_hwm,
    output wire [N_BOOKS-1:0]    fifo_overflow,
    output wire                  framing_err
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

    genvar i;
    generate
    for (i = 0; i < N_BOOKS; i = i + 1) begin : g
        wire [15:0] my_locate = cfg_locates[i*16 +: 16];
        wire        b_valid, b_ready;
        wire [OPW-1:0] rdata;

        op_fifo #(.W(OPW), .LOG2(FIFO_LOG2)) fifo (
            .clk(clk), .rst(rst),
            .wr(p_valid && (p_locate == my_locate)),
            .wdata({p_code, p_locate, p_ref, p_side, p_shares, p_price, p_ref2}),
            .rvalid(b_valid), .rready(b_ready), .rdata(rdata),
            .hwm(fifo_hwm[i*32 +: 32]), .overflow(fifo_overflow[i])
        );

        order_book #(
            .TICKS_LOG2(TICKS_LOG2),
            .SETS_LOG2(SETS_LOG2)
        ) book (
            .clk(clk), .rst(rst),
            .cfg_locate(my_locate), .cfg_base(cfg_bases[i*32 +: 32]),
            .op_valid(b_valid), .op_ready(b_ready),
            .op_code(rdata[OPW-1 -: 3]),
            .op_locate(rdata[OPW-4 -: 16]),
            .op_ref(rdata[OPW-20 -: 64]),
            .op_side(rdata[128]),
            .op_shares(rdata[127:96]),
            .op_price(rdata[95:64]),
            .op_ref2(rdata[63:0]),
            .bbo_valid(bbo_valid[i]),
            .bid_present(bid_present[i]),
            .bid_tick(bid_tick[i*TICKS_LOG2 +: TICKS_LOG2]),
            .bid_shares(bid_shares[i*32 +: 32]),
            .ask_present(ask_present[i]),
            .ask_tick(ask_tick[i*TICKS_LOG2 +: TICKS_LOG2]),
            .ask_shares(ask_shares[i*32 +: 32]),
            .cnt_filtered(),
            .cnt_outlier(cnt_outlier[i*32 +: 32]),
            .cnt_miss(cnt_miss[i*32 +: 32]),
            .cnt_overflow(cnt_overflow[i*32 +: 32]),
            .dbg_addr({(TICKS_LOG2+1){1'b0}}), .dbg_shares()
        );
    end
    endgenerate

endmodule

`default_nettype wire
