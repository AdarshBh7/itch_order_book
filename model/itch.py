"""NASDAQ ITCH 5.0 decoder. Spec: Nasdaq TotalView-ITCH 5.0, rev 5.0.

Messages come length-prefixed in the public sample files: 2-byte big-endian
length, then the message. All multi-byte ints are big-endian, prices have 4
implied decimals, timestamps are ns since midnight packed into 6 bytes.
"""

import gzip
import struct
from collections import namedtuple

# total message length by type, not counting the 2-byte framing prefix
MSG_LEN = {
    b'S': 12, b'R': 39, b'H': 25, b'Y': 20, b'L': 26,
    b'V': 35, b'W': 12, b'K': 28, b'J': 35, b'h': 21,
    b'A': 36, b'F': 40, b'E': 31, b'C': 36, b'X': 23,
    b'D': 19, b'U': 35, b'P': 44, b'Q': 40, b'B': 19,
    b'I': 50, b'N': 20, b'O': 48,
}

AddOrder = namedtuple('AddOrder', 'ts locate ref side shares stock price')
Execute = namedtuple('Execute', 'ts locate ref shares match')
ExecutePx = namedtuple('ExecutePx', 'ts locate ref shares match printable price')
Cancel = namedtuple('Cancel', 'ts locate ref shares')
Delete = namedtuple('Delete', 'ts locate ref')
Replace = namedtuple('Replace', 'ts locate old_ref new_ref shares price')
StockDir = namedtuple('StockDir', 'ts locate stock')
SystemEvent = namedtuple('SystemEvent', 'ts code')


def _ts(buf, off):
    # 6-byte timestamp, high 2 bytes then low 4
    hi, lo = struct.unpack_from('>HI', buf, off)
    return (hi << 32) | lo


def decode(msg):
    """Decode one message. Returns a namedtuple for book-relevant types,
    None for everything else (still counted by the caller)."""
    t = msg[0:1]
    if t == b'A' or t == b'F':
        locate, = struct.unpack_from('>H', msg, 1)
        ts = _ts(msg, 5)
        ref, side, shares, stock, price = struct.unpack_from('>QcI8sI', msg, 11)
        return AddOrder(ts, locate, ref, side, shares, stock.rstrip(), price)
    if t == b'E':
        locate, = struct.unpack_from('>H', msg, 1)
        ts = _ts(msg, 5)
        ref, shares, match = struct.unpack_from('>QIQ', msg, 11)
        return Execute(ts, locate, ref, shares, match)
    if t == b'C':
        locate, = struct.unpack_from('>H', msg, 1)
        ts = _ts(msg, 5)
        ref, shares, match, printable, price = struct.unpack_from('>QIQcI', msg, 11)
        return ExecutePx(ts, locate, ref, shares, match, printable, price)
    if t == b'X':
        locate, = struct.unpack_from('>H', msg, 1)
        ts = _ts(msg, 5)
        ref, shares = struct.unpack_from('>QI', msg, 11)
        return Cancel(ts, locate, ref, shares)
    if t == b'D':
        locate, = struct.unpack_from('>H', msg, 1)
        ts = _ts(msg, 5)
        ref, = struct.unpack_from('>Q', msg, 11)
        return Delete(ts, locate, ref)
    if t == b'U':
        locate, = struct.unpack_from('>H', msg, 1)
        ts = _ts(msg, 5)
        old_ref, new_ref, shares, price = struct.unpack_from('>QQII', msg, 11)
        return Replace(ts, locate, old_ref, new_ref, shares, price)
    if t == b'R':
        locate, = struct.unpack_from('>H', msg, 1)
        ts = _ts(msg, 5)
        stock, = struct.unpack_from('>8s', msg, 11)
        return StockDir(ts, locate, stock.rstrip())
    if t == b'S':
        ts = _ts(msg, 5)
        return SystemEvent(ts, msg[11:12])
    return None


def messages(path, max_msgs=None):
    """Yield (msg_type, raw_bytes) from a length-prefixed ITCH file,
    gzipped or not. Stops cleanly at a truncated tail."""
    opener = gzip.open if str(path).endswith('.gz') else open
    n = 0
    with opener(path, 'rb') as f:
        while True:
            try:
                hdr = f.read(2)
                if len(hdr) < 2:
                    return
                (length,) = struct.unpack('>H', hdr)
                msg = f.read(length)
            except EOFError:
                return  # sliced .gz ends mid-stream, that's fine
            if len(msg) < length:
                return  # truncated slice, done
            t = msg[0:1]
            expect = MSG_LEN.get(t)
            if expect is not None and expect != length:
                raise ValueError(f'bad length for {t}: got {length} want {expect}')
            yield t, msg
            n += 1
            if max_msgs and n >= max_msgs:
                return
