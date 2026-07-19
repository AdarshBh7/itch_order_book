"""Reference limit order book, driven by decoded ITCH messages.

This is the golden model the RTL gets checked against, so it favors
being obviously correct over being fast. Prices stay as raw ints
(4 implied decimals), shares as ints, one book per stock locate.
"""

from sortedcontainers import SortedDict


class Order:
    __slots__ = ('locate', 'side', 'price', 'shares')

    def __init__(self, locate, side, price, shares):
        self.locate = locate
        self.side = side
        self.price = price
        self.shares = shares


class Book:
    def __init__(self):
        self.bids = SortedDict()   # price -> total displayed shares
        self.asks = SortedDict()

    def _levels(self, side):
        return self.bids if side == b'B' else self.asks

    def add(self, side, price, shares):
        lv = self._levels(side)
        lv[price] = lv.get(price, 0) + shares

    def remove(self, side, price, shares):
        lv = self._levels(side)
        left = lv[price] - shares
        if left < 0:
            raise ValueError('level went negative')
        if left == 0:
            del lv[price]
        else:
            lv[price] = left

    def bbo(self):
        bid = self.bids.peekitem(-1) if self.bids else None
        ask = self.asks.peekitem(0) if self.asks else None
        return bid, ask

    def top(self, n):
        bids = [self.bids.peekitem(-1 - i) for i in range(min(n, len(self.bids)))]
        asks = [self.asks.peekitem(i) for i in range(min(n, len(self.asks)))]
        return bids, asks


class Market:
    """All books plus the order-ref table. Apply decoded messages in order."""

    def __init__(self):
        self.books = {}
        self.orders = {}
        self.symbols = {}   # locate -> stock symbol, from R messages
        self.violations = []

    def book(self, locate):
        b = self.books.get(locate)
        if b is None:
            b = self.books[locate] = Book()
        return b

    def _bust(self, what):
        # real feed data should never trip these; if one fires the decoder
        # or the book logic is wrong, which is exactly what we want to catch
        self.violations.append(what)

    def apply(self, m):
        kind = type(m).__name__
        if kind == 'AddOrder':
            if m.ref in self.orders:
                return self._bust(f'dup ref {m.ref}')
            self.orders[m.ref] = Order(m.locate, m.side, m.price, m.shares)
            self.book(m.locate).add(m.side, m.price, m.shares)
        elif kind == 'Execute' or kind == 'ExecutePx':
            o = self.orders.get(m.ref)
            if o is None:
                return self._bust(f'exec unknown ref {m.ref}')
            self._take(o, m.ref, m.shares, 'exec')
        elif kind == 'Cancel':
            o = self.orders.get(m.ref)
            if o is None:
                return self._bust(f'cancel unknown ref {m.ref}')
            self._take(o, m.ref, m.shares, 'cancel')
        elif kind == 'Delete':
            o = self.orders.pop(m.ref, None)
            if o is None:
                return self._bust(f'delete unknown ref {m.ref}')
            self.book(o.locate).remove(o.side, o.price, o.shares)
        elif kind == 'Replace':
            o = self.orders.pop(m.old_ref, None)
            if o is None:
                return self._bust(f'replace unknown ref {m.old_ref}')
            self.book(o.locate).remove(o.side, o.price, o.shares)
            if m.new_ref in self.orders:
                return self._bust(f'replace dup new ref {m.new_ref}')
            self.orders[m.new_ref] = Order(o.locate, o.side, m.price, m.shares)
            self.book(o.locate).add(o.side, m.price, m.shares)
        elif kind == 'StockDir':
            self.symbols[m.locate] = m.stock

    def _take(self, o, ref, shares, why):
        if shares > o.shares:
            return self._bust(f'{why} {shares} > resting {o.shares} on ref {ref}')
        self.book(o.locate).remove(o.side, o.price, shares)
        o.shares -= shares
        if o.shares == 0:
            del self.orders[ref]
