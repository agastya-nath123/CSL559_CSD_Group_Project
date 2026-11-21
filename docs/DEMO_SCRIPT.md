# Demo script

A fifteen-minute walk through the prototype for a presentation. Each step says what to click and what
to say. Rehearse it twice; press **Reset demo data** on the ops desk before every run.

## Before you start

```bash
RATE_LIMIT_PER_SECOND=0 REPLENISHMENT_INTERVAL_MINUTES=0 python -m uvicorn app.main:app
```

The rate limit is off so the load test below is not refused. Automatic replenishment (every 15 minutes) is off
so the transfer order in section 7 comes from the button, not from the timer half-way through the demo.

Then, in a second terminal:

```bash
python scripts/load_test.py --seconds 30 --reset   # once, before the audience arrives; keep its table for section 12
python scripts/generate_sales.py                   # after every reset
python scripts/generate_demand.py                  # after every reset
```

The load test places real orders, so press **Reset demo data** after it and run the two generators again.
The sales generator gives the forecast in section 7 two months of sales; the demo data alone has one week,
too little to forecast from the same weekday or to measure forecast error. The demand generator adds the
delivery lookups the map in section 11 needs, including ones no store could serve.

Open http://localhost:8000 in **two browser windows** side by side (you need the second one for
steps 3 and 4). Keep the store selector on *DS-01 Central*.

## 1. The three stations (30 seconds)

> "This screen shows three people in one dark store. On the left, a customer's phone. In the
> middle, a picker's handheld. On the right, the store manager's desk. Everything they do goes
> through the same API a real app would use."

## 2. Only sellable items are shown (1 minute)

1. Customer station: choose *Home near DS-02 North* and press **Check delivery**.
2. Point at the note under the list: one item is hidden. Bananas are out of stock in that store.
3. Choose *Home far outside the city*: "No store delivers here yet."
4. Go back to *Home near DS-01 Central*.

> "The app first asks which store serves this address. Then it only lists what that one store can
> actually deliver. That is FR-1 and FR-2."

## 3. The last ice cream (2 minutes)

This is the centrepiece. The demo data has exactly **one** vanilla ice cream in store 1.

1. In **both** windows, add 1 *Vanilla ice cream* to the cart.
2. In window A press **Hold my items**. A blue panel appears with a 10-minute countdown.
3. On the ops desk, find ice cream in the stock table: *On hand 1, Held 1, Available 0*.
4. In window B press **Hold my items**. It is refused straight away: "Not enough stock... Nothing
   was held."

> "Two customers wanted the last unit. The database decided in a single statement who got it, and
> the other was told before paying, not after. On hand is still 1: the item has not left the shelf,
> it is only promised. Our tests run this race with twenty buyers at once and exactly one wins."

5. In window A press **Cancel order**. Held goes back to 0. In window B the ice cream can now be held.

## 4. A paid order, picked oldest-first (2 minutes)

1. Customer: add 8 *Toned milk* and 2 *Basmati rice*. **Hold my items**, then **Pay now**.
2. Ops desk: "Waiting for a picker" is 1, and the event feed shows `order.paid`. Point at window B: its
   ops desk shows the same within a second, without a refresh.

> "Every change is written as an event, and each store's screens are told over a live connection. If that
> connection drops, the page falls back to checking every few seconds and reconnects on its own."

3. Picker: press **Get next order**.

> "The milk line is split across two bins. Six come from batch M-OLD, which expires tomorrow, and
> only two from the newer batch. That is first-expiry-first-out. The lines are also sorted in
> walking order, nearest bin first."

4. On the rice line (the first one) press **Scan a wrong item**: rejected, nothing changes.
5. Press **Scan item** on the rice line.

## 5. An empty shelf (1 minute)

1. On the first milk line (bin C-01-01, batch M-OLD) press **Not on shelf**. The line moves to
   bin C-01-04.

> "The picker says the bin is empty. The system corrects the count, writes a ledger entry so we
> know who found the gap, and looks for the same item elsewhere. Had there been none, it would have
> dropped the line and released the hold so the customer is not charged."

2. Press **Scan item** on each line that is left.

## 6. Packing commits the sale (1 minute)

1. Before packing, point at the milk row on the ops desk: *Held* still shows the order's units.
2. Press **Mark packed**.

> "Only now does On hand go down. Until this moment a cancellation only had to release a hold.
> That is why a failed payment can never leave the stock count wrong."

3. Press **Rider collected the bag**. The order's rider checks in and the desk scans the bag ("Rider R-12
   collected order … from S-01"; the rider's number varies). "Handed to rider" becomes 1; the average pack
   time appears.

> "A rider gets only the bag of the order they were assigned. Another rider, or another order's bag, is refused."

## 7. Restocking (2 minutes)

1. Ops desk: the bread row is highlighted; it is below its reorder point.
2. Press **Check what needs reordering**. A transfer order appears.
3. Press it again: "Nothing to reorder".

> "Stock that is already on its way counts, so the system never orders the same thing twice."

4. Press **Hub dispatches truck**, then **Receive truck**.

> "Each item is put in a bin in the right temperature zone and is on sale the moment it is put away."

5. Customer station: bread now shows the new quantity.
6. Press **Recalculate forecasts**. Reorder points for the items with sales history change.

> "Each item's demand is forecast from the same weekday over the last four weeks, the model that won our
> backtest. Reorder points follow the formula in section 6.2 of the HLD, and the safety stock covers how wrong
> the forecast was over the last two weeks."

## 8. A busy store (2 minutes, optional)

1. Place and pay for six small orders without picking them. The store tile turns to *Busy*, and
   **Check delivery** now quotes an ETA five minutes longer.

> "When a store's backlog passes a threshold it protects itself by quoting a longer delivery time."

2. On the picker station press **Get next order**: "Busy store: 3 orders in one walk." Each line shows its
   tote (A, B or C), and the lines of all three orders are in one walking order.
3. Press **Scan item** on every line, then **Mark tote A packed**, then B and C. The crate slot moves on by
   itself.

> "At peak one walk serves three customers instead of one. Our comparison script,
> scripts/compare_picking.py, measured 48% less walking over 30 orders."

## 9. Logins: roles, stores and a safety buffer (2 minutes)

Open http://localhost:8000/docs.

1. **POST /v1/auth/login** → *Try it out* → `{"username": "picker1", "password": "demo"}` → *Execute*.
   Copy the `access_token`.
2. **Authorize** (top right): paste the token, *Authorize*, *Close*.
3. **GET /v1/pickers/{picker_id}/next-task** → *Try it out* → `picker_id` *picker1*, `store_id` *2* →
   *Execute*. Refused with 403: "You can only act on your own store".

> "The picker is from store 1. The token says so, and every staff call checks the role and the store
> before it does anything."

4. Log in again as `manager1`. In **Authorize**, press *Logout*, paste the new token, *Authorize*.
5. **PUT /v1/stores/{store_id}/stock-settings/{sku_id}** → `store_id` *1*, `sku_id` *7* (rice), body
   `{"safety_buffer": 5}` → *Execute*. The answer shows `atp` 5 lower than `on_hand`.
6. Back on the main screen: within five seconds the customer station shows 5 fewer rice left. (The ops
   desk table catches up at its next refresh; reload the page to show it at once.)

> "A manager can hide a few units of an item whose count is often a little off, so customers are never
> sold the last unit that may not be on the shelf."

## 10. Fast movers near the packing desk (1 minute, optional)

Still logged in as `manager1` in Swagger. Do this after the picking steps: it can move milk's second batch.

1. **GET /v1/slotting/plan** → *Try it out* → `store_id` *1* → *Execute*. `homes` ranks each zone's items by
   how fast they sold over the last 14 days, fastest in the bin nearest the packing desk; `moves` lists the
   batches to move there.
2. **POST /v1/slotting/apply** → `store_id` *1* → *Execute*. `moved` says what went where. A batch a picker is
   on the way to would be under `skipped`, and no bin is ever filled past its capacity.

> "Fast movers live nearest the packing desk. Stock is moved, never gained or lost: two ledger rows per batch.
> New deliveries go straight to an item's planned bin, so the shelf re-sorts itself as stock arrives."

3. In the terminal: `python scripts/compare_slotting.py` (a few seconds, on its own copy of the data).

> "Orders of shelf items walk less. Most orders also hold a chilled item, and the fridges are at the far end of
> the walk, so the overall saving is small: moving the fridges nearer the desk would help more, and that is a
> layout change, not slotting."

## 11. Where to open the next store (1 minute)

Open http://localhost:8000/network.html.

1. The blue outlines are the two stores' delivery zones, built from H3 hexagons. Green cells are the paid
   orders from this run.
2. The red cells are addresses customers asked about that no store could serve.
3. The dashed yellow cells are the suggested new sites. The list on the right says how many of those
   turned-away customers each would reach.

> "Every lookup we could not serve is recorded by hexagon. The suggestions pick, one at a time, the site that
> reaches the most of them, outside our current zones."

## 12. The ops dashboard and the load test (1 minute)

Open http://localhost:8000/ops.html.

1. The top tiles are problems that need someone now: stalled orders, bags waiting more than 5 minutes for a
   rider (the busy-hour totes from section 8, if you left them), items out of stock. Then every HLD 12 KPI,
   with its target and whether it is met, plus *Packed to rider (p90)* from the rider handoff. A KPI with no
   data yet (for example inventory accuracy before the first cycle count) says so instead of showing a
   made-up number.
2. Switch to the terminal with the load-test table from *Before you start*.

> "Thirty seconds of mostly reads with some checkouts. The read p99 is what the HLD's 100 ms target is about.
> This is one laptop and SQLite, not the HLD's cluster, so the number shows where time goes, not what
> production would do. Prometheus can scrape the same timings from /metrics."

## 13. Close (30 seconds)

Back on http://localhost:8000/docs.

> "Every action you saw is one of these API calls, and every staff call carries a login with a role and a
> store. The code has one module per service in our HLD, and one automated test walks this whole script
> through the API, from order to a final stock audit. What you saw is a prototype: SQLite instead of
> PostgreSQL, and an outbox table with a relay inside the app instead of Kafka."

## If something goes wrong

| Problem | Fix |
| --- | --- |
| Odd numbers from an earlier run | **Reset demo data** |
| "The hold ran out" | **Release expired holds**, then order again |
| Picker says no orders are waiting | The order was not paid, or the store selector is on the other store |
| Page will not load | Check the terminal running `uvicorn`; restart it |
| Errors such as "no such column" after a `git pull` | Stop the app, delete `darkstore.db`, start it again; the demo data is loaded afresh |
| A screen looks or behaves as before a `git pull` | The browser kept the old script: hard-refresh each page (Ctrl+Shift+R) |
| The map has no red cells or suggestions | Run `python scripts/generate_demand.py` (needed after every reset) |
| The first **Check what needs reordering** in section 7 says "Nothing to reorder" | The timer already ordered the bread: the app was started without `REPLENISHMENT_INTERVAL_MINUTES=0`. Show the order it raised, or restart the app with that setting |
| A *Try it out* call in /docs says 401 | The token was not pasted into **Authorize**, or it has expired; log in again |
