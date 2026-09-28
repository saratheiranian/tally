"""Count-Min Sketch (Cormode & Muthukrishnan, 2005).

A depth × width grid of counters. Each item hashes to one counter per row.
Adding increments those counters; querying returns the minimum of them.
Collisions can only add, never subtract, so the answer is never too low, and
taking the min across rows picks the least-polluted counter.

Guarantee: with total count N, for every item
    true ≤ estimate ≤ true + ε·N      with probability ≥ 1 − δ
using width = ⌈e/ε⌉ and depth = ⌈ln(1/δ)⌉ counters.

Conservative update (Estan & Varghese, 2002) is an optional refinement: only
raise counters that are below the new minimum. The same one-sided guarantee
holds, with much less over-counting on skewed data. The trade-off: it can't
support decrements.

Row indices use double hashing (Kirsch & Mitzenmacher, 2006):
h_i = h1 + i·h2 needs one digest per item instead of `depth` of them, with no
loss in the asymptotic guarantee.
"""

from __future__ import annotations

import math
import struct
import sys
from array import array

from ._hashing import hash128_pair

_MAGIC = b"CMS"
_VERSION = 1
_HEADER = struct.Struct("<3sBIIBQ")  # magic, version, width, depth, conservative, total


class CountMinSketch:
    __slots__ = ("width", "depth", "conservative", "total", "_table")

    def __init__(self, width: int, depth: int, conservative: bool = False) -> None:
        if width < 1 or depth < 1:
            raise ValueError("width and depth must be positive")
        self.width = width
        self.depth = depth
        self.conservative = conservative
        self.total = 0
        self._table = array("Q", bytes(8 * width * depth))  # row-major, zeroed

    @classmethod
    def from_error(cls, epsilon: float = 0.001, delta: float = 0.01, conservative: bool = False):
        """Size the sketch from the error you can tolerate."""
        if not (0 < epsilon < 1 and 0 < delta < 1):
            raise ValueError("epsilon and delta must be in (0, 1)")
        return cls(math.ceil(math.e / epsilon), math.ceil(math.log(1 / delta)), conservative)

    @property
    def epsilon(self) -> float:
        return math.e / self.width

    @property
    def delta(self) -> float:
        return math.exp(-self.depth)

    @property
    def nbytes(self) -> int:
        return self._table.itemsize * len(self._table)

    def _cells(self, item: str | bytes) -> list[int]:
        h1, h2 = hash128_pair(item)
        w = self.width
        return [row * w + (h1 + row * h2) % w for row in range(self.depth)]

    def add(self, item: str | bytes, count: int = 1) -> int:
        """Add `count` occurrences and return the item's new estimate."""
        if count < 0:
            raise ValueError("Count-Min Sketch does not support negative counts")
        cells = self._cells(item)
        t = self._table
        self.total += count
        if self.conservative:
            target = min(t[c] for c in cells) + count
            for c in cells:
                if t[c] < target:
                    t[c] = target
            return target
        for c in cells:
            t[c] += count
        return min(t[c] for c in cells)

    def estimate(self, item: str | bytes) -> int:
        t = self._table
        return min(t[c] for c in self._cells(item))

    def __getitem__(self, item: str | bytes) -> int:
        return self.estimate(item)

    def merge(self, other: CountMinSketch) -> None:
        """In-place sum. Exact (equal to sketching both streams) without
        conservative update. With it, the result is still a valid one-sided
        upper bound, just slightly looser."""
        if (other.width, other.depth) != (self.width, self.depth):
            raise ValueError("cannot merge sketches with different dimensions")
        t, o = self._table, other._table
        for i in range(len(t)):
            t[i] += o[i]
        self.total += other.total

    # -- serialization (little-endian on every platform) -------------------
    def to_bytes(self) -> bytes:
        header = _HEADER.pack(_MAGIC, _VERSION, self.width, self.depth, self.conservative, self.total)
        table = array("Q", self._table)
        if sys.byteorder == "big":
            table.byteswap()
        return header + table.tobytes()

    @classmethod
    def from_bytes(cls, data: bytes) -> CountMinSketch:
        magic, version, width, depth, conservative, total = _HEADER.unpack_from(data)
        if magic != _MAGIC or version != _VERSION:
            raise ValueError("not a tally-sketches CountMinSketch (or unsupported version)")
        cms = cls(width, depth, bool(conservative))
        body = data[_HEADER.size :]
        if len(body) != 8 * width * depth:
            raise ValueError("corrupt CountMinSketch payload")
        table = array("Q")
        table.frombytes(body)
        if sys.byteorder == "big":
            table.byteswap()
        cms._table, cms.total = table, total
        return cms

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, CountMinSketch)
            and (self.width, self.depth, self.conservative, self.total)
            == (other.width, other.depth, other.conservative, other.total)
            and self._table == other._table
        )

    def __repr__(self) -> str:
        return f"CountMinSketch(width={self.width}, depth={self.depth}, total={self.total})"
