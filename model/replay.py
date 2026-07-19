"""Replay an ITCH file through the reference book and print stats.

Usage:
    python -m model.replay data/itch_prefix.gz --max 2000000 --watch AAPL,SPY
"""

import argparse
import time
from collections import Counter

from . import itch
from .book import Market


def fmt_price(p):
    return f'{p / 10000:.4f}'


def fmt_ts(ns):
    s = ns // 1_000_000_000
    return f'{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}.{ns % 1_000_000_000 // 1_000_000:03d}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('file')
    ap.add_argument('--max', type=int, default=None)
    ap.add_argument('--watch', default='', help='comma separated symbols to print at the end')
    ap.add_argument('--topn', type=int, default=5)
    args = ap.parse_args()

    mkt = Market()
    counts = Counter()
    last_ts = 0
    t0 = time.perf_counter()

    for t, msg in itch.messages(args.file, args.max):
        counts[t] += 1
        m = itch.decode(msg)
        if m is not None:
            last_ts = m.ts
            mkt.apply(m)

    dt = time.perf_counter() - t0
    total = sum(counts.values())
    print(f'{total:,} messages in {dt:.1f}s ({total / dt / 1e6:.2f} M msg/s python)')
    print(f'feed clock at end: {fmt_ts(last_ts)}')
    print(f'live orders: {len(mkt.orders):,}   books: {len(mkt.books):,}   '
          f'integrity violations: {len(mkt.violations)}')
    for v in mkt.violations[:10]:
        print('  VIOLATION:', v)
    print('\ncounts by type:')
    for t, c in counts.most_common():
        print(f'  {t.decode()}: {c:,}')

    watch = [s.strip().encode() for s in args.watch.split(',') if s.strip()]
    by_sym = {v: k for k, v in mkt.symbols.items()}
    for sym in watch:
        locate = by_sym.get(sym)
        if locate is None or locate not in mkt.books:
            print(f'\n{sym.decode()}: no book')
            continue
        bids, asks = mkt.books[locate].top(args.topn)
        print(f'\n{sym.decode()} top {args.topn}:')
        print('  bid                 ask')
        for i in range(max(len(bids), len(asks))):
            b = f'{bids[i][1]:>7,} @ {fmt_price(bids[i][0])}' if i < len(bids) else ' ' * 18
            a = f'{asks[i][1]:>7,} @ {fmt_price(asks[i][0])}' if i < len(asks) else ''
            print(f'  {b}   {a}')


if __name__ == '__main__':
    main()
