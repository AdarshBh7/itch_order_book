"""Grab NASDAQ ITCH 5.0 sample data (real market data, published free by Nasdaq).

The full day is ~5.6 GB compressed. For development you usually just want the
first chunk, and the server supports range requests, so:

    python scripts/fetch_data.py --slice-mb 64      # first 64 MB -> data/itch_prefix.gz
    python scripts/fetch_data.py --full             # whole day   -> data/01302020.NASDAQ_ITCH50.gz

A sliced .gz decompresses fine up to the cut point, the decoder just stops at
the truncated tail.
"""

import argparse
import os
import urllib.request

URL = 'https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/01302020.NASDAQ_ITCH50.gz'
DATA = os.path.join(os.path.dirname(__file__), '..', 'data')


def fetch(dest, headers=None):
    req = urllib.request.Request(URL, headers=headers or {})
    os.makedirs(DATA, exist_ok=True)
    with urllib.request.urlopen(req) as r, open(dest, 'wb') as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    print(f'{dest}: {os.path.getsize(dest):,} bytes')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--slice-mb', type=int, default=64)
    ap.add_argument('--full', action='store_true')
    args = ap.parse_args()
    if args.full:
        fetch(os.path.join(DATA, '01302020.NASDAQ_ITCH50.gz'))
    else:
        end = args.slice_mb * (1 << 20) - 1
        fetch(os.path.join(DATA, 'itch_prefix.gz'), {'Range': f'bytes=0-{end}'})


if __name__ == '__main__':
    main()
