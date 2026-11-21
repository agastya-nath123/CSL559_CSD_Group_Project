# Member 3 guide: Supply

**Your goal:** the shelf is full before the customer asks.
**You own:** `app/modules/forecasting/`, `replenishment/`, `inbound/`, `slotting/`, `assortment/`,
the transfer-order and forecast tables in `app/models.py`, and `app/seed.py`.
**HLD sections to know well:** 5 (services 2, 6, 7, 8, 9), 6.2 Replenishment, 6.3 Inbound, 12 KPIs.

Members 1 and 2 make sure an order for something *in stock* goes well. You decide whether it is in
stock in the first place. A dark store has room for only about 5,000 SKUs, so every unit of shelf
space you fill with the wrong thing is a sale lost on the right thing.

---

## 1. Your part in one minute

Your modules form a loop:

```
   sales (stock ledger)
        |
        v
   FORECASTING  -- daily demand, reorder point, target stock -->  REPLENISHMENT
        ^                                                              |
        |                                                   transfer order to the hub
        |                                                              v
   customers buy  <--  stock on shelf  <--  SLOTTING picks the bin  <--  INBOUND receives the truck
```

ASSORTMENT sits above the loop: it decides which SKUs the store carries at all.

## 2. Tour of your code

Each file is short. Read all five `service.py` files (about 25 minutes).

| Function | Called by | What it does |
| --- | --- | --- |
| `forecasting.daily_sales` | `run` | Units sold per SKU per day, read from `sale` rows in the ledger |
| `forecasting.run` | `POST /v1/forecasting/run` | Moving average and standard deviation, then sets `reorder_point` and `target_stock` |
| `replenishment.on_stock_changed` | `stock.changed` event | Publishes `stock.low` when ATP is under the reorder point, unless reordering is stopped |
| `replenishment.in_transit` | `run` | Units ordered from the hub but not yet received |
| `replenishment.run` | `POST /v1/replenishment/run` | One transfer order for everything whose stock position is low |
| `replenishment.set_reorder_stopped` | `PUT /v1/stores/{id}/reorder-settings/{sku_id}` | Stops or restarts reordering one SKU, e.g. to let it run down before dropping it |
| `replenishment.dispatch` | `POST /v1/transfer-orders/{id}/dispatch` | Pretends the hub sent the truck |
| `inbound.open_receipt`, `scan` | `POST /v1/goods-receipts/open`, `/{id}/scans` | Saves a receipt; scanned units (batch and expiry required) wait on the dock. Damaged units and batches with under half their shelf life left are recorded, never stocked |
| `inbound.putaway_tasks`, `put_away` | `GET /v1/goods-receipts/{id}/putaway-tasks`, `POST /{id}/putaway` | Bins with room for what is on the dock; a line put away is sellable at once |
| `inbound.close_receipt` | `POST /v1/goods-receipts/{id}/close` | Marks the transfer order received and reports ordered versus received for every SKU; `send_back=true` returns what is left on the dock. Refused for a receipt with nothing scanned (`409 nothing_scanned`) |
| `inbound.cancel_receipt` | `POST /v1/goods-receipts/{id}/cancel` | Drops a receipt opened or scanned by mistake, before anything is put away; the transfer order stays on its way |
| `inbound.receive_goods` | `POST /v1/goods-receipts` | The one-call receipt: runs the five steps in order |
| `slotting.velocity`, `plan`, `apply` | `GET /v1/slotting/plan`, `POST /v1/slotting/apply` | Ranks each zone's SKUs by units sold a day over 14 days and gives the fastest the bins nearest the packing desk; `apply` moves the batches with `inventory.move`, never past a bin's capacity, and skips batches promised to a picker |
| `slotting.suggest_bin`, `slotting.place` | Inbound | The SKU's planned home bin first, then its own bins, then the emptiest bin with room in the right temperature zone; `place` splits a delivery over bins, never past a bin's capacity |
| `assortment.add_sku` | `POST /v1/stores/{id}/assortment` | Starts carrying a SKU (a zero-stock `StockLevel` row) |

### Try it by hand

```bash
# Bread in store 1 starts at 4 units, below its reorder point of 10
curl -X POST "localhost:8000/v1/replenishment/run?store_id=1"        # raises a transfer order
curl -X POST "localhost:8000/v1/replenishment/run?store_id=1"        # null: stock in transit counts
curl -X POST localhost:8000/v1/transfer-orders/1/dispatch

# Receive it, 6 loaves short: 40 bread and 42 ice cream were ordered, in whole cases
# (SKU 4 = bread, 14 = ice cream; use the quantities from the first response)
curl -X POST localhost:8000/v1/goods-receipts -H "Content-Type: application/json" \
  -d '{"store_id":1,"transfer_order_id":1,"lines":[{"sku_id":4,"qty":34},{"sku_id":14,"qty":42}]}'

# Recalculate forecasts from the seeded week of sales
curl -X POST "localhost:8000/v1/forecasting/run?store_id=1"
```

Then read `tests/test_supply.py`.

## 3. Ideas you need

**Stock position, not stock on hand.** Replenishment looks at
`position = on hand − reserved + in transit`. Without "in transit", every run would re-order the
same items until the truck arrived.

**The reorder-point formula (HLD 6.2), with numbers.** Milk sells 20 a day on average, with a
standard deviation of 4. The truck takes 1 day. We want to run out at most 5% of the time, so z = 1.65.

```
reorder point = mean × lead time + z × std × sqrt(lead time)
              = 20 × 1 + 1.65 × 4 × 1  =  26.6  ->  27
target stock  = reorder point + mean × cover days  =  27 + 20 × 3  =  87
```

When the position falls below 27, order `87 − position`. The first part (20) covers expected sales
while the truck is on its way; the second part (7) is **safety stock** for a busier-than-usual day.

**WAPE: how wrong is a forecast?** Weighted absolute percentage error:

```
WAPE = sum of |actual − forecast|  ÷  sum of actual
```

Forecast 20, sold 25: error 5. Forecast 10, sold 8: error 2. WAPE = 7 ÷ 33 = 21%. The HLD target
is 30% or lower at store-SKU-day level. It is preferred over a plain average of percentages
because slow sellers (forecast 1, sold 2 = "100% wrong") do not swamp the result.

**You do not write stock quantities.** Inbound calls `inventory.receive`. Forecasting does write
`reorder_point` and `target_stock`, which are planning settings, not stock, so that is allowed.

**The ledger must always add up.** If you insert historical `sale` rows for testing, you must also
insert a matching earlier `receipt`, or `assert_stock_is_consistent()` fails. `app/seed.py` shows
how: the history gets a `receipt` row of its own for everything it "sold", tagged `reference="history"`
like the sales, so the history rows always add up to zero.

---

## 4. Your tasks

### Milestone 1

#### Task 3.1 (warm-up): Sales history generator

*Why:* the demo data has 7 days of sales for 4 SKUs. You cannot build or judge a forecast on that.

*Steps*

1. Create `scripts/generate_sales.py` with a function `generate(store_id, days=60, seed=42)`.
2. For each SKU the store carries, for each of the last `days` days (not today), draw units sold:
   ```python
   base = BASE_DEMAND.get(sku.category, 3)            # e.g. Dairy 20, Bakery 12, Staples 4, Frozen 2
   weekday_factor = 1.35 if day.weekday() >= 5 else 1.0      # busier weekends
   festival_factor = 2.0 if day in FESTIVAL_DAYS else 1.0    # pick two dates in the window
   units = rng.poisson(base * weekday_factor * festival_factor)    # or random.gauss, floored at 0
   ```
   Use `random.Random(seed)` so everyone on the team generates the same history.
3. Give each sale a time of day, weighted towards 7–9 pm (HLD 2.2 mentions evening peaks). You will
   use the hour later.
4. Write one `StockLedger` row per SKU per day with `reason="sale"`, a negative `delta`,
   `reference="history"`, `actor="generator"`.
5. **Keep the ledger adding up:** for each SKU, also write one `receipt` row dated before the first
   day, with `delta = total units generated`. (Read section 3, last paragraph.)
6. Delete any earlier `reference="history"` rows first, so the script can be re-run.

*Done when*

- `python scripts/generate_sales.py` runs twice in a row without error.
- `python -m pytest -q` still passes afterwards (the script does not touch the test database).
- A quick query shows weekend days selling more than weekdays.
- New test: call `generate` on the test database, then `assert_stock_is_consistent()`.

#### Task 3.2: Forecast backtest

*Why:* HLD 12 sets "Forecast accuracy: WAPE ≤ 30%". You need the measuring stick before you
improve the model, otherwise you cannot tell whether a change helped.

*Steps*

1. **Fix a flaw first.** `daily_sales` includes *today*, which is only partly over, so it drags the
   average down. Change it to cover the `window_days` days ending **yesterday**. Update the
   forecast test if its numbers move.
2. Refactor `daily_sales(db, store_id, days_back)` to return `{sku_id: {date: units}}` so other
   code can ask for any range.
3. Add `forecasting.backtest(db, store_id, test_days=14, window=7) -> dict`:
   - for each of the last `test_days` days *d* and each SKU: the forecast for *d* is the moving
     average of the `window` days before *d*; the actual is what sold on *d*;
   - return overall WAPE, and WAPE per SKU.
4. Expose it as `GET /v1/forecasting/backtest?store_id=1`.

*Done when*

- Test with hand-made history (for example 10 units every day): WAPE is exactly 0.
- Test with history you can work out on paper: WAPE matches your arithmetic.
- You have written down the baseline WAPE on the generated data. This is the number to beat.

### Milestone 2

#### Task 3.3: A better forecast model

*Why:* the generated data has a weekend pattern. A plain moving average forecasts Saturday from
mostly weekdays, so it is always too low on the days that matter most.

*Steps*

1. Give every model the same shape, so they can be swapped and compared:
   ```python
   # history: {date: units} for one SKU; returns the forecast for target_day
   def moving_average(history, target_day, window=7): ...
   def weekday_average(history, target_day, weeks=4): ...     # mean of the last 4 same weekdays
   def exp_smoothing(history, target_day, alpha=0.3): ...     # recent days count more

   MODELS = {"moving_average": moving_average, "weekday_average": weekday_average,
             "exp_smoothing": exp_smoothing}
   ```
2. Let `backtest` take a `model` name, and add `compare_models(db, store_id)` returning the WAPE of each.
3. In `run`, use `config.FORECAST_MODEL` (default: whichever wins your comparison) and store its
   name in `Forecast.model`.
4. The reorder-point formula needs a standard deviation. Use the standard deviation of the model's
   *errors* in the backtest rather than of raw sales: safety stock should cover what the forecast
   gets wrong, not variation it already predicts.
5. Put the comparison table in the report.

*Done when*

- Test: on data with a strong weekend effect, `weekday_average` has a lower WAPE than `moving_average`.
- `GET /v1/forecasts` shows the model name.

*If you want more:* (a) **Hourly forecasts**, as the HLD specifies: work out the share of a day's
sales that falls in each hour, and multiply the daily forecast by that curve. (b) A gradient-boosted
model (LightGBM) with features such as weekday, festival flag and last week's sales. Only do (b)
if (a) and the three simple models are finished; a simple model you can explain beats a complex
one you cannot.

#### Task 3.4: Smarter replenishment

*Why:* three gaps against HLD 6.2: it only runs when someone presses a button, it ignores shelf
space ("capped by free bin space"), and it orders odd quantities a hub would never ship.

*Steps*

1. **Run it automatically.** Add `run_all(db)` that calls `run` for every open store. Ask Member 1
   to call it from the background loop every `REPLENISHMENT_INTERVAL_MINUTES`. Because stock in
   transit counts, running often is harmless.
2. **Case sizes.** Add `case_size` to `SKU` (default 1; set 12 for milk, 6 for eggs and so on in
   the seed). Round each order quantity **up** to a full case.
3. **Bin capacity.** Add `capacity` (units) to `Bin`; ask Member 1 to review the model change.
   Free space in a zone = total capacity − units stored − units in transit for that zone.
   When the lines for a zone do not fit:
   - sort lines by urgency, lowest *days of cover* first (`ATP ÷ daily demand`);
   - fill in that order, cutting the last line that fits partly;
   - return what was cut in the response (`"capped": [...]`) so a manager can see it.
4. Make `inbound` refuse a putaway into a bin that would go over capacity (`409 bin_full`).

*Done when*

- Test: an order of 37 milk with a case size of 12 becomes 48.
- Test: with a small chilled zone, the most urgent SKU is ordered in full and a less urgent one is cut.
- Test: after `run_all`, a second `run_all` creates nothing.

### Milestone 3

#### Task 3.5: Two-step receiving with saved goods receipts

*Why:* HLD 6.3 describes five steps; the prototype squashes them into one call and keeps no record
of the receipt itself. Step 5 is the interesting one: "Each item becomes sellable as soon as its
bin scan is confirmed, not when the whole truck is done."

*What to build*

| Step | Endpoint | Effect on stock |
| --- | --- | --- |
| Open | `POST /v1/goods-receipts/open` `{store_id, transfer_order_id}` | None |
| Scan each item off the truck | `POST /v1/goods-receipts/{id}/scans` `{barcode, qty, batch_no, expiry_date, damaged}` | None. Good units wait on the dock; damaged units are recorded and never enter stock |
| Get putaway tasks | `GET /v1/goods-receipts/{id}/putaway-tasks` | None. One task per scanned line with the bin Slotting suggests |
| Put away | `POST /v1/goods-receipts/{id}/putaway` `{sku_id, batch_no, bin_code, qty}` | `inventory.receive`: **sellable now** |
| Close | `POST /v1/goods-receipts/{id}/close` | Marks the transfer order received; computes shortfalls; publishes `goods.received` |

*Steps*

1. Add `GoodsReceipt` (id, store_id, transfer_order_id, status, created_at, closed_at) and
   `GoodsReceiptLine` (receipt_id, sku_id, batch_no, expiry_date, qty_scanned, qty_damaged, qty_put_away).
2. Build the five functions in `inbound/service.py`.
3. **Shelf-life check on arrival:** refuse a batch that has less than half its shelf life left
   (`MIN_REMAINING_SHELF_LIFE = 0.5` in `config.py`). Record it on the receipt as rejected.
4. Keep the existing one-call `POST /v1/goods-receipts` working by making it call the five steps
   in order. The demo screen and current tests depend on it.
5. With Member 1, wrap the write endpoints in `run_once` (their task 1.4) so a retried scan does
   not count twice.

*Done when*

- Test: after scanning but before putaway, ATP is unchanged; after putting away one line, that SKU's ATP rises and the others do not.
- Test: 5 damaged units appear on the receipt and in the shortfall list, and never in stock.
- Test: a batch expiring tomorrow is refused.
- Test: closing reports ordered versus received for every line.

#### Task 3.6: Velocity-based slotting

*Why:* HLD service 6: "Places fast movers near the packing station". In the demo data, bins are
handed out in catalogue order, so milk can sit at the far end while soap is by the door.

*Needs from Member 1:* `inventory.move(db, stock_item, to_bin_id, qty)`: two ledger rows (out of
one bin, into the other, reason `move`), with `qty_on_hand` unchanged. Ask early in the milestone.

*Steps*

1. Add `slotting.velocity(db, store_id, days=14) -> {sku_id: units_per_day}` from the ledger.
2. Add `slotting.plan(db, store_id)`: within each temperature zone, sort SKUs by velocity
   (fastest first) and bins by `walk_seq` (nearest first), and pair them up. Return the list of
   moves needed: `[{sku, from_bin, to_bin, qty}]`.
3. Add `GET /v1/slotting/plan?store_id=` (look only) and `POST /v1/slotting/apply?store_id=`
   (do the moves through `inventory.move`).
4. Refuse to move a batch that has a pending pick task pointing at it; return it under `"skipped"`.
5. Change `suggest_bin` to prefer the SKU's planned home bin for new deliveries.
6. **Measure it** with Member 2's walking model (their task 2.6): average route length of 200
   random orders, weighted by velocity, before and after applying the plan.

*Done when*

- Test: after `apply`, the fastest ambient SKU is in the ambient bin with the lowest `walk_seq`.
- Test: chilled SKUs never leave chilled bins.
- Test: `assert_stock_is_consistent()` passes after `apply`.
- The before and after route lengths are in the report.

*If you want more:* **affinity.** Find pairs of SKUs often bought in the same order (ledger rows
share `reference="order:..."`) and place them in neighbouring bins.

### Milestone 4

#### Task 3.7: Assortment recommendations

*Why:* HLD service 2: "Decides which SKUs each store carries, based on local demand and shelf space".

*Steps*

1. Add `assortment.recommendations(db, store_id) -> dict` with three lists:

   | List | Rule |
   | --- | --- |
   | `add` | SKUs not carried here that sell in other stores, ranked by their daily demand there |
   | `drop` | SKUs carried here that had stock on hand 30 days ago and have not sold one unit since (dead stock taking space) |
   | `space` | Per zone: SKUs carried versus bins available, so nobody adds a 5th chilled SKU to 4 chilled bins |

2. Expose it as `GET /v1/stores/{store_id}/assortment/recommendations`.
3. Add `DELETE /v1/stores/{store_id}/assortment/{sku_id}`: allowed only when on hand, reserved and
   in transit are all zero; otherwise `409` with the reason.

*Done when*

- Test: bananas sell in store 1 and are absent from store 2's shelf: they appear in store 2's `add` list.
- Test: a SKU with stock cannot be dropped.

#### Task 3.8: Supply KPIs

*Why:* HLD 12 lists five KPIs that belong to you. Member 4 needs them for the dashboard.

*Steps*

1. Add `replenishment.kpis(db, store_id) -> dict`:

   | Key | How | HLD target |
   | --- | --- | --- |
   | `stock_out_rate` | Carried SKUs with ATP ≤ 0 ÷ carried SKUs | ≤ 2% |
   | `days_of_cover` | Per SKU: ATP ÷ forecast daily demand; also the count under 1 day and over 14 | 3–5 ambient, ≤ 2 fresh |
   | `wastage_rate` | Value written off (`expiry` + `damage`) ÷ value sold, last 30 days | ≤ 1% of sales |
   | `replenishment_on_time` | Transfer orders received within the lead time ÷ all received | ≥ 95% |
   | `forecast_wape` | From your backtest | ≤ 30% |

2. Expose it as `GET /v1/stores/{store_id}/supply-kpis` and tell Member 4 the response shape.
3. Write the "Monitoring and KPIs" evidence for your five rows: the measured value on generated
   data, whether it meets the target, and why or why not.

*Done when*

- Test: with one SKU at zero out of 16, `stock_out_rate` is 0.0625.
- Test: writing off 5 units through `POST /v1/stock-adjustments` raises `wastage_rate`.

---

## 5. What others need from you

| Who | Needs | When |
| --- | --- | --- |
| Member 1 | `suggest_bin` to keep returning a bin in the right temperature zone (their cycle counts use it) | Always |
| Member 1 | Review requests for any change to `Bin` or `SKU` (3.4) and a heads-up before you need `inventory.move` (3.6) | Milestones 2 and 3 |
| Member 2 | `Bin.walk_seq` to keep meaning "distance from the packing desk": their pick paths sort by it | Always |
| Member 4 | `kpis` (3.8) and the backtest result for the dashboard | Milestone 4 |
| Everyone | `app/seed.py` to keep the four planted demo situations listed at the top of that file | Always |

## 6. What to show at the final demo

1. Bread running low, a transfer order raised automatically, and a second check ordering nothing.
2. A truck received 6 short: the shortfall on screen, and the bread on sale the moment it is put away.
3. The model comparison table: baseline WAPE versus your best model.
4. The slotting plan: route length before and after.

## 7. If you have time left

- Forecast new stores with no history by copying a similar store's demand (HLD 13.2, first risk).
- Markdown suggestions: a discount percentage for batches near expiry, using Member 1's near-expiry report.
- Truck slots: limit each store to one delivery window per day and plan orders to fit it.
