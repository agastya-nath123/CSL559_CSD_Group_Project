# Member 1 guide: Inventory and stock integrity

**Your goal:** stock is never wrong and never oversold.
**You own:** `app/modules/inventory/`, the stock tables in `app/models.py`, and `app/db.py`.
**HLD sections to know well:** 5 (service 3), 7 Data design, 9 Scalability and consistency.

Everything else in the system trusts your module. If Inventory says 3 units are available, the
customer app shows 3, the picker expects to find 3, and replenishment orders based on 3.

---

## 1. Your part in one minute

Inventory answers two questions and guards one rule.

- *How many can we sell?* That is ATP (available to promise) = on hand − held.
- *Where exactly are they?* That is the bin and batch detail used for picking.
- *The rule:* a unit can only be promised to one customer. Checkout **holds** units; packing
  **commits** the sale; anything in between only has to release a hold.

## 2. Tour of your code

Open `app/modules/inventory/service.py` and read it top to bottom once (about 15 minutes).

| Function | Called by | What it does | Commits? |
| --- | --- | --- | --- |
| `availability` | `GET /v1/stores/{id}/availability` | ATP per SKU | read only |
| `reserve` | `POST /v1/reservations` | Holds a whole cart with one conditional `UPDATE` per line | yes |
| `confirm` | `POST /v1/reservations/{id}/confirm` | Payment done; publishes `order.paid` | yes |
| `release` | `DELETE /v1/reservations/{id}` | Gives held stock back | yes |
| `sweep_expired` | `POST /v1/admin/sweep-reservations` | Releases holds past their time | yes |
| `receive` | Inbound (Member 3) | Adds units to a bin; they are sellable at once | no |
| `adjust` | `POST /v1/stock-adjustments` | Damage, expiry, theft, count corrections | yes |
| `commit_sale` | Pick & Pack (Member 2) | Removes picked units and ends their hold | no |
| `write_off_missing` | Pick & Pack (Member 2) | Corrects the count when a shelf is empty | no |
| `write_off_unpromised` | Pick & Pack (Member 2) | Then writes off the rest of that bin no unpacked order is waiting for (issue #61) | no |
| `unreserve` | Members 2 and 1 | Gives a quantity back to ATP | no |
| `allocate` | Fulfilment (Member 2) | Chooses bins: first expiry first, then nearest | read only |
| `swap_line` | Pick & Pack (Member 2, task 2.4) | Holds a substitute and rewrites the order's lines | no |
| `move` | Slotting (Member 3, task 3.6) | Moves units of a batch to another bin; two `move` ledger rows | no |
| `audit` | `GET /v1/admin/stock-audit`, the tests | Lists every store and SKU whose level, bins and ledger disagree | read only |

### Try it by hand

Start the app, then in another terminal:

```bash
# What can store 1 sell? (SKU 14 is ice cream: exactly 1 in stock)
curl "localhost:8000/v1/stores/1/availability?skus=14,7"

# Hold 1 ice cream and 2 rice
curl -X POST localhost:8000/v1/reservations -H "Content-Type: application/json" \
  -d '{"order_id":"demo-1","store_id":1,"lines":[{"sku_id":14,"qty":1},{"sku_id":7,"qty":2}]}'

# A second customer wants the same ice cream: 409 insufficient_stock, and their rice is NOT held
curl -X POST localhost:8000/v1/reservations -H "Content-Type: application/json" \
  -d '{"order_id":"demo-2","store_id":1,"lines":[{"sku_id":14,"qty":1},{"sku_id":7,"qty":1}]}'

# Release the first hold (use the reservation_id from the first response)
curl -X DELETE localhost:8000/v1/reservations/<reservation_id>
```

Then read `tests/test_inventory.py`. Each test is one behaviour you must never break.

## 3. Ideas you need

**Hold, then commit.** `qty_reserved` goes up at checkout. `qty_on_hand` only goes down at packing.
A failed payment therefore never has to "add stock back"; it only lowers `qty_reserved`.

**The conditional UPDATE.** The check and the change happen in one SQL statement, so the database
itself makes sure two requests cannot both pass the check:

```sql
UPDATE stock_levels
SET qty_reserved = qty_reserved + :qty
WHERE store_id = :store AND sku_id = :sku
  AND qty_on_hand - qty_reserved >= :qty;      -- 0 rows changed = not enough stock
```

If you ever write `level = read(); if level.atp >= qty: level.reserved += qty`, you have created
an overselling bug. `test_last_unit_goes_to_exactly_one_of_twenty_buyers` exists to catch it.

**Three records that must agree.** `StockLevel.qty_on_hand`, the sum of `StockItem.qty`, and the
sum of `StockLedger.delta` must be equal for every store and SKU. `assert_stock_is_consistent()`
checks it. Every function that changes one must change all three.

**The ledger is append-only.** Never update or delete a ledger row. To undo something, write a new
row with the opposite sign.

---

## 4. Your tasks

### Milestone 1

#### Task 1.1 (warm-up): Safety buffer in ATP

*Why:* HLD 7.1 defines ATP = on hand − reserved − **safety buffer**. The buffer hides a few units
from customers so that small counting errors do not become failed orders. The prototype has no buffer.

*Steps*

1. In `app/models.py`, add to `StockLevel`:
   ```python
   safety_buffer: Mapped[int] = mapped_column(Integer, default=0)
   ```
   Delete `darkstore.db` so the table is recreated.
2. In `availability`, compute `atp = max(0, on_hand - reserved - safety_buffer)` and return
   `safety_buffer` in each row.
3. In `reserve`, change the `WHERE` condition to
   `StockLevel.qty_on_hand - StockLevel.qty_reserved - StockLevel.safety_buffer >= qty`,
   and use the same formula for `available` in the error details.
4. Search the whole project for `qty_on_hand - ` to find other ATP calculations
   (`replenishment/service.py`, `dashboard/service.py`). Decide each one:
   - Dashboard "out of stock" count: **include** the buffer (it shows what customers see).
   - Replenishment stock position: **do not** subtract the buffer (those units are physically there).
     Tell Member 3 what you decided.
5. Add `PUT /v1/stores/{store_id}/stock-settings/{sku_id}` with body `{"safety_buffer": 2}`.
6. In `app/seed.py`, give milk in store 1 a buffer of 2.

*Done when*

- With 30 milk on hand and a buffer of 2, availability shows ATP 28.
- Holding 29 fails; holding 28 succeeds.
- All existing tests pass (update the two that assume milk ATP is 30).

*Watch out:* do not touch `pickpack.report_short`. When a shelf is short, buffer units are exactly
what should cover the line.

#### Task 1.2: Automatic hold sweeper, and race-proof releases

*Why:* HLD 9.1 says "a sweeper job releases expired holds". Today someone has to press a button.

*Steps*

1. Add to `app/config.py`:
   ```python
   SWEEP_INTERVAL_SECONDS = int(os.getenv("SWEEP_INTERVAL_SECONDS", "30"))
   BACKGROUND_JOBS = os.getenv("BACKGROUND_JOBS", "on") == "on"
   ```
2. Create `app/background.py`:
   ```python
   import asyncio
   import logging

   from . import config
   from .db import SessionLocal
   from .modules.inventory import service as inventory

   log = logging.getLogger("darkstore.background")

   def sweep_once() -> int:
       with SessionLocal() as db:
           return inventory.sweep_expired(db)

   async def sweep_loop():
       while True:
           await asyncio.sleep(config.SWEEP_INTERVAL_SECONDS)
           try:
               released = await asyncio.to_thread(sweep_once)   # database calls block; keep them off the event loop
               if released:
                   log.info("released %s expired holds", released)
           except Exception:
               log.exception("sweep failed; will retry")
   ```
3. In `app/main.py`, inside `lifespan`, start the loop after seeding and cancel it on shutdown:
   ```python
   task = asyncio.create_task(sweep_loop()) if config.BACKGROUND_JOBS else None
   yield
   if task:
       task.cancel()
   ```
4. In `tests/conftest.py`, set `os.environ["BACKGROUND_JOBS"] = "off"` next to the database line, so
   tests stay predictable.
5. **Make releasing safe when two things happen at once.** Today `_release` reads the status, then
   changes it. If the sweeper and a customer's *Cancel* (or two app instances' sweepers) hit the
   same hold together, the stock is given back twice. Fix it with the same trick as `reserve`:
   ```python
   changed = db.execute(
       update(Reservation)
       .where(Reservation.id == res.id, Reservation.status == "held")
       .values(status=new_status)
   ).rowcount
   if changed == 0:
       return          # someone else already released or confirmed it
   # only now give the stock back
   ```
   Do the same in `confirm` (`held -> confirmed`), so a payment that lands at the same instant as
   the expiry either wins cleanly or loses cleanly.

*Done when*

- Run with `RESERVATION_TTL_MINUTES=1 SWEEP_INTERVAL_SECONDS=10`, hold an item, wait: the "Held"
  column returns to 0 on its own.
- New test: 10 threads call `release` on the same hold; ATP goes back up exactly once.
- New test: `sweep_once()` releases a back-dated hold.

### Milestone 2

#### Task 1.3: Expiry handling

*Why:* FR-10: "Track batch and expiry dates; enforce FEFO and flag items near expiry". The prototype
picks oldest first but will happily pick, and sell, a batch that expired yesterday.

*Steps*

1. **Never allocate expired stock.** In `allocate`, add `StockItem.expiry_date >= date.today()` to
   the query.
2. **Write expired stock off.** Add `expire_stock(db, store_id=None) -> int`:
   - find `StockItem` rows with `expiry_date < today` and `qty > 0`;
   - for each, remove all units from that exact item, lower `qty_on_hand`, write a ledger row with
     reason `expiry`, and publish `stock.changed` with cause `expiry`;
   - commit once at the end and return the number of units written off.

   The existing `adjust` removes "earliest expiry first in a bin", which is nearly what you need.
   Pull the shared part into a private helper, for example
   `_remove_from_item(db, item, qty, reason, reference, actor)`, and use it in both.
3. Add `POST /v1/admin/expire-stock`, and call `expire_stock` from the background loop once an hour.
4. **Near-expiry report.** Add `GET /v1/stores/{store_id}/near-expiry?days=2`, returning batches
   that expire within `days` (bin, batch, quantity, days left). From the hourly job, publish
   `item.near_expiry` (HLD 8.2) for each such batch, at most once per batch per day. A simple way:
   check `event_log` for today's event with the same `stock_item_id` before publishing.

*Done when*

- Test: back-date batch `M-OLD` to yesterday, order milk: the pick tasks use only `M-NEW`.
- Test: `expire_stock` removes the 6 expired units, and the ledger has a `-6 expiry` row.
- Test: the near-expiry report lists `M-OLD` (it expires tomorrow in the demo data).

*Watch out:* if expired units were on hold for a paid order, the write-off can leave
`qty_reserved > qty_on_hand` for a moment. Do not try to cancel the order from here. When the picker
reaches that line, allocation finds nothing and the existing "dropped line" path releases the hold.
Note this as a trade-off in the report, and relax the `reserved <= on_hand` check only inside the
one test that covers it.

#### Task 1.4: Idempotency keys

*Why:* HLD 8: "Every write API takes an `Idempotency-Key` header, so a retried request never
reserves or moves stock twice." Reservations are already safe (the `order_id` is the key); stock
adjustments and goods receipts are not. A manager who double-clicks "write off 4 damaged" loses 8.

*Steps*

1. Add a table:
   ```python
   class IdempotencyRecord(Base):
       __tablename__ = "idempotency_records"
       scope: Mapped[str] = mapped_column(String(40), primary_key=True)    # e.g. "stock-adjustment"
       key: Mapped[str] = mapped_column(String(80), primary_key=True)
       response: Mapped[dict] = mapped_column(JSON)
       created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
   ```
2. Create `app/idempotency.py`:
   ```python
   def run_once(db, scope: str, key: str | None, action):
       """Run `action()` once per (scope, key). A repeat returns the first result."""
       if key is None:
           return action()
       seen = db.get(IdempotencyRecord, (scope, key))
       if seen is not None:
           return seen.response
       result = action()
       db.add(IdempotencyRecord(scope=scope, key=key, response=result))
       db.commit()
       return result
   ```
3. In the router, read the header and wrap the call:
   ```python
   from fastapi import Header

   @router.post("/v1/stock-adjustments")
   def create_adjustment(body: AdjustmentIn, idempotency_key: str | None = Header(None),
                         db: Session = Depends(get_db)):
       return run_once(db, "stock-adjustment", idempotency_key,
                       lambda: service.adjust(db, body.store_id, body.sku_id, body.delta, body.reason,
                                              body.bin_id, body.actor))
   ```
4. Ask Member 3 to wrap `POST /v1/goods-receipts` the same way (or do it together).

*Done when*

- Test: the same adjustment posted twice with the same key changes stock once and returns the same body.
- Test: the same adjustment with two different keys changes stock twice.

*Watch out:* the record is saved in a second commit after the action. If the process dies between
the two, a retry repeats the action. Closing that gap means saving the record in the *same*
transaction as the stock change; describe this limit in the report, or fix it by giving `adjust`
a `commit=False` option.

### Milestone 3

#### Task 1.5: Cycle counts with manager approval

*Why:* FR-11 and HLD 10: "adjustments above a set value need manager approval". Also the source of
the "inventory accuracy ≥ 99.5%" KPI in HLD 12.

*What to build*

A blind count: the counter is told which bin to count but **not** what the system expects.

| Step | Endpoint | What happens |
| --- | --- | --- |
| Start | `POST /v1/cycle-counts` `{store_id, bin_id}` | Creates a count with status `open`; returns the SKUs to look for, without quantities |
| Submit | `POST /v1/cycle-counts/{id}/submit` `{lines: [{sku_id, counted_qty}]}` | Compares with the system quantity **at this moment**; stores both |
| Small difference | (automatic) | If the value of the differences is at most `COUNT_AUTO_APPROVE_VALUE`, apply them with `adjust(..., reason="count")`; status `approved` |
| Large difference | `POST /v1/cycle-counts/{id}/approve` or `/reject` | Waits as `submitted` until a manager decides |
| KPI | `GET /v1/stores/{id}/inventory-accuracy` | Share of counted lines with no difference, over the last 30 days |

*Steps*

1. Add `CycleCount` (id, store_id, bin_id, status, created_by, created_at, decided_by) and
   `CycleCountLine` (count_id, sku_id, system_qty, counted_qty) to `models.py`.
2. Put the logic in a new file `app/modules/inventory/counts.py` so `service.py` stays readable.
3. Value of a difference = `abs(counted - system) * sku.price`. Add `COUNT_AUTO_APPROVE_VALUE` to
   `config.py` (try 500).
4. Apply differences with the existing `adjust(db, store, sku, delta, "count", bin_id=...)`.
5. After Member 4 finishes task 4.2, protect `approve` and `reject` with `require_role("manager")`.
6. **Count a bin when a picker finds it short.** `pickpack.report_short` writes off the bin's units
   except those other orders are still waiting for, but only a count shows what is really there.
   Subscribe to `stock.changed` with cause `not_found` and open a cycle count for that bin
   (the event payload has the SKU; add the `bin_id` to it with Member 2).

*Done when*

- Test: counting 28 rice where the system says 30 (value 280) is applied automatically; ATP drops by 2.
- Test: counting 10 where the system says 30 waits for approval; stock is unchanged until approved.
- Test: the start response contains no quantities.

*Watch out:* units already promised to a pending pick task are still physically in the bin, so they
should be counted. Do not subtract them.

#### Task 1.6: ATP cache

*Why:* HLD 3 estimates about 10,000 stock reads per second at peak, and HLD 9.2 answers with
"ATP served from Redis". Reads outnumber writes roughly 13 to 1.

*Steps*

1. Create `app/cache.py` with a tiny interface so the storage can be swapped later:
   ```python
   import time

   class TTLCache:
       def __init__(self, ttl_seconds: float):
           self.ttl, self._data = ttl_seconds, {}
       def get(self, key):
           hit = self._data.get(key)
           return hit[1] if hit and hit[0] > time.monotonic() else None
       def set(self, key, value):
           self._data[key] = (time.monotonic() + self.ttl, value)
       def delete(self, key):
           self._data.pop(key, None)

   atp_cache = TTLCache(ttl_seconds=2)
   ```
2. In `availability`, when all SKUs are requested, try `atp_cache.get(store_id)` first; on a miss,
   query and `set`.
3. Subscribe to `stock.changed` and `delete` that store's entry.
4. **Never** read the cache inside `reserve`. The hold must always check the database.
5. Write `scripts/bench_availability.py`: fire 2,000 availability requests with `httpx` and print
   the median and 99th-percentile time. Run it with the cache off and on; put both in the report.
6. *Optional:* add Redis to Member 4's Docker Compose file and write a `RedisCache` class with the
   same three methods.

*Done when*

- Test: after a hold, availability shows the new ATP (the event cleared the cache).
- Test: with the cache deliberately stale, a hold for more than the real ATP still fails.
- The benchmark numbers are in the report.

*Watch out:* the event handler runs *before* the transaction commits. Another request can refill
the cache with the old value in that gap. That is why the entries also expire after 2 seconds, and
why a stale cache "can only cause a failed reservation, never an oversell" (HLD 9.2). Explain this
in your own words in the report; it is a good exam question.

### Milestone 4

#### Task 1.7: PostgreSQL and migrations

*Why:* HLD 7.2 chooses PostgreSQL because "ACID transactions and row locks prevent overselling".
Showing the race test pass on real PostgreSQL is strong evidence for the report.

*Steps*

1. With Member 4 (task 4.1), add a `postgres` service to `docker-compose.yml`.
2. Add `psycopg[binary]` and `alembic` to `requirements.txt`.
3. Run the app against it:
   ```bash
   export DARKSTORE_DB="postgresql+psycopg://darkstore:darkstore@localhost:5432/darkstore"
   python -m uvicorn app.main:app
   ```
4. Let tests choose the database: in `tests/conftest.py`, only set `DARKSTORE_DB` to the temporary
   SQLite file when `DARKSTORE_TEST_DB` is not set; otherwise use that value.
5. Run the whole suite on PostgreSQL and fix anything SQLite was forgiving about.
6. Set up migrations: `alembic init migrations`, point `target_metadata` at `Base.metadata` in
   `migrations/env.py`, then `alembic revision --autogenerate -m "initial tables"` and
   `alembic upgrade head`. Update the "When you change the database tables" section of
   `CONTRIBUTING.md` to the new two-command routine.

*Done when*

- `DARKSTORE_TEST_DB=postgresql+psycopg://... python -m pytest -q` passes, including the race test.
- A teammate can add a column with one `alembic revision --autogenerate` and one `alembic upgrade head`.

*For the report:* under PostgreSQL's default isolation level, the second of two competing `UPDATE`s
waits for the first to commit, then **re-checks its `WHERE` clause** against the new row. That
re-check is what makes the pattern safe. Sharding by `store_id` (HLD 7) is not built; explain that
every query already filters by `store_id`, which is what makes it possible later.

#### Task 1.8: Stock audit endpoint and load test

*Why:* to prove, with numbers, that the main promise of the design holds.

*Steps*

1. Move the checks from `assert_stock_is_consistent` into `inventory.audit(db) -> list[dict]`, which
   returns one entry per mismatch (store, SKU, which records disagree, the values). Expose it as
   `GET /v1/admin/stock-audit`. Make the test helper call `audit` and assert the list is empty.
2. Write `scripts/load_checkout.py` (with Member 4):
   - reset the demo data and set one SKU to exactly 50 units;
   - start 200 threads, each sending one `POST /v1/reservations` for 1 unit;
   - assert exactly 50 succeed and 150 get `409`;
   - print median and 99th-percentile response times;
   - call the audit endpoint and assert it is empty.
3. Run it against SQLite and PostgreSQL. Put the table of results in the report under
   "Scalability, availability and consistency".

*Done when*

- The script exits with success on both databases and the audit is clean afterwards.

---

## 5. What others need from you

| Who | Needs | When |
| --- | --- | --- |
| Member 2 | `allocate`, `commit_sale`, `unreserve`, `write_off_missing` to keep their meaning | Always. Tell them before changing a signature |
| Member 2 | A new `swap_line(db, reservation, old_sku_id, new_sku_id, qty)` for substitutes (their task 2.4): hold the new SKU with the conditional `UPDATE`, release the old line, update the `ReservationLine`, all in one transaction | Milestone 2 |
| Member 3 | `receive` to stay non-committing, and to keep rejecting the wrong temperature zone | Always |
| Member 3 | A new `move(db, stock_item, to_bin_id, qty)` for re-slotting (their task 3.6): two ledger rows with reason `move` (out of one bin, into the other), `qty_on_hand` unchanged, refused if the target bin is in the wrong temperature zone | Milestone 3 |
| Member 4 | An optional `h3_cell` on the reservation request and table (their task 4.4) | Milestone 2 |
| Member 3 | Your decision from task 1.1 on whether replenishment subtracts the safety buffer | Milestone 1 |
| Member 4 | The audit function (1.8) and inventory-accuracy KPI (1.5) for the dashboard | Milestones 3 and 4 |

## 6. What to show at the final demo

1. The "last ice cream" race in two browser tabs: one customer wins, the other is told at once.
2. A hold expiring by itself and the stock returning.
3. The load test: 200 buyers, 50 units, exactly 50 holds, clean audit.
4. One sentence each on *hold then commit* and *the conditional UPDATE*.

## 7. If you have time left

- Use the `version` column for optimistic locking on `adjust`, and explain when you would choose
  optimistic over pessimistic locking.
- Simulate sharding: route each `store_id` to one of two SQLite files and show that a reservation
  never needs both.
- Add a `GET /v1/stores/{id}/ledger` filter by SKU and date, and a CSV export for audits.
