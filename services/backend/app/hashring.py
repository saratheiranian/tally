"""Consistent hashing with virtual nodes.

Tally splits its event queue into shards, and each tenant must always land on the
same shard so a single worker owns that tenant's in-memory state (Phase 3's
sketches). A plain `hash(tenant) % N` would remap almost every tenant whenever N
changes. A hash ring remaps only ~1/N of them, and virtual nodes smooth out the
load so no shard owns a disproportionate arc of the ring.

Note: Python's built-in hash() is salted per process, so two API nodes would
disagree. We use MD5 purely as a fast, stable, well-distributed hash, not for security.
"""

import bisect
import hashlib
from collections.abc import Iterable


def _hash(key: str) -> int:
    return int.from_bytes(hashlib.md5(key.encode()).digest()[:8], "big")


class ConsistentHashRing:
    def __init__(self, nodes: Iterable[str] = (), vnodes: int = 160) -> None:
        self._vnodes = vnodes
        self._ring: list[int] = []  # sorted vnode positions
        self._owners: dict[int, str] = {}  # position -> physical node
        self._nodes: set[str] = set()
        for node in nodes:
            self.add(node)

    @property
    def nodes(self) -> frozenset[str]:
        return frozenset(self._nodes)

    def add(self, node: str) -> None:
        if node in self._nodes:
            return
        self._nodes.add(node)
        for i in range(self._vnodes):
            pos = _hash(f"{node}#{i}")
            if pos in self._owners:  # astronomically rare 64-bit collision; skip
                continue
            bisect.insort(self._ring, pos)
            self._owners[pos] = node

    def remove(self, node: str) -> None:
        if node not in self._nodes:
            return
        self._nodes.discard(node)
        doomed = {pos for pos, owner in self._owners.items() if owner == node}
        self._ring = [p for p in self._ring if p not in doomed]
        for pos in doomed:
            del self._owners[pos]

    def get(self, key: str) -> str:
        """Owner of `key`: the first vnode clockwise from the key's position."""
        if not self._ring:
            raise LookupError("hash ring is empty")
        idx = bisect.bisect_right(self._ring, _hash(key)) % len(self._ring)
        return self._owners[self._ring[idx]]
