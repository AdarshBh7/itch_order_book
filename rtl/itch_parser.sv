`timescale 1ns/1ps
// itch 5.0 parser. byte stream in, decoded book ops out.
//
// framing is the sample-file style: 2 byte big endian length, then the
// message. body bytes land in a buffer and decode fires the cycle after
// the last byte, so op_valid trails the wire by exactly one cycle.

`default_nettype none

module itch_parser (
    input  wire        clk,
    input  wire        rst,

    input  wire        in_valid,
    input  wire [7:0]  in_data,

    // book op, valid for one cycle
    output reg         op_valid,
    output reg  [2:0]  op_code,
    output reg  [15:0] op_locate,
    output reg  [63:0] op_ref,
    output reg         op_side,      // 1 = buy
    output reg  [31:0] op_shares,
    output reg  [31:0] op_price,
    output reg  [63:0] op_ref2,      // new ref for replace
    output reg  [47:0] op_ts,

    // every message, book related or not
    output reg         msg_valid,
    output reg  [7:0]  msg_type,

    output reg         framing_err   // sticky, should never fire
);

    localparam OP_ADD     = 3'd0;
    localparam OP_EXEC    = 3'd1;   // E and C both, shares sit at the same offset
    localparam OP_CANCEL  = 3'd2;
    localparam OP_DELETE  = 3'd3;
    localparam OP_REPLACE = 3'd4;

    localparam MAX_LEN = 50;        // longest itch 5.0 message (NOII)

    localparam S_LEN_HI = 2'd0;
    localparam S_LEN_LO = 2'd1;
    localparam S_BODY   = 2'd2;

    reg [1:0]  state;
    reg [15:0] msg_len;
    reg [15:0] cnt;
    reg [7:0]  buf_ [0:MAX_LEN-1];
    reg        dec_pend;

    wire last_byte = (state == S_BODY) && in_valid && (cnt == msg_len - 1);

    always @(posedge clk) begin
        if (rst) begin
            state       <= S_LEN_HI;
            framing_err <= 1'b0;
            dec_pend    <= 1'b0;
        end else begin
            dec_pend <= last_byte;
            case (state)
                S_LEN_HI: if (in_valid) begin
                    msg_len[15:8] <= in_data;
                    state <= S_LEN_LO;
                end
                S_LEN_LO: if (in_valid) begin
                    msg_len[7:0] <= in_data;
                    cnt <= 16'd0;
                    if ({msg_len[15:8], in_data} == 16'd0 ||
                        {msg_len[15:8], in_data} > MAX_LEN)
                        framing_err <= 1'b1;   // stream is unrecoverable past this
                    else
                        state <= S_BODY;
                end
                S_BODY: if (in_valid) begin
                    buf_[cnt[5:0]] <= in_data;
                    cnt <= cnt + 16'd1;
                    if (cnt == msg_len - 1)
                        state <= S_LEN_HI;
                end
                default: state <= S_LEN_HI;
            endcase
        end
    end

    // by the time dec_pend is up the whole body including the final byte has
    // been written, back to back messages cannot clobber buf_[0] until three
    // cycles later so reading here is safe
    wire [7:0] mtype = buf_[0];

    function [63:0] f64(input [15:0] o);
        f64 = {buf_[o], buf_[o+1], buf_[o+2], buf_[o+3],
               buf_[o+4], buf_[o+5], buf_[o+6], buf_[o+7]};
    endfunction
    function [31:0] f32(input [15:0] o);
        f32 = {buf_[o], buf_[o+1], buf_[o+2], buf_[o+3]};
    endfunction
    function [15:0] f16(input [15:0] o);
        f16 = {buf_[o], buf_[o+1]};
    endfunction

    always @(posedge clk) begin
        if (rst) begin
            op_valid  <= 1'b0;
            msg_valid <= 1'b0;
        end else begin
            op_valid  <= 1'b0;
            msg_valid <= 1'b0;
            if (dec_pend) begin
                msg_valid <= 1'b1;
                msg_type  <= mtype;
                op_locate <= f16(1);
                op_ts     <= {f16(5), f32(7)};
                op_ref    <= f64(11);
                op_side   <= (buf_[19] == "B");
                case (mtype)
                    "A", "F": begin
                        op_valid  <= 1'b1;
                        op_code   <= OP_ADD;
                        op_shares <= f32(20);
                        op_price  <= f32(32);
                    end
                    "E", "C": begin
                        op_valid  <= 1'b1;
                        op_code   <= OP_EXEC;
                        op_shares <= f32(19);
                    end
                    "X": begin
                        op_valid  <= 1'b1;
                        op_code   <= OP_CANCEL;
                        op_shares <= f32(19);
                    end
                    "D": begin
                        op_valid <= 1'b1;
                        op_code  <= OP_DELETE;
                    end
                    "U": begin
                        op_valid  <= 1'b1;
                        op_code   <= OP_REPLACE;
                        op_ref2   <= f64(19);
                        op_shares <= f32(27);
                        op_price  <= f32(31);
                    end
                    default: ;
                endcase
            end
        end
    end

endmodule

`default_nettype wire
