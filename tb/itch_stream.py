"""Shared helpers for the cocotb benches: real-data loading, synthetic
message building, and the byte feeder."""

import os
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from model import itch  # noqa: E402

DATA = REPO / 'data' / 'itch_prefix.gz'
TICKS = 4096
TICK = 100


def load_real_multi(n_msgs, symbols, other_every=500, scan_cap=3_000_000):
    """Real feed messages, densified: every message for the chosen symbols
    plus one in other_every of everything else, order preserved. Premarket
    flow per symbol is sparse and simulators are slow, this keeps the
    bench minutes long while still hammering the locate filter. The full
    unfiltered day goes through the verilator harness instead. Returns
    raw, decoded, {symbol: locate}, {symbol: base}."""
    raw, decoded = [], []
    locates = {}
    bases = {}
    by_locate = {}
    kept_other = 0
    for i, (t, msg) in enumerate(itch.messages(DATA, scan_cap)):
        if len(raw) >= n_msgs:
            break
        m = itch.decode(msg)
        k = type(m).__name__ if m is not None else None
        if k == 'StockDir' and m.stock in symbols:
            locates[m.stock] = m.locate
            by_locate[m.locate] = m.stock
        sym = by_locate.get(getattr(m, 'locate', None)) if m is not None else None
        kept_other += 1
        if sym is None and kept_other % other_every:
            continue
        raw.append(msg)
        decoded.append(m)
        if sym is not None and sym not in bases and k == 'AddOrder':
            b = max(0, m.price - (TICKS // 2) * TICK)
            bases[sym] = b - b % TICK
    for s in symbols:
        if s not in locates:
            raise RuntimeError(f'{s} never appeared in the slice')
        bases.setdefault(s, 0)
    return raw, decoded, locates, bases


def load_real(n_msgs, symbol=b'AAPL', other_every=500, scan_cap=3_000_000):
    raw, decoded, locates, bases = load_real_multi(
        n_msgs, (symbol,), other_every, scan_cap)
    return raw, decoded, locates[symbol], bases[symbol]


def to_stream(raw_msgs):
    out = bytearray()
    for m in raw_msgs:
        out += struct.pack('>H', len(m)) + m
    return bytes(out)


TS6 = (0).to_bytes(6, 'big')


def m_add(locate, ref, side, shares, price, mpid=False):
    body = (b'F' if mpid else b'A') + struct.pack('>H', locate) + b'\x00\x00' \
        + TS6 + struct.pack('>Q', ref) + (b'B' if side else b'S') \
        + struct.pack('>I', shares) + b'TESTSTK ' + struct.pack('>I', price)
    return body + (b'MPID' if mpid else b'')


def m_exec(locate, ref, shares, with_price=None):
    body = (b'C' if with_price is not None else b'E') \
        + struct.pack('>H', locate) + b'\x00\x00' + TS6 \
        + struct.pack('>Q', ref) + struct.pack('>I', shares) \
        + struct.pack('>Q', 7)
    if with_price is not None:
        body += b'Y' + struct.pack('>I', with_price)
    return body


def m_cancel(locate, ref, shares):
    return b'X' + struct.pack('>H', locate) + b'\x00\x00' + TS6 \
        + struct.pack('>Q', ref) + struct.pack('>I', shares)


def m_delete(locate, ref):
    return b'D' + struct.pack('>H', locate) + b'\x00\x00' + TS6 \
        + struct.pack('>Q', ref)


def m_replace(locate, old_ref, new_ref, shares, price):
    return b'U' + struct.pack('>H', locate) + b'\x00\x00' + TS6 \
        + struct.pack('>Q', old_ref) + struct.pack('>Q', new_ref) \
        + struct.pack('>I', shares) + struct.pack('>I', price)


def n_msgs_env(default):
    return int(os.environ.get('ITCH_N_MSGS', default))


def build_torture(locate=7, noise=9, base=3000000, n_ops=None, seed=20260719):
    if n_ops is None:
        n_ops = int(os.environ.get('ITCH_TORTURE_OPS', 6000))
    """Deterministic hostile stimulus. One builder shared by the bench and
    any debug script so a repro is always the real thing."""
    import random
    rng = random.Random(seed)
    live = []
    next_ref = 1000
    msgs = []

    def fresh_ref():
        nonlocal next_ref
        next_ref += rng.choice([2, 4, 6])
        return next_ref

    # pin the corners of the tick space on both sides so the priority
    # encoders get exercised at group 0 bit 0 and group 63 bit 63
    for tick, side in ((0, True), (0, False), (4095, True), (4095, False)):
        msgs.append(m_add(locate, fresh_ref(), side, 100, base + tick * TICK))

    # refs 2^33 apart hash to the same set, six of them overflows the
    # four ways and the drop path has to match the model exactly
    for k in range(6):
        msgs.append(m_add(locate, 500001 + (k << 33), True, 10 + k,
                          base + (500 + k) * TICK))

    # duplicate ref adds, plain and via a replace's new_ref
    msgs.append(m_add(locate, 500001, False, 77, base + 600 * TICK))
    dup_target = fresh_ref()
    msgs.append(m_add(locate, dup_target, True, 55, base + 700 * TICK))
    victim = fresh_ref()
    msgs.append(m_add(locate, victim, True, 66, base + 701 * TICK))
    msgs.append(m_replace(locate, victim, dup_target, 44, base + 702 * TICK))

    for _ in range(n_ops):
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
            msgs.append(m_replace(locate, old, new, rng.randrange(1, 5000), price))
            if old in live and loc == locate:
                live.remove(old)
                live.append(new)

    return msgs
