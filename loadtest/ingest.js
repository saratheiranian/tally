// Tally load test (k6).
//
//   k6 run -e BASE_URL=http://localhost:8000 -e API_KEY=tk_live_... \
//          -e RATE=50 -e BATCH=100 -e DURATION=60s loadtest/ingest.js
//
// Open model: a constant *arrival rate* of requests, whatever the latency. A
// closed model (fixed VUs looping) quietly slows its own load when the server
// slows down, which hides saturation. That's the classic "coordinated omission"
// mistake. Here, if the server can't keep up, iterations queue or get dropped,
// and the thresholds fail.
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const KEY = __ENV.API_KEY;
const RATE = Number(__ENV.RATE || 50); // ingest requests per second
const BATCH = Number(__ENV.BATCH || 100); // events per request
const DURATION = __ENV.DURATION || "60s";
const LABEL = __ENV.LABEL || `rate${RATE}-batch${BATCH}`;

if (!KEY) throw new Error("set -e API_KEY=tk_live_...");

const eventsAccepted = new Counter("events_accepted");
const rateLimited = new Counter("rate_limited_429");

export const options = {
  discardResponseBodies: false,
  scenarios: {
    ingest: {
      executor: "constant-arrival-rate",
      exec: "ingest",
      rate: RATE,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: Math.max(10, RATE),
      maxVUs: Math.max(50, RATE * 10),
    },
    stats: {
      // Dashboards polling in the background while ingest runs.
      executor: "constant-arrival-rate",
      exec: "stats",
      rate: 2,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: 2,
      maxVUs: 20,
    },
  },
  thresholds: {
    "http_req_failed{scenario:ingest}": ["rate<0.01"],
    "http_req_duration{scenario:ingest}": ["p(99)<500"],
    "http_req_duration{scenario:stats}": ["p(95)<500"],
    dropped_iterations: ["count<1"], // the server kept up with the offered load
  },
  summaryTrendStats: ["avg", "p(50)", "p(90)", "p(95)", "p(99)", "max"],
};

// RFC 4122 v4 from Math.random: fine for load generation (not for security).
function uuid4() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

const PAGES = ["/", "/pricing", "/docs", "/blog", "/signup", "/login", "/about", "/careers"];
const headers = { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" };

export function ingest() {
  const now = new Date().toISOString();
  const events = [];
  for (let i = 0; i < BATCH; i++) {
    const pv = Math.random() < 0.8;
    events.push({
      event_id: uuid4(),
      name: pv ? "page_view" : Math.random() < 0.5 ? "signup" : "purchase",
      distinct_id: `user_${Math.floor(Math.random() * 50000)}`,
      properties: pv ? { path: PAGES[Math.floor(Math.random() * PAGES.length)] } : {},
      occurred_at: now,
    });
  }
  const res = http.post(`${BASE}/v1/events`, JSON.stringify({ events }), { headers, tags: { name: "POST /v1/events" } });
  if (res.status === 202) eventsAccepted.add(BATCH);
  if (res.status === 429) rateLimited.add(1);
  check(res, { "ingest 202": (r) => r.status === 202 });
}

export function stats() {
  const d = new Date().toISOString().slice(0, 10);
  const res = http.get(`${BASE}/v1/stats?start=${d}&end=${d}`, { headers, tags: { name: "GET /v1/stats" } });
  check(res, { "stats 200": (r) => r.status === 200 });
}

export function handleSummary(data) {
  const m = data.metrics;
  const secs = data.state.testRunDurationMs / 1000;
  const ing = m["http_req_duration{scenario:ingest}"] || m.http_req_duration;
  const pick = (metric, k) => (metric && metric.values[k] !== undefined ? metric.values[k] : null);
  const summary = {
    label: LABEL,
    offered_rps: RATE,
    batch: BATCH,
    duration_s: Math.round(secs),
    events_per_s: Math.round((pick(m.events_accepted, "count") || 0) / secs),
    ingest_rps_achieved: Math.round(((pick(m.events_accepted, "count") || 0) / BATCH / secs) * 10) / 10,
    ingest_p50_ms: pick(ing, "p(50)"),
    ingest_p99_ms: pick(ing, "p(99)"),
    stats_p95_ms: pick(m["http_req_duration{scenario:stats}"], "p(95)"),
    error_rate: pick(m["http_req_failed{scenario:ingest}"], "rate"),
    rate_limited: pick(m.rate_limited_429, "count") || 0,
    dropped_iterations: pick(m.dropped_iterations, "count") || 0,
    thresholds_passed: Object.values(m).every((x) => !x.thresholds || Object.values(x.thresholds).every((t) => t.ok)),
  };
  return {
    stdout: JSON.stringify(summary, null, 2) + "\n",
    [`loadtest/results/${LABEL}.json`]: JSON.stringify(summary, null, 2),
  };
}
