"""Unit-by-unit scanning and partial finds (Member 2 guide, task 2.2), and `picked_at` per line (task 2.1).

Store 1 has 30 rice in one bin, and milk in two: 6 of batch M-OLD and 24 of M-NEW."""
from sqlalchemy import select

from app.models import StockItem
from app.modules.inventory import service as inventory

from .conftest import assert_stock_is_consistent, place_paid_order
from .test_cycle_counts import start, submit
from .test_order_flow import next_job, scan_all


def scan(client, task: dict, qty: int | None = None, barcode: str | None = None):
    body = {"barcode": barcode or task["barcode"]} | ({"qty": qty} if qty is not None else {})
    return client.post(f"/v1/pick-tasks/{task['task_id']}/scans", json=body)


def short(client, task: dict, found: int | None = None):
    body = {"found": found} if found is not None else None
    return client.post(f"/v1/pick-tasks/{task['task_id']}/short", json=body)


def stock(client, sku_id: int) -> tuple[int, int]:
    row = client.get("/v1/stores/1/availability", params={"skus": str(sku_id)}).json()[0]
    return row["on_hand"], row["reserved"]


def rice_line(client, sku, qty: int = 6) -> tuple[dict, dict]:
    """A paid order of `qty` rice, claimed; returns (job, its one line)."""
    place_paid_order(client, "u-1", 1, [(sku("Basmati rice"), qty)])
    job = next_job(client)
    [line] = job["tasks"]
    return job, line


def task_in(job: dict, task_id: int) -> dict:
    return next(t for t in job["tasks"] if t["task_id"] == task_id)


def test_six_single_unit_scans_pick_a_six_unit_line_and_a_seventh_is_refused(client, sku):
    job, line = rice_line(client, sku)
    assert (line["scanned_qty"], line["picked_at"]) == (0, None)

    for n in range(1, 6):
        res = scan(client, line).json()                        # qty left out: one unit per beep
        assert res["match"] is True and res["message"] == f"{n} of 6 scanned."
        assert task_in(res["job"], line["task_id"])["status"] == "pending"
    # Half-scanned is not done: the order cannot be packed yet.
    blocked = client.post(f"/v1/fulfilment-jobs/{job['job_id']}/packed", json={"crate_slot": "S-01"})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "lines_pending"

    last = scan(client, line).json()
    done = task_in(last["job"], line["task_id"])
    assert last["message"] == "Picked." and last["lines_left"] == 0
    assert (done["status"], done["scanned_qty"]) == ("picked", 6) and done["picked_at"].endswith("Z")

    seventh = scan(client, line)
    assert seventh.status_code == 409 and seventh.json()["error"]["code"] == "task_closed"
    packed = client.post(f"/v1/fulfilment-jobs/{job['job_id']}/packed", json={"crate_slot": "S-01"}).json()
    assert packed["state"] == "packed"
    assert stock(client, sku("Basmati rice")) == (24, 0)
    assert_stock_is_consistent()


def test_more_units_than_the_line_still_needs_change_nothing(client, sku):
    _job, line = rice_line(client, sku)
    assert scan(client, line, qty=4).json()["message"] == "4 of 6 scanned."

    too_many = scan(client, line, qty=3).json()

    assert too_many["match"] is False and too_many["message"] == "Too many. This line needs 2 more, not 3."
    assert scan(client, line, qty=2).json()["message"] == "Picked."


def test_a_wrong_item_scanned_partway_changes_nothing(client, sku):
    job, line = rice_line(client, sku)
    scan(client, line, qty=2)

    wrong = scan(client, line, barcode="0000000000000").json()

    assert wrong["match"] is False
    line_now = task_in(client.get(f"/v1/fulfilment-jobs/{job['job_id']}").json(), line["task_id"])
    assert (line_now["scanned_qty"], line_now["status"]) == (2, "pending")


def test_found_four_of_six_with_no_other_stock_picks_four_and_drops_two(client, sku):
    rice = sku("Basmati rice")
    job, line = rice_line(client, sku)

    res = short(client, line, found=4)

    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["picked"], body["reallocated"], body["dropped"]) == (4, 0, 2)
    assert body["message"].startswith("4 picked. Count corrected.")
    rice_lines = sorted((t["status"], t["qty"]) for t in body["job"]["tasks"])
    assert rice_lines == [("picked", 4), ("short", 2)]
    assert stock(client, rice) == (4, 4)                      # the rest of the bin written off; the 4 in the tote held

    client.post(f"/v1/fulfilment-jobs/{job['job_id']}/packed", json={"crate_slot": "S-01"})
    assert stock(client, rice) == (0, 0)
    assert_stock_is_consistent()


def test_found_four_of_six_moves_the_other_two_to_another_bin(client, sku):
    milk = sku("Toned milk")
    place_paid_order(client, "u-2", 1, [(milk, 8)])
    job = next_job(client)
    old_batch = next(t for t in job["tasks"] if t["batch_no"] == "M-OLD")
    assert old_batch["qty"] == 6

    body = short(client, old_batch, found=4).json()

    assert (body["picked"], body["reallocated"], body["dropped"]) == (4, 2, 0)
    assert sorted((t["batch_no"], t["status"], t["qty"]) for t in body["job"]["tasks"]) == [
        ("M-NEW", "pending", 2), ("M-NEW", "pending", 2), ("M-OLD", "picked", 4), ("M-OLD", "reallocated", 2)]
    scan_all(client, body["job"])
    packed = client.post(f"/v1/fulfilment-jobs/{job['job_id']}/packed", json={"crate_slot": "S-01"}).json()
    assert packed["state"] == "packed"
    assert stock(client, milk) == (20, 0)                     # 30 - 2 written off - 8 sold
    assert_stock_is_consistent()


def test_an_empty_shelf_after_some_scans_keeps_the_scanned_units(client, sku):
    _job, line = rice_line(client, sku)
    for _ in range(3):
        scan(client, line)

    body = short(client, line).json()                          # no body: the 3 already scanned were found

    assert (body["picked"], body["dropped"]) == (3, 3)
    assert_stock_is_consistent()


def test_found_cannot_be_fewer_than_scanned_or_the_whole_line(client, sku):
    rice = sku("Basmati rice")
    job, line = rice_line(client, sku)
    scan(client, line, qty=2)

    too_few, everything = short(client, line, found=1), short(client, line, found=6)

    assert too_few.status_code == 409 and too_few.json()["error"]["code"] == "found_below_scanned"
    assert everything.status_code == 409 and everything.json()["error"]["code"] == "nothing_missing"
    assert short(client, line, found=-1).status_code == 422
    line_now = task_in(client.get(f"/v1/fulfilment-jobs/{job['job_id']}").json(), line["task_id"])
    assert (line_now["qty"], line_now["scanned_qty"], line_now["status"]) == (6, 2, "pending")
    assert stock(client, rice) == (30, 6)                     # nothing written off
    assert_stock_is_consistent()


def test_cancel_partway_through_a_line_gives_its_whole_hold_back(client, sku):
    rice = sku("Basmati rice")
    job, line = rice_line(client, sku)
    scan(client, line, qty=4)

    assert client.post(f"/v1/fulfilment-jobs/{job['job_id']}/cancel", json={"reason": "test"}).status_code == 200

    assert stock(client, rice) == (30, 0)
    assert_stock_is_consistent()


def test_a_count_during_scanning_leaves_out_the_units_already_in_the_tote(client, sku):
    """Inventory's cycle count must see the 4 scanned rice as off the shelf, though the line is not finished."""
    rice = sku("Basmati rice")
    _job, line = rice_line(client, sku)
    scan(client, line, qty=4)

    counted = submit(client, start(client, rice), {rice: 26}).json()   # 30 in the records, 4 in the tote

    assert counted["lines"][0]["difference"] == 0
    assert_stock_is_consistent()


def test_an_expiry_write_off_leaves_out_the_units_already_in_the_tote(client, db, sku):
    place_paid_order(client, "u-3", 1, [(sku("Toned milk"), 6)])
    [old_batch] = next_job(client)["tasks"]
    scan(client, old_batch, qty=2)

    item = db.scalar(select(StockItem).where(StockItem.batch_no == "M-OLD", StockItem.store_id == 1))

    assert inventory._in_totes(db, item) == 2


def test_an_absurd_quantity_is_refused_before_it_reaches_the_database(client, sku):
    _job, line = rice_line(client, sku)

    assert scan(client, line, qty=10**20).status_code == 422
    assert short(client, line, found=10**20).status_code == 422
    assert scan(client, line, qty=6).json()["message"] == "Picked."
