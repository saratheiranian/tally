import random
from collections import Counter

import pytest

from tally_sketches import CountMinSketch


def zipf_stream(n, distinct, s=1.1, seed=7):
    rng = random.Random(seed)
    weights = [1 / (r**s) for r in range(1, distinct + 1)]
    return [f"item-{i}" for i in rng.choices(range(distinct), weights, k=n)]


@pytest.mark.parametrize("conservative", [False, True])
def test_never_underestimates_and_respects_error_bound(conservative):
    stream = zipf_stream(50_000, 5_000)
    truth = Counter(stream)
    cms = CountMinSketch.from_error(epsilon=0.001, delta=0.01, conservative=conservative)
    for x in stream:
        cms.add(x)
    bound = cms.epsilon * cms.total
    violations = 0
    for item, true in truth.items():
        est = cms.estimate(item)
        assert est >= true  # one-sided: collisions only add
        violations += est - true > bound
    assert violations / len(truth) <= cms.delta


def test_conservative_update_is_tighter():
    stream = zipf_stream(50_000, 5_000)
    truth = Counter(stream)
    plain = CountMinSketch(200, 4)
    cons = CountMinSketch(200, 4, conservative=True)
    for x in stream:
        plain.add(x)
        cons.add(x)
    err = lambda s: sum(s.estimate(i) - c for i, c in truth.items())  # noqa: E731
    # No fixed factor is guaranteed; on this undersized sketch we measure ~43% less
    # total over-count. Assert a clear improvement; benchmarks report the real number.
    assert err(cons) < 0.7 * err(plain)


def test_merge_equals_sketching_the_combined_stream():
    s1, s2 = zipf_stream(10_000, 1_000, seed=1), zipf_stream(10_000, 1_000, seed=2)
    a, b, both = CountMinSketch(500, 5), CountMinSketch(500, 5), CountMinSketch(500, 5)
    for x in s1:
        a.add(x)
        both.add(x)
    for x in s2:
        b.add(x)
        both.add(x)
    a.merge(b)
    assert a == both


def test_sizing_from_error():
    cms = CountMinSketch.from_error(epsilon=0.01, delta=0.001)
    assert cms.width == 272 and cms.depth == 7
    assert cms.epsilon <= 0.01 and cms.delta <= 0.001


def test_round_trip_and_weighted_adds():
    cms = CountMinSketch(100, 3, conservative=True)
    cms.add("a", 5)
    cms.add("b")
    restored = CountMinSketch.from_bytes(cms.to_bytes())
    assert restored == cms and restored["a"] >= 5 and restored.total == 6


def test_rejects_bad_input():
    with pytest.raises(ValueError):
        CountMinSketch(10, 2).add("x", -1)
    with pytest.raises(ValueError):
        CountMinSketch(10, 2).merge(CountMinSketch(11, 2))
    with pytest.raises(ValueError):
        CountMinSketch.from_error(epsilon=0)
