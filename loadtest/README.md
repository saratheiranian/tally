# Load testing

`ingest.js` is a [k6](https://k6.io) test using an **open workload model**: a constant *arrival rate* of requests, no matter how slowly the server answers. A closed model (N users in a loop) quietly slows its own load down when the server slows down. That hides saturation and makes latency look better than it is, a problem known as *coordinated omission*. Here, if the server can't keep up, requests queue or get dropped, and the thresholds fail.

Each run mixes **ingest** (`POST /v1/events`, 100 events per request) with a background **dashboard** load of 2 stats queries per second, and checks:

| Threshold | Meaning |
|---|---|
| ingest error rate < 1% | Correctness under load |
| ingest p99 < 500 ms | Tail latency, not averages |
| stats p95 < 500 ms | Reads stay fast while writes run |
| 0 dropped iterations | The server kept up with the offered load |

```bash
make loadtest KEY=tk_live_... RATE=50 DURATION=60s                      # local stack
make loadtest KEY=tk_live_... URL=$(terraform -chdir=infra/terraform output -raw api_url) RATE=200
```

Results are written to `loadtest/results/<label>.json`.

## Results: single-vCPU sandbox (baseline)

**Setup, stated plainly:** everything shares **one vCPU and 3 GB of RAM**: the k6 load generator, a single-process API, Postgres 16, and Redis 7. This is `TALLY_SINK=postgres` mode (synchronous writes). The SQS pipeline was exercised for correctness (see [`chaos/`](../chaos)) but not benchmarked here, because the local AWS emulator, not Tally, would be the bottleneck. Treat these as a floor, not a capacity claim.

Step test, 20 s per step, 100 events per request:

| Offered | Achieved | Events/s | Ingest p50 | Ingest p99 | Stats p95 | Errors | Thresholds |
|---|---|---|---|---|---|---|---|
| 5 req/s | 5.0 req/s | 504 | 7 ms | 26 ms | 24 ms | 0% | ✅ pass |
| 10 req/s | 10.0 req/s | 1,003 | 7 ms | 18 ms | 50 ms | 0% | ✅ pass |
| 20 req/s | 19.9 req/s | 1,992 | 6 ms | 21 ms | 156 ms | 0% | ✅ pass |
| 40 req/s | 39.4 req/s | 3,935 | 12 ms | 285 ms | 518 ms | 0% | ❌ stats p95 |
| 60 req/s | 59.4 req/s | 5,941 | 29 ms | 612 ms | 676 ms | 0% | ❌ p99, 1 dropped |

**Reading it:**

* **About 2,000 events/s comfortably, and about 4,000 events/s at the edge, on one shared core**, with zero errors at every step: 270,400 events ingested in total.
* **The first thing to break is reads, not writes.** In postgres mode, stats are an exact `COUNT(DISTINCT)` over an events table growing by thousands of rows a second, so they slow down as data accumulates. That is exactly the problem the sketch pipeline solves. With sketches, the same query took **9 ms** on 5,000 events versus 1,455 ms for an exact scan (see the main README), and sketch cost depends on the number of days, not the number of events.
* **Past the knee (40–60 req/s), tail latency climbs** while the median stays low, the classic signature of CPU saturation and queueing. Autoscaling in AWS targets 60% CPU for exactly this reason: scale out *before* p99 falls apart.

## Results: AWS (to be filled in after `terraform apply`)

Run the same script against the deployed ALB with the default demo sizing (2–6 API tasks at 0.5 vCPU, SQS pipeline) and record the results here, with a screenshot of the CloudWatch dashboard during the run.

| Offered | Achieved | Events/s | Ingest p99 | Stats p95 | API tasks (autoscaled) | Thresholds |
|---|---|---|---|---|---|---|
| | | | | | | |
