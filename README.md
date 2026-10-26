# shopsim

A small web shop made of seven HTTP services. It is the system under test for Perftelion's integration lab.

```
                    ┌──────────────┐
 load generator ──▶ │   gateway    │ :7000   public API (/api/*)
                    └──┬────────┬──┘
          GET /products│        │/orders
                ┌──────▼───┐  ┌─▼────────┐
         :7001  │ products │  │  orders  │ :7002
                └──┬────┬──┘  └┬───┬───┬─┘
       products-db │    │      │   │   │ (async) POST /notifications
                   │    │ GET  │   │   └──────────────▶ notifications :7005
                   │    │/stock│   │ POST /payments
                   │  ┌─▼──────▼─┐ └──▶ payments :7004 ──▶ bank-sim :7006 (external bank)
                   │  │inventory │ :7003
                   │  └────┬─────┘
                   │       │ inventory-db
                   ▼       ▼
                SQLite files (each statement pays a simulated network round trip)
```

| Service | Port | Responsibility | Data store |
|---|---|---|---|
| gateway | 7000 | Public API, routes to internal services | — |
| products | 7001 | Product catalogue; product detail includes stock | products-db |
| orders | 7002 | Checkout: reserve stock → authorise payment → store order → confirm | orders-db |
| inventory | 7003 | Stock levels and reservations | inventory-db |
| payments | 7004 | Card authorisation via the bank | — |
| notifications | 7005 | Order confirmation e-mail/SMS (simulated provider) | — |
| bank-sim | 7006 | Simulated external acquirer (log-normal latency, long tail) | — |

## Public API (gateway)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/products?category=&limit=` | list products |
| GET | `/api/products/{productId}` | product `p-1` … `p-500` with availability |
| POST | `/api/orders` | body `{"customerId":"c-1","productId":"p-1","quantity":1,"paymentMethod":"card"}` |
| GET | `/api/orders/{orderId}` | seeded orders `o-1` … `o-5000` exist from the start |

Every service also serves `GET /health`, `GET /metrics` (Prometheus), `GET /openapi.json` and `GET|PUT /admin/faults`.

## Observability

* **Logs** — one JSON object per line in `<log_dir>/<service>.log`; flat fields (`@timestamp`, `level`, `service`, `version`, `message`, `trace_id`, `span_id`, `event`, `http_route`, `http_status`, `duration_ms`, …).
* **Traces** — OTLP/HTTP to the lab collector; SERVER spans per route template, CLIENT spans per downstream call and per database statement, `db.pool.acquire` spans for connection-pool waits.
* **Metrics** — request duration histograms, in-flight requests, DB pool size / in-use / wait, process CPU and memory, and the ratios `sut_cpu_utilization_of_limit_ratio` / `sut_memory_utilization_of_limit_ratio`.

## Resource limits

Each service runs inside a Windows Job Object with a hard CPU cap and a committed-memory limit (the equivalent of a container's cgroup limits). The limits are environment configuration (`shopsim.json` on the lab host), not code, and are reported by `/health`, the `service_started` log line and the `sut_*_limit_*` metrics.

## Versioning and deployment

`versions.json` holds each service's version. A deployment is a commit to this repository followed by a restart of the services whose code changed; the deployment is recorded in Perftelion as a deployment event that carries the commit SHA. See `docs/capacity.md` for the capacity assumptions behind the pool sizes and timeouts.
