"""book_top vs the windowed reference. Every BBO strobe the hardware emits
gets compared against the Python prediction, real data first, then a
constrained random stream aimed at the corner cases."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

from itch_stream import (load_real, to_stream, n_msgs_env, TICKS, TICK,
                         build_torture)
from windowed_ref import WindowedBook

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model import itch  # noqa: E402


def predict(decoded, locate, base):
    ref = WindowedBook(locate, base)
    exp = []
    for m in decoded:
        if m is None or type(m).__name__ in ('StockDir', 'SystemEvent'):
            continue
        if ref.apply(m):
            exp.append(ref.bbo())
    return ref, exp


async def run_stream(dut, stream, locate, base, gap_pct=0.03):
    cocotb.start_soon(Clock(dut.clk, 10, 'ns').start())
    dut.rst.value = 1
    dut.in_valid.value = 0
    dut.cfg_locate.value = locate
    dut.cfg_base.value = base
    dut.dbg_addr.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)

    got = []

    async def monitor():
        while True:
            await RisingEdge(dut.clk)
            if dut.bbo_valid.value:
                got.append((
                    int(dut.bid_tick.value) if dut.bid_present.value else None,
                    int(dut.bid_shares.value),
                    int(dut.ask_tick.value) if dut.ask_present.value else None,
                    int(dut.ask_shares.value)))

    cocotb.start_soon(monitor())

    # the wire is allowed to pause anywhere, including inside a length
    # prefix, so pause it randomly and make sure nobody cares
    gaps = random.Random(1234)
    for b in stream:
        dut.in_valid.value = 1
        dut.in_data.value = b
        await RisingEdge(dut.clk)
        if gap_pct and gaps.random() < gap_pct:
            dut.in_valid.value = 0
            await ClockCycles(dut.clk, gaps.randrange(1, 4))
    dut.in_valid.value = 0
    await ClockCycles(dut.clk, 40)   # let the fifo drain
    return got


async def sweep_depth(dut, ref):
    """Read every level of both sides back through the debug port and
    compare against the reference, catches miscounts at ticks that never
    made it to best."""
    bad = []
    for side_bit, levels in ((1, ref.bid), (0, ref.ask)):
        for tick in range(TICKS):
            dut.dbg_addr.value = (side_bit << 12) | tick
            await ClockCycles(dut.clk, 2)
            want = levels.get(tick, 0)
            hw = int(dut.dbg_shares.value)
            if hw != want:
                bad.append((side_bit, tick, hw, want))
    for side_bit, tick, hw, want in bad[:10]:
        dut._log.error(f'depth mismatch side {side_bit} tick {tick}: '
                       f'hw {hw} ref {want}')
    assert not bad, f'{len(bad)} depth mismatches'


def check(dut, got, exp, ref, base):
    assert not dut.framing_err.value, 'framing error'
    assert not dut.fifo_overflow.value, 'op fifo overflowed'
    assert int(dut.cnt_overflow.value) == ref.overflow
    assert int(dut.cnt_filtered.value) == ref.filtered
    assert int(dut.cnt_outlier.value) == ref.outlier
    assert int(dut.cnt_miss.value) == ref.miss
    assert len(got) == len(exp), f'{len(got)} strobes vs {len(exp)} expected'
    for i, (g, e) in enumerate(zip(got, exp)):
        eb, ebs, ea, eas = e
        eg = (eb, ebs if eb is not None else 0,
              ea, eas if ea is not None else 0)
        assert g == eg, (
            f'strobe {i}: hw {g} vs ref {eg} '
            f'(prices hw bid {fmt(g[0], base)} ask {fmt(g[2], base)}, '
            f'ref bid {fmt(eg[0], base)} ask {fmt(eg[2], base)})')
    dut._log.info(f'{len(got)} bbo updates matched, fifo high water '
                  f'{int(dut.fifo_hwm.value)}, '
                  f'filtered {ref.filtered} outliers {ref.outlier} '
                  f'misses {ref.miss}')


def fmt(tick, base):
    if tick is None:
        return '-'
    return f'{(base + tick * TICK) / 10000:.2f}'


@cocotb.test()
async def real_data_bbo(dut):
    n = n_msgs_env(20000)
    raw, decoded, locate, base = load_real(n)
    ref, exp = predict(decoded, locate, base)
    got = await run_stream(dut, to_stream(raw), locate, base)
    check(dut, got, exp, ref, base)
    await sweep_depth(dut, ref)
    dut._log.info('full depth sweep matched on both sides')


@cocotb.test()
async def random_torture(dut):
    locate, base = 7, 3000000
    msgs = build_torture(locate=locate, base=base)

    decoded = [itch.decode(m) for m in msgs]
    ref, exp = predict(decoded, locate, base)
    got = await run_stream(dut, to_stream(msgs), locate, base, gap_pct=0.05)
    check(dut, got, exp, ref, base)
    assert ref.overflow >= 2, 'collision attack failed to overflow a set'
    await sweep_depth(dut, ref)
