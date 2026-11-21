"""Velocity-based slotting (guide task 3.6): fast movers live in the bins nearest the packing desk.

Store 1 of the demo data, by walk_seq (1 is next to the desk): ambient A-01-01 bread, A-01-02 eggs, A-01-03 bananas,
A-01-04 rice, A-02-01 atta, A-02-02 sugar, A-02-03 tea, A-02-04 noodles, A-03-01 chips, A-03-02 oil, A-03-03 soap,
A-03-04 empty; chilled C-01-01 milk (batch M-OLD), C-01-02 curd, C-01-03 paneer, C-01-04 milk (M-NEW); frozen F-01-01
ice cream, F-01-02 peas."""
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func, select, update

from app.db import SessionLocal
from app.errors import DomainError
from app.models import SKU, Bin, StockItem, StockLedger
from app.modules.assortment import service as assortment
from app.modules.inbound import service as inbound
from app.modules.inventory import service as inventory
from app.modules.slotting import service as slotting

from .conftest import assert_stock_is_consistent, place_paid_order, plant_sales
from .test_order_flow import next_job

FORTNIGHT = range(1, 15)                                    # the 14 whole days before today


def every_day(units: int) -> dict[int, int]:
    return dict.fromkeys(FORTNIGHT, units)


def moves_of(plan: dict) -> dict[int, list[tuple[str, str]]]:
    """{sku_id: [(from_bin, to_bin), ...]} in the plan's order."""
    found: dict[int, list[tuple[str, str]]] = {}
    for move in plan["moves"]:
        found.setdefault(move["sku_id"], []).append((move["from_bin"], move["to_bin"]))
    return found


def test_velocity_is_units_sold_a_day_over_whole_days(db, sku):
    chips, milk = sku("Potato chips"), sku("Toned milk")
    plant_sales(db, {chips: every_day(2), milk: {0: 50, 20: 30}})          # today is only partly over
    assert slotting.velocity(db, 1) == {chips: 2.0}
    assert slotting.velocity(db, 1, days=7) == {chips: 2.0}


def test_the_plan_puts_the_fastest_sku_in_the_nearest_bin(db, sku):
    chips, bread = sku("Potato chips"), sku("White bread")
    plant_sales(db, {chips: every_day(9), bread: every_day(3)})
    plan = slotting.plan(db, 1)
    homes = {home["sku_id"]: home["bin"] for home in plan["homes"]}
    assert (homes[chips], homes[bread]) == ("A-01-01", "A-01-02")
    moves = moves_of(plan)
    assert (moves[chips], moves[bread]) == ([("A-03-01", "A-01-01")], [("A-01-01", "A-01-02")])


def test_skus_that_do_not_sell_stay_unless_a_seller_needs_their_bin(db, sku):
    chips, bread, eggs = sku("Potato chips"), sku("White bread"), sku("Eggs")
    plant_sales(db, {chips: every_day(9), bread: every_day(3)})
    moves = moves_of(slotting.plan(db, 1))
    assert moves[eggs] == [("A-01-02", "A-03-04")]          # bread needs its bin: to the furthest free one
    assert set(moves) == {chips, bread, eggs}                # milk, split over two bins, is left as it is too


def test_chilled_skus_are_planned_into_chilled_bins_only(db, sku):
    milk, curd, paneer = sku("Toned milk"), sku("Curd"), sku("Paneer")
    plant_sales(db, {paneer: every_day(8), curd: every_day(5), milk: every_day(1)})
    plan = slotting.plan(db, 1)
    assert {home["sku_id"]: home["bin"] for home in plan["homes"]} == {paneer: "C-01-01", curd: "C-01-02",
                                                                      milk: "C-01-03"}
    assert moves_of(plan) == {paneer: [("C-01-03", "C-01-01")],
                              milk: [("C-01-01", "C-01-03"), ("C-01-04", "C-01-03")]}   # both milk batches


def test_with_more_sellers_than_bins_the_slowest_keeps_its_place(db, sku):
    ice_cream, peas = sku("Vanilla ice cream"), sku("Frozen green peas")
    new_sku = SKU(name="Kulfi", category="Frozen", barcode="8909999999999", storage_type="frozen",
                  shelf_life_days=180, price=60, case_size=1)
    db.add(new_sku)
    db.commit()
    kulfi = new_sku.id
    assortment.add_sku(db, 1, kulfi)
    first = db.scalar(select(Bin).where(Bin.store_id == 1, Bin.code == "F-01-01"))
    inventory.receive(db, 1, kulfi, 5, first.id, "K1", date.today() + timedelta(days=90), "test", "test")
    db.commit()
    plant_sales(db, {kulfi: every_day(9), ice_cream: every_day(4), peas: every_day(1)})
    plan = slotting.plan(db, 1)
    assert {home["sku_id"]: home["bin"] for home in plan["homes"]} == {kulfi: "F-01-01", ice_cream: "F-01-02"}
    assert moves_of(plan) == {ice_cream: [("F-01-01", "F-01-02")]}         # peas: no bin of its own, no bin free
    assert_stock_is_consistent()


def test_no_sales_in_the_last_whole_days_means_nothing_moves(db, sku):
    plant_sales(db, {sku("Potato chips"): {0: 50, 15: 40}})               # today, and before the 14 days
    assert slotting.plan(db, 1) == {"store_id": 1, "days": 14, "homes": [], "moves": []}


def test_planning_changes_nothing(db, sku):
    plant_sales(db, {sku("Potato chips"): every_day(9), sku("Toned milk"): every_day(4)})

    def snapshot():
        return (sorted(db.execute(select(StockItem.id, StockItem.bin_id, StockItem.qty)).all()),
                db.scalar(select(func.count(StockLedger.id))))

    before = snapshot()
    assert slotting.plan(db, 1)["moves"]
    assert snapshot() == before


def reslot(client, key: str | None = None):
    return client.post("/v1/slotting/apply", params={"store_id": 1}, headers={"Idempotency-Key": key} if key else {})


def bins_holding(db, sku_id: int) -> set[str]:
    return set(db.scalars(select(Bin.code).join(StockItem, StockItem.bin_id == Bin.id)
                          .where(StockItem.store_id == 1, StockItem.sku_id == sku_id, StockItem.qty > 0)))


def test_after_apply_the_fastest_ambient_sku_is_in_the_nearest_ambient_bin(client, db, sku):
    chips, bread = sku("Potato chips"), sku("White bread")
    plant_sales(db, {chips: every_day(9), bread: every_day(3)})
    applied = reslot(client)
    assert applied.status_code == 200, applied.text
    nearest = db.scalar(select(Bin.code).where(Bin.store_id == 1, Bin.zone == "ambient").order_by(Bin.walk_seq))
    assert bins_holding(db, chips) == {nearest}
    assert_stock_is_consistent()


def test_chilled_skus_never_leave_chilled_bins(client, db, sku):
    chilled = (sku("Toned milk"), sku("Curd"), sku("Paneer"))
    plant_sales(db, {chilled[2]: every_day(8), chilled[1]: every_day(5), chilled[0]: every_day(1),
                     sku("Potato chips"): every_day(9)})
    assert reslot(client).status_code == 200
    zones = {zone for sku_id in chilled for zone in db.scalars(
        select(Bin.zone).join(StockItem, StockItem.bin_id == Bin.id)
        .where(StockItem.store_id == 1, StockItem.sku_id == sku_id, StockItem.qty > 0))}
    assert zones == {"chilled"}
    assert bins_holding(db, chilled[0]) == {"C-01-03"}                     # both milk batches, together
    assert_stock_is_consistent()


def test_apply_does_the_plan_and_a_second_apply_has_nothing_to_move(client, db, sku):
    plant_sales(db, {sku("Potato chips"): every_day(9), sku("White bread"): every_day(3)})
    planned = client.get("/v1/slotting/plan", params={"store_id": 1}).json()
    applied = reslot(client).json()

    def by_batch(moves: list[dict]) -> list[dict]:
        return sorted(moves, key=lambda move: move["stock_item_id"])

    assert (by_batch(applied["moved"]), applied["skipped"]) == (by_batch(planned["moves"]), [])
    assert client.get("/v1/slotting/plan", params={"store_id": 1}).json()["moves"] == []
    again = reslot(client).json()
    assert (again["moved"], again["skipped"]) == ([], [])
    assert_stock_is_consistent()


def test_a_batch_a_picker_was_sent_for_is_skipped(client, db, sku):
    chips = sku("Potato chips")
    plant_sales(db, {chips: every_day(9)})
    place_paid_order(client, "reslot-1", 1, [(chips, 2)])
    next_job(client)                                            # a picker is on the way to A-03-01 for 2 chips
    applied = reslot(client).json()
    [skipped] = [move for move in applied["skipped"] if move["sku_id"] == chips]
    assert (skipped["from_bin"], skipped["to_bin"], skipped["code"]) == ("A-03-01", "A-01-01", "promised_to_picker")
    assert bins_holding(db, chips) == {"A-03-01"}               # nothing of that batch moved
    assert_stock_is_consistent()


def test_apply_never_overfills_a_bin(client, db, sku):
    """Two full bins that must swap: neither batch fits until the other has left, so both are skipped, not forced."""
    eggs, bread = sku("Eggs"), sku("White bread")
    plant_sales(db, {eggs: every_day(9), bread: every_day(3)})            # eggs to A-01-01, bread to A-01-02
    db.execute(update(Bin).where(Bin.store_id == 1, Bin.code.in_(("A-01-01", "A-01-02"))).values(capacity=30))
    db.commit()                                                           # A-01-01: 4 bread, A-01-02: 30 eggs
    applied = reslot(client).json()
    assert applied["moved"] == []
    assert sorted((move["sku_id"], move["code"]) for move in applied["skipped"]) == sorted(
        [(eggs, "no_room"), (bread, "no_room")])
    assert (bins_holding(db, eggs), bins_holding(db, bread)) == ({"A-01-02"}, {"A-01-01"})
    assert_stock_is_consistent()


def test_a_retried_apply_moves_once(client, db, sku):
    plant_sales(db, {sku("Potato chips"): every_day(9)})
    first, again = reslot(client, key="reslot-key"), reslot(client, key="reslot-key")
    assert first.json()["moved"] and first.json() == again.json()
    assert db.scalar(select(func.count(StockLedger.id)).where(StockLedger.reason == "move")) == \
        2 * len(first.json()["moved"])
    assert_stock_is_consistent()


def test_the_ledger_names_the_manager_who_reslotted(client_as, db, sku):
    plant_sales(db, {sku("Potato chips"): every_day(9)})
    assert reslot(client_as("manager", 1)).status_code == 200
    rows = db.execute(select(StockLedger.actor, StockLedger.reference).where(StockLedger.reason == "move")).all()
    assert rows and set(rows) == {("test-manager", "reslot")}


def test_staff_see_the_plan_and_only_managers_of_the_store_apply_it(client_as):
    url, store = "/v1/slotting/plan", {"store_id": 1}
    assert client_as("picker", 1).get(url, params=store).status_code == 200
    assert client_as("picker", 2).get(url, params=store).status_code == 403
    assert reslot(client_as("picker", 1)).status_code == 403
    assert reslot(client_as("manager", 2)).status_code == 403
    assert reslot(client_as("manager", 1)).status_code == 200


def open_receipt(client) -> int:
    opened = client.post("/v1/goods-receipts/open", json={"store_id": 1})
    assert opened.status_code == 201, opened.text
    return opened.json()["receipt_id"]


def scan_in(client, db, receipt_id: int, sku_id: int, qty: int) -> None:
    sku = db.get(SKU, sku_id)
    scanned = client.post(f"/v1/goods-receipts/{receipt_id}/scans", json={
        "barcode": sku.barcode, "qty": qty, "batch_no": "NEW",
        "expiry_date": (date.today() + timedelta(days=sku.shelf_life_days)).isoformat()})
    assert scanned.status_code == 200, scanned.text


def tasks(client, receipt_id: int) -> list[tuple[str, int]]:
    return [(task["bin_code"], task["qty"])
            for task in client.get(f"/v1/goods-receipts/{receipt_id}/putaway-tasks").json()]


def test_a_delivery_goes_to_the_planned_home_bin(client, db, sku):
    """Re-slotting as stock arrives: new units go to the SKU's planned home even before anyone applies the plan."""
    chips = sku("Potato chips")
    plant_sales(db, {chips: every_day(9)})                     # chips: home A-01-01, stock still in A-03-01
    receipt = open_receipt(client)
    scan_in(client, db, receipt, chips, 10)
    assert tasks(client, receipt) == [("A-01-01", 10)]
    one_call = client.post("/v1/goods-receipts", json={"store_id": 1, "lines": [{"sku_id": chips, "qty": 5}]})
    assert one_call.status_code == 201, one_call.text
    assert bins_holding(db, chips) == {"A-01-01", "A-03-01"}
    assert_stock_is_consistent()


def test_a_full_home_bin_falls_back_to_the_old_rule(client, db, sku):
    chips = sku("Potato chips")
    plant_sales(db, {chips: every_day(9)})
    db.execute(update(Bin).where(Bin.store_id == 1, Bin.code == "A-01-01").values(capacity=10))
    db.commit()                                                # 4 bread in it: room for 6
    receipt = open_receipt(client)
    scan_in(client, db, receipt, chips, 10)
    assert tasks(client, receipt) == [("A-01-01", 6), ("A-03-01", 4)]     # the rest joins the chips already there


def test_suggest_bin_prefers_the_home_bin(client, db, sku):
    chips, rice = sku("Potato chips"), sku("Basmati rice")
    plant_sales(db, {chips: every_day(9)})

    def suggested(sku_id: int) -> str:
        return client.get("/v1/slotting/suggest", params={"store_id": 1, "sku_id": sku_id}).json()["code"]

    assert (suggested(chips), suggested(rice)) == ("A-01-01", "A-01-04")   # rice does not sell: kept together


def test_the_walk_gets_shorter_when_a_fast_mover_was_far_from_the_desk(db, sku):
    """The measurement of scripts/compare_slotting.py on sales that do not change with the date: chips sell three
    times as fast as bread but sit at A-03-01 (walk_seq 9)."""
    from scripts.compare_slotting import measure

    plant_sales(db, {sku("Potato chips"): every_day(9), sku("White bread"): every_day(3)})
    result = measure(db, 1, orders=30, random_seed=1)
    everything = result["all"]
    assert (result["orders"], result["moved"], result["skipped"]) == (30, 3, 0)
    assert everything["orders"] == result["ambient_only"]["orders"] == 30            # chips and bread are ambient
    assert everything["after"] < everything["before"] / 2
    assert_stock_is_consistent()


def test_compare_measures_the_demo_data_with_generated_sales(fresh_data):
    from scripts.compare_slotting import compare

    result = compare(orders=20, random_seed=1)
    assert result["orders"] == result["all"]["orders"] == 20 and result["moved"] > 0
    assert 0 <= result["ambient_only"]["orders"] <= 20
    assert_stock_is_consistent()


def test_compare_slotting_runs_from_the_command_line():
    """On its own throwaway database, like scripts/compare_picking.py: it never touches darkstore.db."""
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, "scripts/compare_slotting.py", "--orders", "20", "--seed", "1"], cwd=root,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "20 random orders in store 1" in done.stdout
    assert "All orders" in done.stdout and "Ambient items only" in done.stdout
    assert "Before re-slotting" in done.stdout and "after re-slotting" in done.stdout
    assert "the plan saves" in done.stdout and "-0%" not in done.stdout


def test_a_batch_in_the_wrong_zone_goes_back_to_its_own_zone(client, db, sku):
    """A count can record a SKU in a bin of the wrong zone: here 2 paneer in A-01-01, which chips need. The paneer goes
    to the chilled bin already holding paneer; it was sent to an ambient bin, and inventory.move's wrong_zone then
    failed every apply."""
    chips, paneer = sku("Potato chips"), sku("Paneer")
    plant_sales(db, {chips: every_day(9)})                     # chips: home A-01-01
    nearest = db.scalar(select(Bin.id).where(Bin.store_id == 1, Bin.code == "A-01-01"))
    inventory.adjust(db, 1, paneer, 2, "count", bin_id=nearest, actor="test")
    assert moves_of(slotting.plan(db, 1))[paneer] == [("A-01-01", "C-01-03")]
    applied = reslot(client)
    assert applied.status_code == 200, applied.text
    assert bins_holding(db, paneer) == {"C-01-03"}
    assert_stock_is_consistent()


def test_one_batch_that_cannot_move_does_not_stop_the_others(client, db, sku, monkeypatch):
    chips, bread, eggs = sku("Potato chips"), sku("White bread"), sku("Eggs")
    plant_sales(db, {chips: every_day(9), bread: every_day(3)})
    real_move = inventory.move

    def move_all_but_chips(session, item, to_bin_id, qty, **details):
        if item.sku_id == chips:                    # say the units went missing since the plan was made
            raise DomainError(409, "stock_mismatch", "The bin holds fewer units than the record says")
        return real_move(session, item, to_bin_id, qty, **details)

    monkeypatch.setattr(inventory, "move", move_all_but_chips)
    applied = reslot(client)
    assert applied.status_code == 200, applied.text
    assert [(move["sku_id"], move["code"]) for move in applied.json()["skipped"]] == [(chips, "stock_mismatch")]
    assert {move["sku_id"] for move in applied.json()["moved"]} == {bread, eggs}
    assert_stock_is_consistent()


def test_expired_batches_are_left_for_the_write_off(db, sku):
    """Picking never sends anyone to an expired batch, so moving it would only take room in the home bin."""
    chips = sku("Potato chips")
    plant_sales(db, {chips: every_day(9)})
    db.execute(update(StockItem).where(StockItem.store_id == 1, StockItem.sku_id == chips)
               .values(expiry_date=date.today() - timedelta(days=1)))
    db.commit()
    assert chips not in moves_of(slotting.plan(db, 1))


def test_apply_uses_the_days_of_the_plan_it_was_shown(client, db, sku):
    chips, bread = sku("Potato chips"), sku("White bread")
    plant_sales(db, {chips: dict.fromkeys(range(8, 15), 9), bread: dict.fromkeys(range(1, 8), 3)})
    week = client.get("/v1/slotting/plan", params={"store_id": 1, "days": 7}).json()
    applied = client.post("/v1/slotting/apply", params={"store_id": 1, "days": 7}).json()
    assert applied["moved"] == week["moves"] == []      # over 7 days only bread sold, and it is nearest already
    assert bins_holding(db, chips) == {"A-03-01"}


def test_apply_and_a_one_call_receipt_take_turns(db, sku, monkeypatch):
    """PostgreSQL: apply locks the store's bins in id order. A one-call receipt that had locked a chilled bin (higher
    id) and then wanted an ambient one deadlocked with it, and one of them failed; now both take the bins in one
    order. On SQLite one writer at a time makes them take turns anyway."""
    milk, bread = sku("Toned milk"), sku("White bread")
    plant_sales(db, {sku("Potato chips"): every_day(9)})
    first_line_in, checks = threading.Event(), []
    check = slotting.ensure_not_overfilled

    def check_slowly(session, bin_id):
        checks.append(bin_id)
        if len(checks) == 1:                        # the receipt has put the milk away and holds its bin
            first_line_in.set()
            time.sleep(0.5)                         # apply starts now and locks what it can
        return check(session, bin_id)

    monkeypatch.setattr(slotting, "ensure_not_overfilled", check_slowly)

    def receive() -> int:
        with SessionLocal() as session:
            lines = [{"sku_id": milk, "qty": 1}, {"sku_id": bread, "qty": 1}]
            return inbound.receive_goods(session, 1, lines)["receipt_id"]

    def reslot_now() -> int:
        assert first_line_in.wait(5)
        with SessionLocal() as session:
            return len(slotting.apply(session, 1, actor="test")["moved"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipt, applied = pool.submit(receive), pool.submit(reslot_now)
        assert receipt.result() > 0 and applied.result() > 0
    assert_stock_is_consistent()
