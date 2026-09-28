# tally-sketches

**Mergeable, serializable probabilistic data structures for Python, with zero dependencies.**

Count unique users, find the most popular items, and estimate frequencies over unbounded streams, in a fixed amount of memory. Every structure can be serialized to bytes, stored, and **merged** with others. You can sketch per shard, per worker, or per day, and combine later without double counting.

Extracted from [Tally](https://github.com/YOUR_USERNAME/tally), a multi-tenant event analytics platform.

```bash
pip install tally-sketches
```

## HyperLogLog: count distinct items

```python
from tally_sketches import HyperLogLog

monday, tuesday = HyperLogLog(), HyperLogLog()   # 16 KiB each, ±0.81%
monday.update(["alice", "bob", "carol"])
tuesday.update(["bob", "dave"])

monday.merge(tuesday)       # exact union: bob is not counted twice
len(monday)                 # 4
```

## Count-Min Sketch: estimate frequencies

```python
from tally_sketches import CountMinSketch

cms = CountMinSketch.from_error(epsilon=0.001, delta=0.01)   # size from the error you accept
cms.add("/pricing")
cms.add("/docs", count=5)
cms["/docs"]                # 5; never underestimates
```

## TopK: heavy hitters

```python
from tally_sketches import TopK

top = TopK(k=10)
for path in page_views:
    top.add(path)
top.top(3)                  # [("/", 1840), ("/pricing", 912), ("/docs", 455)]
```

## Guarantees

| Structure | Guarantee | Memory |
|---|---|---|
| `HyperLogLog(p)` | Standard error `1.04 / √(2ᵖ)`; 0.81% at the default `p=14` | `2ᵖ` bytes (16 KiB default) |
| `CountMinSketch.from_error(ε, δ)` | `true ≤ estimate ≤ true + ε·N` with probability `≥ 1 − δ` | `8 · ⌈e/ε⌉ · ⌈ln 1/δ⌉` bytes |
| `TopK(k, ε, δ)` | Finds the true heavy hitters on skewed data; counts carry Count-Min's one-sided bound | Count-Min plus `O(k)` |

**Merging.** `HyperLogLog.merge` produces *exactly* the sketch of the union, and it's commutative and idempotent. `CountMinSketch.merge` is exact for plain sketches; with conservative update it remains a valid upper bound. `TopK.merge` merges the sketches and re-ranks both candidate sets.

**Serialization.** `to_bytes()` / `from_bytes()` use a versioned, little-endian format on every platform, so sketches move safely between machines.

**Stable hashing.** BLAKE2b, not Python's per-process-salted `hash()`, so sketches built in different processes are compatible.

## Accuracy

Measured, not just claimed: see [`benchmarks/`](benchmarks). Run `python benchmarks/accuracy.py` to reproduce.

## Development

```bash
pip install -e ".[dev]"
pytest && ruff check .
```

## License

MIT
