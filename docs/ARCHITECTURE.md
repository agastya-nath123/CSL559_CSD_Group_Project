# Architecture of the prototype

Read this once before your first task. It explains how the code is organised, how one order
travels through it, and the rules that keep four people's work from colliding.

## 1. The shape: a modular monolith

The HLD describes eleven services. The prototype runs them all in **one process**, as eleven
Python packages under `app/modules/`. HLD section 13.1 recommends exactly this for a student
build: "a student prototype can start as a modular monolith with the same boundaries".

```
                 browser (web/)            other callers (curl, tests)
                        \                       /
                         v                     v
                   +-------------------------------+
                   |  FastAPI app  (app/main.py)   |   <- stands in for the API gateway
                   +-------------------------------+
                                  |
        +-------------+-----------+-----------+----------------+
        v             v                       v                v
  serviceability   fulfilment --> pickpack   inbound      replenishment
                        \            |          |          forecasting
                         \           |          |          slotting / assortment
                          v          v          v
                        +------------------------+
                        |  inventory/service.py  |   <- the only code that writes stock
                        +------------------------+
                                  |
                 +----------------+-----------------+
                 v                                  v
        SQLite (app/models.py)             event_log table (app/events.py)
        stands in for PostgreSQL           stands in for Kafka
```

Each module has two files:

| File | Contains | Rule |
| --- | --- | --- |
| `service.py` | Business logic as plain functions: `reserve(db, ...)`, `pack(db, ...)` | First argument is always the database session. No HTTP types here. |
| `router.py` | HTTP endpoints | Parse input, call **one** service function, shape the output. No business rules here. |

Because logic lives in `service.py`, modules can call each other directly
(`inventory.allocate(...)`) and tests can call logic without going through HTTP.

## 2. One order, followed through the code

| Step | HTTP call | Function | What happens in the database |
| --- | --- | --- | --- |
| Find store | `GET /v1/serviceability` | `serviceability.check` | Read only |
| Browse | `GET /v1/stores/{id}/availability` | `inventory.availability` | Read only. ATP = `qty_on_hand − qty_reserved` |
| Checkout | `POST /v1/reservations` | `inventory.reserve` | One conditional `UPDATE` per line raises `qty_reserved`; a `Reservation` row is written. All lines succeed or the transaction rolls back |
| Pay | `POST /v1/reservations/{id}/confirm` | `inventory.confirm` | Reservation becomes `confirmed`; publishes `order.paid` |
| (event) | | `fulfilment.on_order_paid` → `create_job` | A `FulfilmentJob` and its `PickTask` rows are written. `inventory.allocate` chooses bins, oldest batch first |
| Claim | `GET /v1/pickers/{id}/next-task` | `pickpack.next_job` | Conditional `UPDATE` moves the job `created → picking` for exactly one picker |
| Claim at peak | `GET /v1/pickers/{id}/next-batch` | `pickpack.next_batch` | With `BATCH_PICK_BACKLOG` or more waiting, the same conditional `UPDATE` for up to `BATCH_SIZE` jobs, each given a `tote` letter; one route through the store for all of them |
| Scan | `POST /v1/pick-tasks/{id}/scans` | `pickpack.scan` | Barcode checked; a conditional `UPDATE` adds the units to `scanned_qty` only if the line has room; the task becomes `picked` (with `picked_at`) when every unit is scanned |
| Empty shelf | `POST /v1/pick-tasks/{id}/short` | `pickpack.report_short` | Units found (`{"found": n}`, default: those already scanned) become a `picked` line of their own; for the rest, count corrected (`not_found` ledger row), re-allocated to another bin, or dropped: hold released, `order.line_dropped` published, and a substitute offered (`substitute_offer`) |
| Pack | `POST /v1/fulfilment-jobs/{id}/packed` | `pickpack.pack` → `inventory.commit_sale` | `StockItem.qty`, `qty_on_hand` and `qty_reserved` all drop; `sale` ledger rows; publishes `order.ready_for_pickup` |
| Cancel (until packed) | `POST /v1/fulfilment-jobs/{id}/cancel` | `fulfilment.cancel` → `inventory.unreserve` | Conditional `UPDATE` moves the job `created`/`picking → cancelled`; `qty_reserved` drops for every line still held; `qty_on_hand` is untouched; publishes `order.cancelled` |
| Rider assigned | (event `order.ready_for_pickup`) | `riders.assign_rider` | A `RiderAssignment` row (`assigned`); publishes `rider.assigned` |
| Rider arrives | `POST /v1/riders/{id}/arrive` | `riders.arrive` | Assignment becomes `arrived`; answers the crate slot |
| Handoff | `POST /v1/fulfilment-jobs/{id}/handover` | `riders.handover` | Only with this order's rider and its own bag scanned: conditional `UPDATE` to `handed_over`, assignment `collected` |

Two moments matter most:

- **Checkout** is where overselling is prevented. Stock is *held*, not removed.
- **Packing** is where the sale is *committed*. Until then a cancel or a failed payment only has to
  release a hold; no stock count ever has to be "put back".

## 3. Stock is stored twice, on purpose

| Table | One row per | Used for |
| --- | --- | --- |
| `StockLevel` | store + SKU | Totals: `qty_on_hand`, `qty_reserved`, reorder point. Fast ATP reads and the atomic hold |
| `StockItem` | store + SKU + bin + batch | Where the units physically are and when they expire. Pick paths, FEFO, putaway |
| `StockLedger` | every movement | Append-only history: who moved what, when and why |

The HLD puts `qty_reserved` on every `StockItem` row. The prototype holds stock at the store + SKU
level instead, because a hold then needs exactly one row to change, which keeps the no-overselling
`UPDATE` simple. Bins are chosen later, when the pick tasks are created.

**The three records must always agree:**

```
StockLevel.qty_on_hand  ==  sum of StockItem.qty  ==  sum of StockLedger.delta
0  <=  StockLevel.qty_reserved  <=  StockLevel.qty_on_hand
```

`assert_stock_is_consistent()` in `tests/conftest.py` checks this. Call it at the end of every
test that moves stock. If it fails, some code changed one record without the others.

## 4. Events

`events.publish(db, type, store_id, payload)` writes a row to `event_log` **inside the caller's
transaction** and then calls any subscribed handlers in the same transaction. If the stock change
rolls back, so does the event. This is the "transactional outbox" from HLD 9.2. A relay inside the app
(`events.relay_once`, a few times a second) sends unsent events, oldest first, to every sink (today the
WebSocket feed) and marks them published; delivery is at least once.

| Event | Published by | Handled by (today) |
| --- | --- | --- |
| `stock.changed` | inventory, pickpack | replenishment (raises `stock.low`); inventory counts (opens a count of the bin when the cause is `not_found`) |
| `stock.low` | replenishment | nobody yet |
| `count.needs_approval` | inventory counts (difference above `COUNT_AUTO_APPROVE_VALUE`) | nobody yet (live feed shows it) |
| `item.near_expiry` | inventory (hourly job, once per batch per day) | nobody yet (live feed shows it) |
| `order.paid` | inventory | fulfilment (creates the job) |
| `fulfilment.state_changed` | fulfilment, pickpack | nobody yet |
| `order.ready_for_pickup` | pickpack | fulfilment riders (assigns a made-up rider, standing in for the rider system) |
| `rider.assigned` (payload: `job_id`, `order_id`, `rider_id`) | fulfilment riders | nobody yet (live feed shows it) |
| `order.cancelled` (payload: `order_id`, `job_id`, `reason`, `refund_amount`) | fulfilment | nobody yet (the Order service would refund `refund_amount`: the units still held; dropped units were refunded by `order.line_dropped`) |
| `order.line_dropped` (payload: `order_id`, `job_id`, `sku_id`, `qty`, `refund_amount`) | fulfilment (when a job is created), pickpack (empty shelf) | nobody yet (the Order service would refund that line) |
| `order.line_substituted` (payload: `order_id`, `job_id`, `dropped_sku_id`, `substitute_sku_id`, `qty`, `charge_amount`) | pickpack (substitute accepted) | nobody yet (the Order service would charge for the substitute; the dropped units were refunded by `order.line_dropped`) |
| `store.status_changed` | fulfilment | nobody yet |
| `transfer_order.created` / `.dispatched` | replenishment | nobody yet (the hub WMS would) |
| `goods.received` | inbound | nobody yet |
| `forecast.updated` | forecasting | nobody yet |

To react to an event, subscribe in your module's `service.py`:

```python
from ... import events

@events.subscribe("stock.low")
def on_stock_low(db, store_id, payload):
    ...   # runs inside the publisher's transaction; do not commit here
```

Handlers must not call `db.commit()`: the code that published the event commits once, at the end.

## 5. Transactions: who commits?

- Functions called straight from a router (`reserve`, `confirm`, `pack`, ...) commit. A write that takes an
  `Idempotency-Key` (`inventory.adjust`, the receiving steps in `inbound`) is called with `commit=False`, and
  `run_once` commits it together with the saved answer.
- Helper functions that are one step of a bigger operation (`inventory.receive`, `commit_sale`,
  `unreserve`, `write_off_missing`, `fulfilment.create_job`) do **not** commit. Their docstrings say so.
- Event handlers never commit.

If you are unsure, read the docstring. If it does not say, fix the docstring in your pull request.

## 6. Concurrency rules used in the code

Two places must be safe when many requests arrive at once. Both use the same trick: an `UPDATE`
with the check in its `WHERE` clause, then look at how many rows changed.

```python
# inventory.reserve: hold stock only if enough is free right now
UPDATE stock_levels SET qty_reserved = qty_reserved + :qty
WHERE store_id = :store AND sku_id = :sku AND qty_on_hand - qty_reserved >= :qty
# rowcount == 0  ->  not enough stock

# pickpack.next_job: give an order to exactly one picker
UPDATE fulfilment_jobs SET state = 'picking', picker_id = :me
WHERE id = :job AND state = 'created'
# rowcount == 0  ->  another picker got there first
```

Never write "read the value, check it in Python, then write it back" for stock or job state.
Two requests can both read the old value and both pass the check.

## 7. Errors

Raise `DomainError(status, code, message, details)` from service code. `main.py` turns it into:

```json
{"error": {"code": "insufficient_stock", "message": "Not enough stock for one or more items",
           "details": {"lines": [{"sku_id": 14, "requested": 2, "available": 1}]}}}
```

Use `409` when the request is valid but the current state forbids it (not enough stock, wrong job
state), `422` for bad input, and `404` via `errors.not_found(...)`.

## 8. Where the prototype differs from the HLD

| HLD | Prototype | Who closes the gap |
| --- | --- | --- |
| PostgreSQL sharded by `store_id` | One SQLite file | Member 1 (PostgreSQL), sharding stays a design-only topic |
| Redis ATP cache | Each store's availability list kept in process for `ATP_CACHE_SECONDS` (2), cleared on every stock write (`app/cache.py`); holds never read it | Member 1: done in process; a shared Redis would serve several instances |
| Kafka | `event_log` outbox + in-process handlers; a relay in the app sends events to sinks (the live feed) | Member 4: done as far as the prototype goes; a Kafka producer would be one more sink |
| API gateway, JWT, roles | Signed tokens, roles and own-store checks on every staff endpoint; audit log; a per-client rate limit on the open endpoints (`app/rate_limit.py`); no separate gateway | Member 4: done, except the gateway itself |
| H3 delivery zones | H3 cells (resolution 9) mapped to stores | Member 4: done |
| Hourly forecasts, ML model | Daily forecast: three simple models compared by WAPE, weekday average in use | Member 3 |
| Reservation timeout sweeper | Automatic sweep every `SWEEP_INTERVAL_SECONDS`, plus the manual endpoint | Member 1: done |
| Substitution and refund saga | Line is dropped, hold released, `order.line_dropped` for the refund, a substitute offered (same category, never dearer); only that offer can be accepted, with `POST /v1/fulfilment-jobs/{id}/substitutions` (`inventory.swap_line`), the picker answering for the customer | Member 2: done |
| Stuck orders | No claim, scan or shelf report for `PICK_STALL_MINUTES`: back to the queue every `STALL_CHECK_SECONDS` (`fulfilment.recover_stalled`), or flagged if items are in a tote | Member 2: done |
| Batch picking at peak | Up to `BATCH_SIZE` oldest orders in one walk at peak, one tote each; not grouped by where their items are | Member 2: done |
| Scan-item-then-bin putaway | Saved goods receipts in five steps: open, scan onto the dock, putaway tasks, put away line by line (sellable at once), close with the shortfalls; damaged and short-dated units are never stocked; `Idempotency-Key` on every write | Member 3: done; the ops screen still receives in one call |
| Fast movers near the packing station (slotting) | Each zone's SKUs ranked by 14 days of sales get bins in walking order: a look-only plan, an apply through `inventory.move` within bin capacity, new deliveries to the planned bin; `scripts/compare_slotting.py` measures the walk before and after | Member 3: done; bins are re-sorted only within a temperature zone, and there is no affinity rule |
| Store edge node | Not built; design note and an outage experiment in [`EDGE_NODE.md`](EDGE_NODE.md) | Member 4 (design only) |
| Monitoring and KPIs | Prometheus metrics at `GET /metrics`, request ids and JSON logs (section 9); Prometheus with the alert rules and a Grafana dashboard in Docker Compose (`--profile monitoring`); no Alertmanager | Members 2, 3 and 4 |

## 9. Monitoring

`GET /metrics` is a Prometheus scrape target (`app/metrics.py` defines the metrics, `app/monitoring.py`
serves them):

| Metric | Type | Labels | Where it is counted |
| --- | --- | --- | --- |
| `http_request_duration_seconds` | histogram | method, route template, status | middleware, every request |
| `reservations_total` | counter | result: `held`, `insufficient_stock` | `inventory.reserve` |
| `events_published_total` | counter | event type | `events.publish` |
| `scans_total` | counter | result: `match`, `mismatch` | `pickpack.scan` |
| `atp_cache_lookups_total` | counter | result: `hit`, `miss` | `inventory.availability`, whole-store list only |
| `holds_in_progress` | gauge | | read from the database at scrape time |
| `outbox_lag_seconds` | gauge | | read at scrape time; same number as the dashboard tile |
| `store_seconds_since_last_event` | gauge | store id | read at scrape time |

The latency histogram is labelled with the route template (`/v1/stores/{store_id}/availability`), not the
raw path, so the number of series does not grow with the number of stores; an HTTP method outside the usual
seven is labelled `other`. The counters go up when the code runs, before the transaction commits, so a
rolled-back request still counts: fine for rates and alerts, not for exact totals. Set `METRICS_TOKEN` and
`/metrics` answers only `Authorization: Bearer <METRICS_TOKEN>`; with it unset (local development) it is open.

Every request gets an id: the caller's `X-Request-ID` if it is at most 64 letters, digits, `.`, `_` or `-`,
otherwise a new one. It is returned in the `X-Request-ID` response header and written on every log line
made while the request is handled. The app logs one JSON object per line to stderr, including one line per
request with its method, route, status and time in milliseconds.

The same middleware keeps the audit log. `auth.current_staff` puts the decoded token on the request; after
the response, if one is there, the middleware writes an `audit_entries` row: who (username, role, store),
what (method, route template, path with query string), the status they got and the request id. Refusals are
kept too, so a picker trying another store shows up as a 403. Calls with no valid token have nobody to name
and are only in the request log. The row is written in its own session after the handler has finished, so
it records what the caller was told, and a failed write is logged without failing the call. WebSocket
connections do not pass through the HTTP middleware and are not audited.

### Alert rules (HLD 12)

| Alert | Metric | Fires when (PromQL) |
| --- | --- | --- |
| Spike in failed reservations | `reservations_total` | more than 20% of holds fail over 5 minutes: `sum(rate(reservations_total{result="insufficient_stock"}[5m])) / sum(rate(reservations_total[5m])) > 0.2` |
| Event lag | `outbox_lag_seconds` | above 30 s for 1 minute: `outbox_lag_seconds > 30` |
| Slow reads | `http_request_duration_seconds` | read p99 above the 100 ms target: `histogram_quantile(0.99, sum by (le, route) (rate(http_request_duration_seconds_bucket{method="GET"}[5m]))) > 0.1` |
| Cache and database disagree | Member 1's stock audit (task 1.8) | any mismatch; not wired up until the audit exists |
| Store offline | `store_seconds_since_last_event` | above 15 minutes during opening hours: `store_seconds_since_last_event > 900` |

The thresholds (20%, 15 minutes) are first guesses to tune with real traffic. The rules are in
`monitoring/alerts.yml`, and Prometheus evaluates them when started with `docker compose --profile monitoring up`;
without an Alertmanager they show on Prometheus's *Alerts* page and nowhere else. "Store offline" ignores opening
hours, and the cache rule waits for Member 1's audit.

Grafana, in the same profile, opens on a *Dark store* dashboard (`monitoring/grafana/dashboards/darkstore.json`):
requests per second and read p99 per route against the 100 ms target, checkout holds by result, 429s and 5xx,
events by type, scans by result, outbox lag and each store's silence. A screenshot taken during a load test is in
`docs/img/grafana-dashboard.png`; in that run every read stayed under 50 ms except the picker's
`GET /v1/pickers/{picker_id}/next-task`, at a p99 of 250 to 500 ms. That call claims a job, so it writes, and
probably waits on SQLite's write lock behind checkout.
