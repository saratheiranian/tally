import uuid
from datetime import UTC, datetime

from .conftest import make_tenant, sql


def event(**overrides):
    e = {
        "event_id": str(uuid.uuid4()),
        "name": "page_view",
        "distinct_id": "user_1",
        "properties": {"path": "/pricing"},
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    e.update(overrides)
    return e


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_rejects_missing_and_invalid_keys(client):
    assert client.post("/v1/events", json={"events": [event()]}).status_code == 401
    r = client.post("/v1/events", json={"events": [event()]}, headers=auth("tk_live_nope"))
    assert r.status_code == 401


def test_revoked_key_is_rejected(client, tenant):
    _, key = tenant
    sql("UPDATE api_keys SET revoked_at = now()")
    assert client.post("/v1/events", json={"events": [event()]}, headers=auth(key)).status_code == 401


def test_ingests_batch_and_records_usage(client, tenant):
    tenant_id, key = tenant
    r = client.post("/v1/events", json={"events": [event() for _ in range(3)]}, headers=auth(key))
    assert r.status_code == 202
    assert r.json() == {"accepted": 3, "duplicates": 0}
    assert "X-RateLimit-Remaining" in r.headers
    assert sql("SELECT count(*) FROM events WHERE tenant_id = $1", tenant_id)[0][0] == 3
    assert sql("SELECT event_count FROM usage_daily WHERE tenant_id = $1", tenant_id)[0][0] == 3


def test_retries_are_idempotent_and_not_billed_twice(client, tenant):
    tenant_id, key = tenant
    batch = {"events": [event() for _ in range(4)]}
    client.post("/v1/events", json=batch, headers=auth(key))
    r = client.post("/v1/events", json=batch, headers=auth(key))  # client retry
    assert r.json() == {"accepted": 0, "duplicates": 4}
    assert sql("SELECT count(*) FROM events")[0][0] == 4
    assert sql("SELECT event_count FROM usage_daily WHERE tenant_id = $1", tenant_id)[0][0] == 4


def test_duplicates_within_one_batch(client, tenant):
    _, key = tenant
    e = event()
    r = client.post("/v1/events", json={"events": [e, e, event()]}, headers=auth(key))
    assert r.json() == {"accepted": 2, "duplicates": 1}


def test_same_event_id_is_independent_across_tenants(client):
    _, key_a = make_tenant()
    _, key_b = make_tenant()
    e = event()
    assert client.post("/v1/events", json={"events": [e]}, headers=auth(key_a)).json()["accepted"] == 1
    assert client.post("/v1/events", json={"events": [e]}, headers=auth(key_b)).json()["accepted"] == 1


def test_rate_limit_returns_429_with_retry_after(client):
    _, key = make_tenant(rate=1, burst=5)
    assert (
        client.post("/v1/events", json={"events": [event() for _ in range(5)]}, headers=auth(key)).status_code
        == 202
    )
    r = client.post("/v1/events", json={"events": [event()]}, headers=auth(key))
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_batch_larger_than_burst_gets_413(client):
    _, key = make_tenant(rate=10, burst=5)
    r = client.post("/v1/events", json={"events": [event() for _ in range(6)]}, headers=auth(key))
    assert r.status_code == 413


def test_validation_errors(client, tenant):
    _, key = tenant
    assert client.post("/v1/events", json={"events": []}, headers=auth(key)).status_code == 422
    bad = event(event_id="not-a-uuid")
    assert client.post("/v1/events", json={"events": [bad]}, headers=auth(key)).status_code == 422


def test_naive_timestamp_is_treated_as_utc(client, tenant):
    _, key = tenant
    client.post("/v1/events", json={"events": [event(occurred_at="2026-01-01T12:00:00")]}, headers=auth(key))
    ts = sql("SELECT occurred_at FROM events")[0][0]
    assert ts == datetime(2026, 1, 1, 12, tzinfo=UTC)


def test_health_endpoints(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"postgres": "ok", "redis": "ok"}


def test_dashboard_is_served(client):
    r = client.get("/dashboard")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "/v1/stats" in r.text
