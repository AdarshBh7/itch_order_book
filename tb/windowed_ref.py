"""Mirror of the RTL book's semantics, used to predict every BBO strobe.

Not the same thing as model/book.py: that one is the full-depth golden book.
This one applies the same windowing rules the hardware does (price window,
penny ticks, locate filter, drop rules) so the comparison is exact. Keeping
it separate keeps the golden model honest.
"""

TICKS = 4096
TICK = 100
SETS_LOG2 = 11
SETS = 1 << SETS_LOG2
WAYS = 4


class WindowedBook:
    def __init__(self, locate, base):
        self.locate = locate
        self.base = base
        self.orders = {}          # ref -> [side, tick, shares, set, way]
        self.table = [[None] * WAYS for _ in range(SETS)]
        self.bid = {}             # tick -> shares
        self.ask = {}
        self.filtered = 0
        self.outlier = 0
        self.miss = 0
        self.overflow = 0

    @staticmethod
    def _hash(ref):
        # same xor fold the rtl uses
        mask = SETS - 1
        return (ref ^ (ref >> SETS_LOG2) ^ (ref >> 2 * SETS_LOG2)) & mask

    def _tick(self, price):
        d = price - self.base
        if d < 0 or d >= TICKS * TICK or d % TICK:
            return None
        return d // TICK

    def _levels(self, side):
        return self.bid if side else self.ask

    def bbo(self):
        b = max(self.bid) if self.bid else None
        a = min(self.ask) if self.ask else None
        return (b, self.bid[b] if b is not None else 0,
                a, self.ask[a] if a is not None else 0)

    def _insert(self, ref, side, tick, shares):
        """False when the hash set is full, mirroring the rtl drop."""
        s = self._hash(ref)
        ways = self.table[s]
        for w in range(WAYS):
            if ways[w] is None:
                ways[w] = ref
                lv = self._levels(side)
                lv[tick] = lv.get(tick, 0) + shares
                self.orders[ref] = [side, tick, shares, s, w]
                return True
        self.overflow += 1
        return False

    def _take(self, ref, shares):
        o = self.orders[ref]
        taken = min(shares, o[2])
        lv = self._levels(o[0])
        lv[o[1]] -= taken
        if lv[o[1]] == 0:
            del lv[o[1]]
        o[2] -= taken
        if o[2] == 0:
            self.table[o[3]][o[4]] = None
            del self.orders[ref]

    def add(self, locate, ref, side, shares, price):
        """Returns True when the hardware would strobe a BBO update."""
        if locate != self.locate:
            self.filtered += 1
            return False
        t = self._tick(price)
        if t is None:
            self.outlier += 1
            return False
        if ref in self.orders:
            self.miss += 1
            return False
        return self._insert(ref, side, t, shares)

    def take(self, locate, ref, shares):
        # execute and cancel look identical to the book
        if locate != self.locate:
            self.filtered += 1
            return False
        if ref not in self.orders:
            self.miss += 1
            return False
        self._take(ref, shares)
        return True

    def delete(self, locate, ref):
        if locate != self.locate:
            self.filtered += 1
            return False
        if ref not in self.orders:
            self.miss += 1
            return False
        self._take(ref, self.orders[ref][2])
        return True

    def replace(self, locate, old_ref, new_ref, shares, price):
        if locate != self.locate:
            self.filtered += 1
            return False
        if old_ref not in self.orders:
            self.miss += 1
            return False
        side = self.orders[old_ref][0]
        self._take(old_ref, self.orders[old_ref][2])
        # the delete half moved the book, so the hardware strobes even if
        # the add half gets dropped below
        t = self._tick(price)
        if t is None:
            self.outlier += 1
            return True
        if new_ref in self.orders:
            self.miss += 1
            return True
        self._insert(new_ref, side, t, shares)   # overflow still strobes
        return True

    def apply(self, m):
        """Feed a decoded golden-model message. Returns strobe expected."""
        kind = type(m).__name__
        if kind == 'AddOrder':
            return self.add(m.locate, m.ref, m.side == b'B', m.shares, m.price)
        if kind in ('Execute', 'ExecutePx'):
            return self.take(m.locate, m.ref, m.shares)
        if kind == 'Cancel':
            return self.take(m.locate, m.ref, m.shares)
        if kind == 'Delete':
            return self.delete(m.locate, m.ref)
        if kind == 'Replace':
            return self.replace(m.locate, m.old_ref, m.new_ref, m.shares, m.price)
        return False
