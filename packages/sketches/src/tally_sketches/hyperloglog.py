"""HyperLogLog cardinality estimator (Flajolet et al., 2007; with the
small-range correction from Heule et al., 2013).

Idea: hash every item to 64 uniform bits. Use the first p bits to pick one of
m = 2^p registers. In the remaining bits, the position of the first 1-bit is
geometrically distributed, so seeing a long run of leading zeros is evidence
of many distinct items. Each register keeps the longest run it has seen. The
harmonic mean across registers turns that into an estimate.

Guarantees and properties
-------------------------
* Standard error ≈ 1.04 / sqrt(m). At p=14 that's 0.81% using 16 KiB.
* Memory is fixed: it doesn't grow with the number of items.
* Adding the same item twice changes nothing (idempotent).
* merge() is register-wise max, so merging the sketches of A and B gives
  *exactly* the sketch of A ∪ B. That's what makes "unique users this week"
  from seven daily sketches correct, with no double counting.
"""

from __future__ import annotations

import math
import struct

from ._hashing import hash64

_MAGIC = b"HLL"
_VERSION = 1
_HEADER = struct.Struct("<3sBB")  # magic, version, precision


class HyperLogLog:
    __slots__ = ("p", "m", "_registers", "_suffix_bits", "_suffix_mask")

    def __init__(self, p: int = 14) -> None:
        if not 4 <= p <= 18:
            raise ValueError("precision p must be between 4 and 18")
        self.p = p
        self.m = 1 << p
        self._registers = bytearray(self.m)
        self._suffix_bits = 64 - p
        self._suffix_mask = (1 << self._suffix_bits) - 1

    # -- updates ------------------------------------------------------------
    def add(self, item: str | bytes) -> None:
        h = hash64(item)
        idx = h >> self._suffix_bits
        suffix = h & self._suffix_mask
        # rank = 1-based position of the first 1-bit in the suffix
        rank = self._suffix_bits - suffix.bit_length() + 1
        if rank > self._registers[idx]:
            self._registers[idx] = rank

    def update(self, items) -> None:
        for item in items:
            self.add(item)

    def merge(self, other: HyperLogLog) -> None:
        """In-place union. The result equals the sketch of the combined streams."""
        if other.p != self.p:
            raise ValueError(f"cannot merge p={other.p} into p={self.p}")
        self._registers = bytearray(map(max, self._registers, other._registers))

    # -- queries ------------------------------------------------------------
    def estimate(self) -> float:
        m = self.m
        alpha = {16: 0.673, 32: 0.697, 64: 0.709}.get(m, 0.7213 / (1 + 1.079 / m))
        z = math.fsum(2.0**-r for r in self._registers)
        raw = alpha * m * m / z
        zeros = self._registers.count(0)
        # Small range: many empty registers means linear counting is more accurate.
        if raw <= 2.5 * m and zeros:
            return m * math.log(m / zeros)
        # No large-range correction is needed: 64-bit hashes don't saturate.
        return raw

    def __len__(self) -> int:
        return round(self.estimate())

    @property
    def relative_error(self) -> float:
        """Theoretical standard error (one sigma)."""
        return 1.04 / math.sqrt(self.m)

    @property
    def nbytes(self) -> int:
        return len(self._registers)

    # -- serialization ------------------------------------------------------
    def to_bytes(self) -> bytes:
        return _HEADER.pack(_MAGIC, _VERSION, self.p) + bytes(self._registers)

    @classmethod
    def from_bytes(cls, data: bytes) -> HyperLogLog:
        magic, version, p = _HEADER.unpack_from(data)
        if magic != _MAGIC or version != _VERSION:
            raise ValueError("not a tally-sketches HyperLogLog (or unsupported version)")
        hll = cls(p)
        body = data[_HEADER.size :]
        if len(body) != hll.m:
            raise ValueError("corrupt HyperLogLog payload")
        hll._registers = bytearray(body)
        return hll

    def __eq__(self, other: object) -> bool:
        return isinstance(other, HyperLogLog) and self.p == other.p and self._registers == other._registers

    def __repr__(self) -> str:
        return f"HyperLogLog(p={self.p}, estimate≈{self.estimate():.0f})"
