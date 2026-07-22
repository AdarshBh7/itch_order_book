"""Render the order book ladder animating on real NASDAQ data into a GIF.

Drives the reference book (model/book.py), which the RTL is verified
byte-exact against, so what you see updating is exactly the top of book the
hardware emits. Frames drawn with Pillow, no heavy deps.

    python scripts/make_demo.py data/itch_prefix.gz --symbol AAPL --out docs/demo.gif
"""

import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from model import itch                     # noqa: E402
from model.book import Market              # noqa: E402

W, H = 960, 620
BG = (13, 17, 23)
GRID = (28, 33, 40)
BID = (63, 185, 106)
ASK = (229, 83, 75)
DIM = (120, 130, 140)
TXT = (222, 228, 234)
ACCENT = (88, 166, 255)
LEVELS = 8


def load_font(size, bold=False):
    for name in (('consolab.ttf' if bold else 'consola.ttf'),
                 'DejaVuSansMono.ttf'):
        for root in ('C:/Windows/Fonts', '/usr/share/fonts/truetype/dejavu'):
            p = os.path.join(root, name)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default()


F_BIG = load_font(30, bold=True)
F_MED = load_font(19)
F_SM = load_font(15)
F_TINY = load_font(13)


def fmt_ts(ns):
    s = ns // 1_000_000_000
    return f'{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}.{ns % 1_000_000_000 // 1_000_000:03d}'


def frame(symbol, ts, msg_n, bids, asks, last_type):
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)

    d.text((32, 24), symbol, font=F_BIG, fill=TXT)
    d.text((32, 64), 'NASDAQ TotalView-ITCH 5.0   2020-01-30', font=F_SM, fill=DIM)
    d.text((W - 250, 30), fmt_ts(ts), font=F_MED, fill=ACCENT)
    d.text((W - 250, 58), f'msg {msg_n:>12,}', font=F_SM, fill=DIM)

    # spread band in the middle, asks climbing up, bids dropping down.
    # price sits in a centered column, size bars grow outward from it
    mid_y = 322
    row_h = 26
    band = 30
    max_sz = max([s for _, s in bids[:LEVELS]] + [s for _, s in asks[:LEVELS]] + [1])
    bar_max = 250
    cx = W // 2
    price_l, price_r = cx - 62, cx + 62   # inner edges of the price column

    def draw_side(levels, up):
        for i, (price, size) in enumerate(levels[:LEVELS]):
            base = mid_y - band if up else mid_y + band
            y = base + (-(i + 1) if up else i) * row_h
            best = (i == 0)
            col = ASK if up else BID
            bar = col if best else (col[0] // 3, col[1] // 3, col[2] // 3)
            w = int(bar_max * size / max_sz)
            d.text((cx - d.textlength(f'{price/10000:.4f}', font=F_MED) / 2, y + 3),
                   f'{price/10000:.4f}', font=F_MED, fill=TXT if best else DIM)
            if up:
                d.rectangle([price_r, y + 6, price_r + w, y + row_h - 3], fill=bar)
                d.text((price_r + w + 6, y + 4), f'{size:,}', font=F_SM, fill=col)
            else:
                d.rectangle([price_l - w, y + 6, price_l, y + row_h - 3], fill=bar)
                tw = d.textlength(f'{size:,}', font=F_SM)
                d.text((price_l - w - 8 - tw, y + 4), f'{size:,}', font=F_SM, fill=col)

    draw_side(asks, up=True)
    draw_side(bids, up=False)

    # spread readout in the band
    if bids and asks:
        spread = asks[0][0] - bids[0][0]
        tag = f'spread {spread/10000:.4f}      {bids[0][0]/10000:.4f}  x  {asks[0][0]/10000:.4f}'
        d.text((cx - d.textlength(tag, font=F_SM) / 2, mid_y - 8), tag, font=F_SM, fill=ACCENT)

    d.text((32, H - 40),
           'reference book, real exchange messages  |  FPGA RTL verified byte-exact against this top of book',
           font=F_TINY, fill=DIM)
    d.text((W - 150, H - 40), f'last: {last_type}', font=F_TINY, fill=DIM)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('file')
    ap.add_argument('--symbol', default='AAPL')
    ap.add_argument('--out', default='docs/demo.gif')
    ap.add_argument('--warmup', type=int, default=900_000)
    ap.add_argument('--frames', type=int, default=120)
    ap.add_argument('--per-frame', type=int, default=1500)
    args = ap.parse_args()

    want = args.symbol.encode()
    mkt = Market()
    locate = None
    gen = itch.messages(args.file)

    # warm the book up to where the symbol is trading
    n = 0
    for t, msg in gen:
        n += 1
        m = itch.decode(msg)
        if m is not None:
            if type(m).__name__ == 'StockDir' and m.stock == want:
                locate = m.locate
            mkt.apply(m)
        if n >= args.warmup and locate is not None and locate in mkt.books \
                and len(mkt.books[locate].bids) > LEVELS:
            break

    frames = []
    last_type = '-'
    last_ts = 0
    for _ in range(args.frames):
        for _ in range(args.per_frame):
            try:
                t, msg = next(gen)
            except StopIteration:
                break
            n += 1
            m = itch.decode(msg)
            if m is not None:
                last_ts = getattr(m, 'ts', last_ts)
                if getattr(m, 'locate', None) == locate:
                    last_type = type(m).__name__
                mkt.apply(m)
        bids, asks = mkt.books[locate].top(LEVELS)
        frames.append(frame(args.symbol, last_ts, n, bids, asks, last_type))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    frames[0].save(args.out, save_all=True, append_images=frames[1:],
                   duration=90, loop=0, optimize=True)
    print(f'{args.out}: {len(frames)} frames, {os.path.getsize(args.out)//1024} KB')


if __name__ == '__main__':
    main()
