"""book_top vs the windowed reference. Every BBO strobe the hardware emits
gets compared against the Python prediction, real data first, then a
constrained random stream aimed at the corner cases."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

from itch_stream import (load_real, to_stream, n_msgs_env, TICKS, TICK,
                         m_add, m_exec, m_cancel, m_delete, m_replace)
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


async def run_stream(dut, stream, locate, base):
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

    for b in stream:
        dut.in_valid.value = 1
        dut.in_data.value = b
        await RisingEdge(dut.clk)
    dut.in_valid.value = 0
    await ClockCycles(dut.clk, 40)   # let the fifo drain
    return got


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


@cocotb.test()
async def random_torture(dut):
    rng = random.Random(20260719)
    locate, noise = 7, 9
    base = 3000000
    live = []
    next_ref = 1000
    msgs = []

    def fresh_ref():
        nonlocal next_ref
        next_ref += rng.choice([2, 4, 6])
        return next_ref

    for _ in range(6000):
        r = rng.random()
        loc = noise if rng.random() < 0.15 else locate
        if r < 0.40 or not live:
            price = base + rng.randrange(0, TICKS) * TICK
            if rng.random() < 0.05:
                price = base + rng.randrange(0, TICKS * TICK)  # maybe off tick
            if rng.random() < 0.05:
                price = base + TICKS * TICK + rng.randrange(0, 500000)
            if rng.random() < 0.02 and base > 0:
                price = rng.randrange(0, base)                 # below window
            ref = fresh_ref()
            msgs.append(m_add(loc, ref, rng.random() < 0.5,
                              rng.randrange(1, 5000),
                              price, mpid=rng.random() < 0.1))
            if loc == locate:
                live.append(ref)
        elif r < 0.60:
            ref = rng.choice(live) if rng.random() < 0.9 else fresh_ref()
            shares = rng.randrange(1, 8000)   # sometimes more than resting
            px = base + rng.randrange(0, TICKS) * TICK
            msgs.append(m_exec(loc, ref, shares,
                               with_price=px if rng.random() < 0.3 else None))
        elif r < 0.75:
            ref = rng.choice(live) if rng.random() < 0.9 else fresh_ref()
            msgs.append(m_cancel(loc, ref, rng.randrange(1, 6000)))
        elif r < 0.88:
            ref = rng.choice(live) if rng.random() < 0.9 else fresh_ref()
            msgs.append(m_delete(loc, ref))
            if ref in live and loc == locate:
                live.remove(ref)
        else:
            old = rng.choice(live) if rng.random() < 0.9 else fresh_ref()
            new = fresh_ref()
            price = base + rng.randrange(0, TICKS) * TICK
            if rng.random() < 0.10:
                price = base + TICKS * TICK + 12345   # replace out of window
            msgs.append(m_replace(loc, old, new, rng.randrange(1, 5000), price))
            if old in live and loc == locate:
                live.remove(old)
                live.append(new)

    decoded = [itch.decode(m) for m in msgs]
    ref, exp = predict(decoded, locate, base)
    got = await run_stream(dut, to_stream(msgs), locate, base)
    check(dut, got, exp, ref, base)
