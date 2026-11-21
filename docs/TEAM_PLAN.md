# Team plan

The prototype already runs the whole order flow end to end. From here the work splits into four
parts that follow the HLD's own boundaries, so each person owns a set of modules and can work
without waiting on the others most of the time.

## 1. Who owns what

| | Member 1 | Member 2 | Member 3 | Member 4 |
| --- | --- | --- | --- | --- |
| **Role** | Inventory and stock integrity | Order fulfilment | Supply | Platform, store network and screens |
| **HLD services** | 3 Inventory | 4 Fulfilment Orchestrator, 5 Pick & Pack, 11 Rider Handoff | 7 Forecasting, 8 Replenishment, 9 Inbound, 6 Slotting, 2 Assortment | 1 Serviceability, 10 Network Planning, gateway, events, monitoring |
| **Modules owned** | `inventory/` | `fulfilment/`, `pickpack/` | `forecasting/`, `replenishment/`, `inbound/`, `slotting/`, `assortment/` | `serviceability/`, `network_planning/`, `dashboard/`, `web/` |
| **Shared files owned** | `models.py` (stock tables), `db.py` | `models.py` (job and task tables) | `models.py` (transfer order, forecast tables), `seed.py` | `main.py`, `events.py`, `config.py`, CI, Docker |
| **One-line goal** | Stock is never wrong and never oversold | A paid order is packed fast and never gets stuck | The shelf is full before the customer asks | Everything is secure, observable and demo-ready |
| **Guide** | `guide-member-1-inventory.md` | `guide-member-2-fulfilment.md` | `guide-member-3-supply.md` | `guide-member-4-platform.md` |

Decide among yourselves who takes which number. A fair way to choose: Member 1 suits whoever likes
databases and correctness, Member 2 whoever likes workflows and state machines, Member 3 whoever
likes data and a bit of maths, Member 4 whoever likes infrastructure and front-end work.

"Owns" means: you review every pull request that touches those files, and you are the person to
ask about them. Others may still change your files through a pull request you approve.

## 2. Milestones

Four milestones of about one week each. If your deadline is nearer, do Milestones 1 and 2 fully
and pick one task each from 3 and 4. Every milestone ends with a working `main` and a short demo
to each other.

| | Member 1 | Member 2 | Member 3 | Member 4 |
| --- | --- | --- | --- | --- |
| **Day 0** (together) | Everyone: clone, run, pass tests, do the five-minute tour, read `ARCHITECTURE.md` | | | Create the GitHub repo and invite the team |
| **Milestone 1: learn the code** | 1.1 Safety buffer in ATP · 1.2 Automatic hold sweeper | 2.1 Per-line timing and metrics · 2.2 Unit-by-unit scanning | 3.1 Sales history generator · 3.2 Forecast backtest | 4.1 Repo, CI and Docker · 4.2 Login and roles |
| **Milestone 2: core features** | 1.3 Expiry handling · 1.4 Idempotency keys | 2.3 Cancel after payment · 2.4 Substitutes for dropped lines | 3.3 Better forecast model · 3.4 Smarter replenishment | 4.3 H3 delivery zones · 4.4 Network planning from orders |
| **Milestone 3: scale and resilience** | 1.5 Cycle counts with approval · 1.6 ATP cache | 2.5 Stuck-order recovery · 2.6 Batch picking at peak | 3.5 Two-step receiving · 3.6 Velocity-based slotting | 4.5 Live event feed · 4.6 Ops dashboard page |
| **Milestone 4: prove it** | 1.7 PostgreSQL and migrations · 1.8 Stock audit and load test | 2.7 Rider handoff flow · 2.8 Picker phone screen and timing run | 3.7 Assortment recommendations · 3.8 Supply KPIs | 4.7 Monitoring · 4.8 Integration and final demo |

The first task in every guide is a deliberately small "warm-up" that touches the model, the
service, the router and a test, so you learn the whole path before the bigger tasks.

## 3. Contracts between members

These are the places where one person's code calls another's. Changing anything in this table
needs a message to the other person *before* the pull request, and their review on it.

| Provided by | What | Used by | For |
| --- | --- | --- | --- |
| Member 1 | `inventory.allocate`, `commit_sale`, `unreserve`, `write_off_missing`, `write_off_unpromised` | Member 2 | Creating pick tasks, packing, dropped lines |
| Member 1 | `inventory.receive` | Member 3 | Putaway |
| Member 1 | `inventory.get_level`, `availability` | Members 2, 3, 4 | Reading stock |
| Member 1 | `stock.changed` event (payload: `sku_id`, `cause`) | Member 3 | Low-stock detection |
| Member 1 | `order.paid` event (payload: `order_id`, `reservation_id`) | Member 2 | Creating the fulfilment job |
| Member 2 | `Store.status` set by `fulfilment.refresh_store_load` | Member 4 | ETA and "busy" in serviceability |
| Member 2 | `order.ready_for_pickup`, `fulfilment.state_changed` events | Member 4 | Live feed, dashboard |
| Member 2 | Fulfilment metrics function (task 2.1) | Member 4 | Dashboard (task 4.6) |
| Member 3 | `StockLevel.reorder_point` and `target_stock` (written by forecasting) | Member 1 | Shown in availability; not stock quantities, so this is allowed |
| Member 3 | `slotting.suggest_bin` | Member 3 (inbound), Member 1 (cycle counts) | Choosing bins |
| Member 3 | Supply KPI functions (task 3.8) | Member 4 | Dashboard (task 4.6) |
| Member 4 | `require_role` dependency (task 4.2) | Everyone | Protecting endpoints |
| Member 4 | `events.publish` / `subscribe` | Everyone | All events |

### Tasks that need two people

| Task | Lead | Needs | Why |
| --- | --- | --- | --- |
| 2.4 Substitutes | Member 2 | Member 1 adds `inventory.swap_line` | Holding the substitute must be atomic |
| 1.7 PostgreSQL | Member 1 | Member 4's Docker Compose file (4.1) | Postgres runs in a container |
| 4.2 Login and roles | Member 4 | Everyone updates their own tests | Protected endpoints need a logged-in test client |
| 3.4 Bin capacity | Member 3 | Member 1 reviews the `Bin` model change | Shared table |
| 3.6 Slotting moves | Member 3 | Member 1 adds `inventory.move` | Only Inventory may move stock between bins |
| 2.5 Stuck orders, 3.4 auto-replenishment | Members 2 and 3 | Member 1's background loop (1.2) | One place runs all timed jobs |
| 4.4 Order locations | Member 4 | Member 1 adds `h3_cell` to reservations | Shared table and endpoint |
| 4.6 Ops dashboard | Member 4 | Metrics from 2.1 and 3.8 | Data to display |
| 1.8 / 4.8 Load test | Members 1 and 4 | Each other | One script, two reports (correctness, latency) |

## 4. Working rhythm

- **Twice a week, 15 minutes:** each person says what they merged, what they are doing next, and
  what is blocking them. Blockers are solved after the call by the two people involved.
- **End of each milestone, 30 minutes:** everyone demos their tasks on `main`. Anything not merged
  is not done.
- **One group chat thread per contract change**, so decisions can be found later.
- **Integrator:** Member 4 keeps `main` green. If a merge breaks the tests, the person who merged
  fixes or reverts it the same day.

## 5. Definition of done (for every task)

A task is done when all of these are true:

1. The behaviour in the task's "Done when" list works on `main`.
2. There is at least one automated test for the new behaviour, and `python -m pytest -q` passes.
3. Tests that move stock end with `assert_stock_is_consistent()`.
4. New endpoints appear in `/docs` with a clear summary; new settings are in `config.py`.
5. The "Simplified for now" cell for that feature in `README.md` is updated.
6. A teammate has reviewed and approved the pull request.

## 6. The first day, together

1. Member 4 creates the GitHub repository and pushes this code (steps are in task 4.1).
2. Everyone clones it, runs the app, runs the tests and does the five-minute tour in `README.md`.
3. Everyone reads `ARCHITECTURE.md` (20 minutes) and their own guide's first two sections.
4. Agree who is Member 1, 2, 3 and 4, and write the names at the top of this file.
5. Agree the two weekly call times and the milestone dates, and write them below.
6. Each person starts their warm-up task.

| Milestone | Date | Demo done? |
| --- | --- | --- |
| 1 | | |
| 2 | | |
| 3 | | |
| 4 | | |

## 7. Report and presentation split

Each person writes up and presents the HLD sections that match their code, so nobody explains
something they did not build.

| HLD section | Writes and presents |
| --- | --- |
| 1 Introduction, 2 Requirements, 3 Capacity | Whoever wrote the HLD, reviewed by all |
| 4 Architecture, 8 API and events, 10 Security, 11 Tech stack, 12 Monitoring | Member 4 |
| 5 Core components | Each member, their own services |
| 6.1 Order to dispatch | Member 2 |
| 6.2 Replenishment, 6.3 Inbound | Member 3 |
| 7 Data design, 9 Scalability and consistency | Member 1 |
| 13 Trade-offs, risks, future scope | All: one trade-off each, from what you learned building it |

For the final demo, follow `DEMO_SCRIPT.md` and add one minute per person for the feature they
are proudest of.

## 8. Risks to the project itself

| Risk | What to do about it |
| --- | --- |
| Two people edit `models.py` at once and conflict | Keep model changes in small, separate pull requests; merge them first |
| A contract function changes and breaks another member's code | Follow section 3; the tests of the other module will fail in CI and show it |
| Someone falls behind | Milestone demos make this visible early; the tasks in Milestones 3 and 4 can be dropped without breaking anything |
| Login (4.2) breaks everyone's tests | Member 4 adds a ready-made logged-in test client in the same pull request |
| The demo fails on the day | `main` is always demo-ready; rehearse `DEMO_SCRIPT.md` twice; keep *Reset demo data* handy |
