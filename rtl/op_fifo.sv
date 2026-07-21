`timescale 1ns/1ps
// small elastic fifo between the parser and a book, first word fall
// through. depth stays tiny because ops arrive slower than the book
// retires them, hwm exists to prove that after every run.

`default_nettype none

module op_fifo #(
    parameter W = 212,
    parameter LOG2 = 5
) (
    input  wire         clk,
    input  wire         rst,

    input  wire         wr,
    input  wire [W-1:0] wdata,

    output wire         rvalid,
    input  wire         rready,
    output wire [W-1:0] rdata,

    output reg  [31:0]  hwm,
    output reg          overflow      // must never rise
);

    localparam DEPTH = 1 << LOG2;

    reg [W-1:0] mem [0:DEPTH-1];
    reg [LOG2:0] wptr, rptr;
    wire [LOG2:0] fill = wptr - rptr;
    wire full  = (fill == DEPTH[LOG2:0]);
    wire empty = (fill == 0);
    wire [31:0] fill32 = {{(31-LOG2){1'b0}}, fill};

    assign rvalid = !empty;
    assign rdata  = mem[rptr[LOG2-1:0]];

    always @(posedge clk) begin
        if (rst) begin
            wptr <= 0;
            rptr <= 0;
            hwm <= 32'd0;
            overflow <= 1'b0;
        end else begin
            if (wr) begin
                if (full) overflow <= 1'b1;
                else begin
                    mem[wptr[LOG2-1:0]] <= wdata;
                    wptr <= wptr + 1;
                end
            end
            if (rvalid && rready)
                rptr <= rptr + 1;
            if (fill32 > hwm)
                hwm <= fill32;
        end
    end

`ifdef FORMAL
    reg f_past_valid = 1'b0;
    reg f_init = 1'b0;
    always @(posedge clk) f_past_valid <= 1'b1;
    always @(posedge clk) if (rst) f_init <= 1'b1;
    always @(*) if (!f_past_valid) assume(rst);

    // pointers can never drift more than depth apart, push respects full
    // and pop respects empty so this is inductive once reset has landed
    always @(*) if (f_init && !rst) assert(fill <= DEPTH[LOG2:0]);
`endif

endmodule

`default_nettype wire
