import random
from collections import Counter

from tally_sketches import TopK


def zipf_stream(n, distinct, s=1.2, seed=11):
    rng = random.Random(seed)
    weights = [1 / (r**s) for r in range(1, distinct + 1)]
    return [f"/page/{i}" for i in rng.choices(range(distinct), weights, k=n)]


def test_finds_the_true_heavy_hitters():
    stream = zipf_stream(100_000, 20_000)
    tk = TopK(k=10)
    tk.update(stream)
    true_top = [item for item, _ in Counter(stream).most_common(10)]
    found = [item for item, _ in tk.top()]
    assert found == true_top  # same items, same order


def test_counts_never_underestimate():
    stream = zipf_stream(30_000, 5_000)
    truth = Counter(stream)
    tk = TopK(k=20)
    tk.update(stream)
    for item, est in tk.top():
        assert est >= truth[item]


def test_memory_is_bounded_regardless_of_distinct_items():
    tk = TopK(k=5)
    tk.update(f"unique-{i}" for i in range(50_000))
    assert len(tk._candidates) == 5
    assert len(tk._heap) <= 4 * 5 + 16 + 1


def test_merging_two_halves_matches_one_pass():
    stream = zipf_stream(60_000, 10_000)
    a, b, whole = TopK(k=10), TopK(k=10), TopK(k=10)
    a.update(stream[:30_000])
    b.update(stream[30_000:])
    whole.update(stream)
    a.merge(b)
    assert [i for i, _ in a.top()] == [i for i, _ in whole.top()]


def test_round_trip_serialization():
    tk = TopK(k=5)
    tk.update(zipf_stream(5_000, 500))
    restored = TopK.from_bytes(tk.to_bytes())
    assert restored.top() == tk.top() and restored.total == tk.total
    restored.add("/new")  # still usable after loading
    assert restored.total == tk.total + 1


def test_handles_unicode_items():
    tk = TopK(k=3)
    for item in ["/café", "/日本", "/café", "/emoji/🎉"]:
        tk.add(item)
    assert TopK.from_bytes(tk.to_bytes()).top()[0] == ("/café", 2)
