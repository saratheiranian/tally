"""CloudWatch Embedded Metric Format (EMF).

Printing one JSON line with an `_aws` block makes CloudWatch Logs extract real
metrics from it asynchronously: no agent, no PutMetricData calls on the hot path,
and the raw log line stays searchable. Locally these are just JSON log lines.
"""

import json
import sys
import time

NAMESPACE = "Tally"


def emit(dimensions: dict[str, str], metrics: dict[str, tuple[float, str]]) -> None:
    """metrics: name -> (value, unit), e.g. {"CommitLatency": (12.5, "Milliseconds")}."""
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [list(dimensions), []],  # per-dimension AND fleet-wide totals
                    "Metrics": [{"Name": n, "Unit": u} for n, (_, u) in metrics.items()],
                }
            ],
        },
        **dimensions,
        **{n: v for n, (v, _) in metrics.items()},
    }
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()
