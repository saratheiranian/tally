"""Top-K heavy hitters: a Count-Min Sketch for counting plus a bounded candidate
set kept in a min-heap.

Every item goes into the sketch, but only the current k best candidates are kept
by name. When a non-candidate's estimate beats the weakest candidate (the heap
root, found in O(1)), it takes that slot in O(log k). Memory is
O(k + sketch), independent of how many distinct items the stream contains.

The heap uses lazy invalidation: updating a candidate pushes a fresh entry
rather than searching the heap, and stale entries are skipped when they surface.
The heap is rebuilt when stale entries exceed a small multiple of k, which keeps
memory bounded.

Accuracy: on skewed (Zipf-like) data, which describes page views, event names
and most real traffic, the true heavy hitters are found reliably. Counts carry
Count-Min's one-sided error: never under, at most ε·N over.
"""

from __future__ import annotations

import heapq
import json
import struct

from .countmin import CountMinSketch

_MAGIC = b"TOP"
_VERSION = 1
_HEADER = struct.Struct("<3sBII")  # magic, version, k, len(sketch bytes)


class TopK:
    __slots__ = ("k", "sketch", "_candidates", "_heap")

    def __init__(self, k: int = 10, epsilon: float = 0.001, delta: float = 0.01) -> None:
        if k < 1:
            raise ValueError("k must be positive")
        self.k = k
        self.sketch = CountMinSketch.from_error(epsilon, delta, conservative=True)
        self._candidates: dict[str, int] = {}
        self._heap: list[tuple[int, str]] = []

    @property
    def total(self) -> int:
        return self.sketch.total

    def _min(self) -> tuple[int, str]:
        heap, cand = self._heap, self._candidates
        while heap and cand.get(heap[0][1]) != heap[0][0]:
            heapq.heappop(heap)  # stale entry
        return heap[0]

    def _push(self, item: str, count: int) -> None:
        self._candidates[item] = count
        heapq.heappush(self._heap, (count, item))
        if len(self._heap) > 4 * self.k + 16:
            self._heap = [(c, i) for i, c in self._candidates.items()]
            heapq.heapify(self._heap)

    def add(self, item: str, count: int = 1) -> None:
        est = self.sketch.add(item, count)
        if item in self._candidates or len(self._candidates) < self.k:
            self._push(item, est)
            return
        weakest_count, weakest = self._min()
        if est > weakest_count:
            del self._candidates[weakest]
            heapq.heappop(self._heap)
            self._push(item, est)

    def update(self, items) -> None:
        for item in items:
            self.add(item)

    def top(self, n: int | None = None) -> list[tuple[str, int]]:
        """Best candidates, highest first, with counts re-read from the sketch."""
        ranked = sorted(
            ((item, self.sketch.estimate(item)) for item in self._candidates),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return ranked[: n or self.k]

    def merge(self, other: TopK) -> None:
        """Merge sketches, then re-rank the union of both candidate sets."""
        self.sketch.merge(other.sketch)
        pool = set(self._candidates) | set(other._candidates)
        best = heapq.nlargest(self.k, ((self.sketch.estimate(i), i) for i in pool))
        self._candidates = {i: c for c, i in best}
        self._heap = [(c, i) for i, c in self._candidates.items()]
        heapq.heapify(self._heap)

    # -- serialization ------------------------------------------------------
    def to_bytes(self) -> bytes:
        sketch = self.sketch.to_bytes()
        names = json.dumps(sorted(self._candidates), ensure_ascii=False).encode()
        return _HEADER.pack(_MAGIC, _VERSION, self.k, len(sketch)) + sketch + names

    @classmethod
    def from_bytes(cls, data: bytes) -> TopK:
        magic, version, k, sketch_len = _HEADER.unpack_from(data)
        if magic != _MAGIC or version != _VERSION:
            raise ValueError("not a tally-sketches TopK (or unsupported version)")
        start = _HEADER.size
        tk = cls.__new__(cls)
        tk.k = k
        tk.sketch = CountMinSketch.from_bytes(data[start : start + sketch_len])
        names = json.loads(data[start + sketch_len :].decode())
        tk._candidates = {n: tk.sketch.estimate(n) for n in names}
        tk._heap = [(c, i) for i, c in tk._candidates.items()]
        heapq.heapify(tk._heap)
        return tk

    def __repr__(self) -> str:
        return f"TopK(k={self.k}, total={self.total}, top={self.top(3)})"
