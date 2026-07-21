"""Generate the expected bbo strobe stream for a replay, same 16 byte
records the verilator harness writes, plus a counters json.

    python tb/expected_bbo.py data/01302020.NASDAQ_ITCH50.gz out/expected.bin \
        --locate 13 --base 3000000

Peeks at the type and locate bytes before bothering to decode, the full
day is a third of a billion messages and most of them are not ours.
"""

import argparse
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from model import itch                  # noqa: E402
from windowed_ref import WindowedBook   # noqa: E402

BOOK_TYPES = frozenset(b'AFECXDU')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('itch_file')
    ap.add_argument('out_bin')
    ap.add_argument('--locate', type=int, required=True)
    ap.add_argument('--base', type=int, required=True)
    ap.add_argument('--max', type=int, default=0)
    ap.add_argument('--sets-log2', type=int, default=11)
    args = ap.parse_args()

    ref = WindowedBook(args.locate, args.base, sets_log2=args.sets_log2)
    loc_bytes = struct.pack('>H', args.locate)
    n = 0
    strobes = 0

    with open(args.out_bin, 'wb') as out:
        for t, msg in itch.messages(args.itch_file,
                                    args.max if args.max else None):
            n += 1
            if n % 20000000 == 0:
                print(f'  {n // 1000000}M msgs', file=sys.stderr)
            if t[0] not in BOOK_TYPES:
                continue
            if msg[1:3] != loc_bytes:
                ref.filtered += 1
                continue
            if ref.apply(itch.decode(msg)):
                b, bs, a, as_ = ref.bbo()
                flags = (1 if b is not None else 0) | (2 if a is not None else 0)
                out.write(struct.pack('<HHHHII', flags,
                                      b or 0, a or 0, 0,
                                      bs if b is not None else 0,
                                      as_ if a is not None else 0))
                strobes += 1

    counters = {'msgs': n, 'strobes': strobes, 'filtered': ref.filtered,
                'outlier': ref.outlier, 'miss': ref.miss,
                'overflow': ref.overflow}
    Path(args.out_bin).with_suffix('.json').write_text(json.dumps(counters))
    print(json.dumps(counters))


if __name__ == '__main__':
    main()
