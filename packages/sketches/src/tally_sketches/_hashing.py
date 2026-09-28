"""Stable 64-bit hashing.

Sketches built on different machines or processes must be mergeable, so the hash
has to be identical everywhere. Python's built-in hash() is salted per process,
so we use BLAKE2b (stdlib, fast, well-mixed) truncated to 64 bits.
"""

from hashlib import blake2b


def _encode(item: str | bytes) -> bytes:
    return item.encode() if isinstance(item, str) else item


def hash64(item: str | bytes) -> int:
    return int.from_bytes(blake2b(_encode(item), digest_size=8).digest(), "little")


def hash128_pair(item: str | bytes) -> tuple[int, int]:
    """Two independent 64-bit hashes from a single digest (for double hashing)."""
    d = blake2b(_encode(item), digest_size=16).digest()
    return int.from_bytes(d[:8], "little"), int.from_bytes(d[8:], "little") | 1
