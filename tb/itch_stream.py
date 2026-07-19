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


def load_real(n_msgs, symbol=b'AAPL', other_every=500, scan_cap=3_000_000):
    """Real feed messages, densified: every message for the chosen symbol
    plus one in other_every of everything else, order preserved. Premarket
    flow for one symbol is sparse and simulators are slow, this keeps the
    bench minutes long while still hammering the locate filter. The full
    unfiltered day goes through the verilator harness instead."""
    raw, decoded = [], []
    locate = None
    base = None
    kept_other = 0
    for i, (t, msg) in enumerate(itch.messages(DATA, scan_cap)):
        if len(raw) >= n_msgs:
            break
        m = itch.decode(msg)
        k = type(m).__name__ if m is not None else None
        if k == 'StockDir' and m.stock == symbol:
            locate = m.locate
        mine = m is not None and getattr(m, 'locate', None) == locate \
            and locate is not None
        kept_other += 1
        if not mine and kept_other % other_every:
            continue
        raw.append(msg)
        decoded.append(m)
        if mine and base is None and k == 'AddOrder':
            base = max(0, m.price - (TICKS // 2) * TICK)
            base -= base % TICK
    if locate is None:
        raise RuntimeError(f'{symbol} never appeared in the slice')
    if base is None:
        base = 0
    return raw, decoded, locate, base


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
