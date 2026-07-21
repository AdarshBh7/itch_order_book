"""Parser vs golden decoder, field for field, on real feed bytes."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

from itch_stream import load_real, to_stream, n_msgs_env

OPS = {'AddOrder': 0, 'Execute': 1, 'ExecutePx': 1, 'Cancel': 2,
       'Delete': 3, 'Replace': 4}


def expected_ops(decoded):
    out = []
    for m in decoded:
        k = type(m).__name__
        if k not in OPS:
            continue
        code = OPS[k]
        if k == 'AddOrder':
            out.append((code, m.locate, m.ref, int(m.side == b'B'),
                        m.shares, m.price, None))
        elif k in ('Execute', 'ExecutePx', 'Cancel'):
            out.append((code, m.locate, m.ref, None, m.shares, None, None))
        elif k == 'Delete':
            out.append((code, m.locate, m.ref, None, None, None, None))
        else:
            out.append((code, m.locate, m.old_ref, None, m.shares,
                        m.price, m.new_ref))
    return out


@cocotb.test()
async def parser_matches_golden(dut):
    n = n_msgs_env(20000)
    raw, decoded, _, _ = load_real(n)
    stream = to_stream(raw)
    exp = expected_ops(decoded)

    cocotb.start_soon(Clock(dut.clk, 10, 'ns').start())
    dut.rst.value = 1
    dut.in_valid.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)

    got = []
    msgs_seen = 0

    async def monitor():
        nonlocal msgs_seen
        while True:
            await RisingEdge(dut.clk)
            if dut.msg_valid.value:
                msgs_seen += 1
            if dut.op_valid.value:
                # only sample fields the op actually drives, the rest can
                # be x from reset until first touched
                code = int(dut.op_code.value)
                got.append((code,
                            int(dut.op_locate.value),
                            int(dut.op_ref.value),
                            int(dut.op_side.value),
                            int(dut.op_shares.value) if code in (0, 1, 2, 4) else None,
                            int(dut.op_price.value) if code in (0, 4) else None,
                            int(dut.op_ref2.value) if code == 4 else None))

    cocotb.start_soon(monitor())

    gaps = random.Random(99)
    for b in stream:
        dut.in_valid.value = 1
        dut.in_data.value = b
        await RisingEdge(dut.clk)
        if gaps.random() < 0.03:
            dut.in_valid.value = 0
            await ClockCycles(dut.clk, gaps.randrange(1, 4))
    dut.in_valid.value = 0
    await ClockCycles(dut.clk, 5)

    assert not dut.framing_err.value, 'framing error on real data'
    assert msgs_seen == len(raw), f'saw {msgs_seen} msgs, sent {len(raw)}'
    assert len(got) == len(exp), f'{len(got)} ops vs {len(exp)} expected'

    for i, (g, e) in enumerate(zip(got, exp)):
        code, locate, ref, side, shares, price, ref2 = e
        assert g[0] == code and g[1] == locate and g[2] == ref, \
            f'op {i}: code/locate/ref mismatch {g} vs {e}'
        if side is not None:
            assert g[3] == side, f'op {i}: side {g[3]} vs {side}'
        if shares is not None:
            assert g[4] == shares, f'op {i}: shares {g[4]} vs {shares}'
        if price is not None:
            assert g[5] == price, f'op {i}: price {g[5]} vs {price}'
        if ref2 is not None:
            assert g[6] == ref2, f'op {i}: ref2 {g[6]} vs {ref2}'

    dut._log.info(f'parser clean on {len(raw)} real messages, '
                  f'{len(got)} book ops checked')


@cocotb.test()
async def corrupt_frame_goes_dead(dut):
    """A frame whose length disagrees with its type has to kill the
    parser, not resync it onto garbage. Feed real traffic, corrupt one
    frame, then keep feeding real traffic and demand total silence."""
    import struct
    raw, decoded, _, _ = load_real(400)
    exp_good = len(expected_ops(decoded[:200]))

    cocotb.start_soon(Clock(dut.clk, 10, 'ns').start())
    dut.rst.value = 1
    dut.in_valid.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)

    ops = 0
    msgs = 0

    async def monitor():
        nonlocal ops, msgs
        while True:
            await RisingEdge(dut.clk)
            if dut.op_valid.value:
                ops += 1
            if dut.msg_valid.value:
                msgs += 1

    cocotb.start_soon(monitor())

    async def feed(stream):
        for b in stream:
            dut.in_valid.value = 1
            dut.in_data.value = b
            await RisingEdge(dut.clk)
        dut.in_valid.value = 0

    await feed(to_stream(raw[:200]))
    await ClockCycles(dut.clk, 5)
    assert not dut.framing_err.value
    assert ops == exp_good, f'{ops} ops before corruption, wanted {exp_good}'

    # an A message body chopped to 20 bytes, length field agrees with the
    # frame so only the per type check can catch it
    await feed(struct.pack('>H', 20) + b'A' + bytes(19))
    await ClockCycles(dut.clk, 5)
    assert dut.framing_err.value, 'length/type mismatch not flagged'

    before = (ops, msgs)
    await feed(to_stream(raw[200:400]))
    await ClockCycles(dut.clk, 10)
    assert (ops, msgs) == before, 'parser emitted after going dead'
    dut._log.info(f'parser went dead after corrupt frame and stayed '
                  f'silent through {len(raw) - 200} further messages')
