`timescale 1ns/1ps
// itch 5.0 parser. byte stream in, decoded book ops out.
//
// framing is the sample-file style: 2 byte big endian length, then the
// message. body bytes land in a buffer, decode fires the cycle after the
// last byte and registers its outputs, so op_valid shows up two cycles
// after the final byte on the wire.
//
// field contract: each op only drives the fields that itch message
// carries, everything else holds stale bytes. consumers key off op_code
// and read nothing extra. lengths get checked against the spec table for
// known types, any corrupt frame trips framing_err and the parser goes
// dead until reset rather than guessing at resync and feeding the book
// phantom orders.

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

    localparam S_DEAD   = 2'd3;

    reg [1:0]  state;
    reg [15:0] msg_len;
    reg [15:0] cnt;
    reg [7:0]  buf_ [0:MAX_LEN-1];
    reg        dec_pend;

    integer bi;
    initial for (bi = 0; bi < MAX_LEN; bi = bi + 1) buf_[bi] = 8'd0;

    wire [7:0] mtype = buf_[0];

    // spec lengths for every itch 5.0 type, 0 means unknown to us
    function [7:0] explen_f(input [7:0] t);
        case (t)
            "S": explen_f = 8'd12;  "R": explen_f = 8'd39;
            "H": explen_f = 8'd25;  "Y": explen_f = 8'd20;
            "L": explen_f = 8'd26;  "V": explen_f = 8'd35;
            "W": explen_f = 8'd12;  "K": explen_f = 8'd28;
            "J": explen_f = 8'd35;  "h": explen_f = 8'd21;
            "A": explen_f = 8'd36;  "F": explen_f = 8'd40;
            "E": explen_f = 8'd31;  "C": explen_f = 8'd36;
            "X": explen_f = 8'd23;  "D": explen_f = 8'd19;
            "U": explen_f = 8'd35;  "P": explen_f = 8'd44;
            "Q": explen_f = 8'd40;  "B": explen_f = 8'd19;
            "I": explen_f = 8'd50;  "N": explen_f = 8'd20;
            "O": explen_f = 8'd48;
            default: explen_f = 8'd0;
        endcase
    endfunction

    wire [7:0] explen = explen_f(mtype);
    wire dec_len_bad = (explen != 8'd0) && (msg_len != {8'd0, explen});

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
                        {msg_len[15:8], in_data} > MAX_LEN) begin
                        framing_err <= 1'b1;   // no honest resync from here
                        state <= S_DEAD;
                    end else
                        state <= S_BODY;
                end
                S_BODY: if (in_valid) begin
                    buf_[cnt[5:0]] <= in_data;
                    cnt <= cnt + 16'd1;
                    if (cnt == msg_len - 1)
                        state <= S_LEN_HI;
                end
                S_DEAD: state <= S_DEAD;       // parked until reset
                default: state <= S_LEN_HI;
            endcase
            // last assignment wins, a spec length mismatch overrides
            // whatever the framing fsm wanted to do next
            if (dec_pend && dec_len_bad) begin
                framing_err <= 1'b1;
                state <= S_DEAD;
            end
        end
    end

    // by the time dec_pend is up the whole body including the final byte has
    // been written, back to back messages cannot clobber buf_[0] until three
    // cycles later so reading here is safe
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
            if (dec_pend && !dec_len_bad && !framing_err) begin
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
