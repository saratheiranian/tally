#!/usr/bin/env python3
"""Send realistic sample events to a running Tally ingest API (stdlib only).

    python scripts/send_events.py --key tk_live_... [--batches 10 --size 50]
"""

import argparse
import json
import random
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime

PAGES = ["/", "/pricing", "/docs", "/blog", "/signup", "/login"]
EVENTS = ["page_view"] * 8 + ["signup", "purchase"]


def make_event() -> dict:
    name = random.choice(EVENTS)
    return {
        "event_id": str(uuid.uuid4()),
        "name": name,
        "distinct_id": f"user_{random.randint(1, 200)}",
        "properties": {"path": random.choice(PAGES)} if name == "page_view" else {},
        "occurred_at": datetime.now(UTC).isoformat(),
    }


def post(url: str, key: str, events: list[dict]) -> tuple[int, dict, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps({"events": events}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--key", required=True)
    p.add_argument("--url", default="http://localhost:8000/v1/events")
    p.add_argument("--batches", type=int, default=10)
    p.add_argument("--size", type=int, default=50)
    a = p.parse_args()

    for i in range(a.batches):
        batch = [make_event() for _ in range(a.size)]
        status, body, headers = post(a.url, a.key, batch)
        if status == 429:  # respect back-pressure, then retry the SAME batch (idempotent)
            wait = int(headers.get("Retry-After", 1))
            print(f"batch {i}: 429, retrying in {wait}s")
            time.sleep(wait)
            status, body, _ = post(a.url, a.key, batch)
        print(f"batch {i}: {status} {body}")


if __name__ == "__main__":
    main()
