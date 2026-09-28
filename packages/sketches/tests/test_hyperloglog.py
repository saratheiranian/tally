import pytest

from tally_sketches import HyperLogLog


def rel_err(est, true):
    return abs(est - true) / true


def test_empty_is_zero():
    assert HyperLogLog().estimate() == 0


@pytest.mark.parametrize("n", [100, 1_000, 10_000, 200_000])
def test_estimate_is_within_four_sigma(n):
    h = HyperLogLog(p=14)
    h.update(f"user-{i}" for i in range(n))
    assert rel_err(h.estimate(), n) < 4 * h.relative_error


def test_duplicates_do_not_change_the_estimate():
    h = HyperLogLog()
    h.update(f"u{i}" for i in range(5_000))
    before = h.to_bytes()
    h.update(f"u{i}" for i in range(5_000))
    assert h.to_bytes() == before


def test_merge_is_exactly_the_sketch_of_the_union():
    a, b, union = HyperLogLog(), HyperLogLog(), HyperLogLog()
    a.update(f"u{i}" for i in range(0, 60_000))
    b.update(f"u{i}" for i in range(40_000, 100_000))  # 20k overlap
    union.update(f"u{i}" for i in range(0, 100_000))
    a.merge(b)
    assert a == union
    assert rel_err(a.estimate(), 100_000) < 4 * a.relative_error


def test_merge_is_commutative_and_idempotent():
    a, b = HyperLogLog(p=10), HyperLogLog(p=10)
    a.update(map(str, range(1_000)))
    b.update(map(str, range(500, 3_000)))
    ab = HyperLogLog.from_bytes(a.to_bytes())
    ab.merge(b)
    ba = HyperLogLog.from_bytes(b.to_bytes())
    ba.merge(a)
    assert ab == ba
    again = HyperLogLog.from_bytes(ab.to_bytes())
    again.merge(b)
    assert again == ab


def test_round_trip_serialization():
    h = HyperLogLog(p=12)
    h.update(map(str, range(12_345)))
    restored = HyperLogLog.from_bytes(h.to_bytes())
    assert restored == h and restored.estimate() == h.estimate()
    assert len(h.to_bytes()) == 5 + 4096


def test_rejects_bad_input():
    with pytest.raises(ValueError):
        HyperLogLog(p=3)
    with pytest.raises(ValueError):
        HyperLogLog(p=10).merge(HyperLogLog(p=11))
    with pytest.raises(ValueError):
        HyperLogLog.from_bytes(b"XXX\x01\x0e" + bytes(16384))
