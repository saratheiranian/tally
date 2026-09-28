import subprocess
import sys
from collections import Counter

import pytest

from app.hashring import ConsistentHashRing

KEYS = [f"tenant-{i}" for i in range(20_000)]
NODES = [f"q-{i}" for i in range(4)]


def assignments(ring):
    return {k: ring.get(k) for k in KEYS}


def test_deterministic_across_processes():
    """Must not depend on Python's per-process salted hash(): every API node has to agree."""
    code = "from app.hashring import ConsistentHashRing as R; print(R(['a','b','c']).get('tenant-42'))"
    outs = {
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONHASHSEED": seed}
        ).stdout.strip()
        for seed in ("1", "2", "3")
    }
    assert len(outs) == 1 and outs != {""}


def test_load_is_balanced():
    counts = Counter(assignments(ConsistentHashRing(NODES)).values())
    mean = len(KEYS) / len(NODES)
    assert all(0.8 * mean <= c <= 1.2 * mean for c in counts.values()), counts


def test_adding_a_node_moves_only_about_one_nth():
    ring = ConsistentHashRing(NODES)
    before = assignments(ring)
    ring.add("q-4")
    after = assignments(ring)
    moved = [k for k in KEYS if before[k] != after[k]]
    assert 0.12 < len(moved) / len(KEYS) < 0.28  # ideal is 1/5 = 0.20; mod-N would move ~80%
    assert all(after[k] == "q-4" for k in moved)  # keys only ever move TO the new node


def test_removing_a_node_moves_only_its_keys():
    ring = ConsistentHashRing(NODES)
    before = assignments(ring)
    ring.remove("q-2")
    after = assignments(ring)
    for k in KEYS:
        if before[k] != "q-2":
            assert after[k] == before[k]
        else:
            assert after[k] != "q-2"


def test_empty_ring_raises():
    with pytest.raises(LookupError):
        ConsistentHashRing().get("x")
