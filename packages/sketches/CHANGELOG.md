# Changelog

All notable changes to this package are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-09-28
### Added
- `HyperLogLog` with small-range correction, exact union via `merge`, and binary serialization.
- `CountMinSketch` with optional conservative update, error-based sizing (`from_error`), `merge`, and serialization.
- `TopK` heavy hitters (Count-Min Sketch plus a lazily invalidated min-heap), with `merge` and serialization.
- Versioned, platform-independent binary format for all three structures.
