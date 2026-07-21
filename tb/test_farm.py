"""book_farm with four symbols on real data, each book checked against
its own reference. Proof that scaling is really just replication."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

from itch_stream import load_real_multi, to_stream, n_msgs_env, TICKS
from windowed_ref import WindowedBook

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model import itch  # noqa: E402

SYMBOLS = (b'AAPL', b'SPY', b'TSLA', b'MSFT')
TICKS_LOG2 = 12


@cocotb.test()
async def four_books_real_data(dut):
    n = n_msgs_env(20000)
    raw, decoded, locates, bases = load_real_multi(n, SYMBOLS)

    refs = [WindowedBook(locates[s], bases[s]) for s in SYMBOLS]
    exps = [[] for _ in SYMBOLS]
    for m in decoded:
        if m is None or type(m).__name__ in ('StockDir', 'SystemEvent'):
            continue
        for i, ref in enumerate(refs):
            if ref.apply(m):
                exps[i].append(ref.bbo())

    cocotb.start_soon(Clock(dut.clk, 10, 'ns').start())
    dut.rst.value = 1
    dut.in_valid.value = 0
    cfg_loc = 0
    cfg_base = 0
    for i, s in enumerate(SYMBOLS):
        cfg_loc |= locates[s] << (i * 16)
        cfg_base |= bases[s] << (i * 32)
    dut.cfg_locates.value = cfg_loc
    dut.cfg_bases.value = cfg_base
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)

    gots = [[] for _ in SYMBOLS]

    def sl(sig, i, w):
        # per book slice, lsb first indexing, tolerates x in other books
        bits = str(sig.value)[::-1][i * w:(i + 1) * w][::-1]
        return int(bits, 2)

    async def monitor():
        while True:
            await RisingEdge(dut.clk)
            v = int(dut.bbo_valid.value)
            if not v:
                continue
            bp = int(dut.bid_present.value)
            ap = int(dut.ask_present.value)
            for i in range(len(SYMBOLS)):
                if v >> i & 1:
                    gots[i].append((
                        sl(dut.bid_tick, i, TICKS_LOG2) if bp >> i & 1 else None,
                        sl(dut.bid_shares, i, 32) if bp >> i & 1 else 0,
                        sl(dut.ask_tick, i, TICKS_LOG2) if ap >> i & 1 else None,
                        sl(dut.ask_shares, i, 32) if ap >> i & 1 else 0))

    cocotb.start_soon(monitor())

    for b in to_stream(raw):
        dut.in_valid.value = 1
        dut.in_data.value = b
        await RisingEdge(dut.clk)
    dut.in_valid.value = 0
    await ClockCycles(dut.clk, 60)

    assert not dut.framing_err.value
    assert int(dut.fifo_overflow.value) == 0
    for i, s in enumerate(SYMBOLS):
        ref, exp, got = refs[i], exps[i], gots[i]
        exp_t = [(b, bs if b is not None else 0, a, asv if a is not None else 0)
                 for b, bs, a, asv in exp]
        assert int(dut.cnt_outlier.value) >> i * 32 & 0xFFFFFFFF == ref.outlier
        assert int(dut.cnt_miss.value) >> i * 32 & 0xFFFFFFFF == ref.miss
        assert int(dut.cnt_overflow.value) >> i * 32 & 0xFFFFFFFF == ref.overflow
        assert len(got) == len(exp_t), \
            f'{s}: {len(got)} strobes vs {len(exp_t)}'
        for j, (g, e) in enumerate(zip(got, exp_t)):
            assert g == e, f'{s} strobe {j}: hw {g} vs ref {e}'
        dut._log.info(f'{s.decode()}: {len(got)} bbo updates matched')
