# Capacity assumptions

Design load (from the Perftelion catalog load expectations):

| Flow | web-shop | mobile-app | Total |
|---|---|---|---|
| browse-catalog | 40 TPS | 20 TPS | 60 TPS |
| checkout | 5 TPS | 3 TPS | 8 TPS |

Resulting request rates per service: gateway ≈ 118 rps, products ≈ 104 rps, inventory ≈ 52 rps, orders ≈ 14 rps, payments / bank-sim / notifications ≈ 8 rps.

## inventory-db connection pool

* Statements pay a ~15 ms network round trip to the database server.
* A stock lookup is 1 statement; a reservation is 2 statements.
* Demand at design load: 44 lookups/s × 15 ms + 8 reservations/s × 30 ms ≈ 0.9 connection-seconds per second.
* `DB_POOL_SIZE = 16` keeps pool utilisation under 10 % at design load and leaves room for 3–4× spikes.
* **A pool smaller than ~4 connections will queue at design load**; every extra statement per request multiplies demand.

## Bank authorisation timeout

* bank-sim latency is log-normal: median ≈ 90 ms, p90 ≈ 270 ms, p99 ≈ 650 ms.
* `BANK_TIMEOUT_SEC = 2.0` fails fewer than 0.05 % of authorisations.
* A timeout of 300 ms would fail roughly 8 % of checkouts.

## CPU

* Services are CPU-capped (see `shopsim.json`): inventory 1.0 core, products 1.0, gateway 1.0, orders/payments 0.5, notifications/bank-sim 0.25–0.5.
* Request handling costs ~1–3 ms CPU, so design load uses 10–40 % of each cap. Synchronous CPU work on the event loop (hashing, compression, large JSON) is not budgeted.

## Memory

* Services are memory-capped (192–320 MB committed). A service process needs ~70–90 MB at rest.
* Unbounded in-process caches are not allowed; a cache must have a size bound and an eviction policy.
