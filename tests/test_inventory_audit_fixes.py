"""Five inventory problems found in Member 4's audit of main (issues #56 to #60).

Store 1 has 30 rice and 30 sugar, each in one ambient bin, 4 white bread, and one vanilla ice cream and 30 peas in
frozen bins; store 2 stocks rice too."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app import db as app_db
from app import seed
from app.db import Base
from app.errors import DomainError
from app.models import Bin, StockItem, Store
from app.modules.inventory import service as inventory

from .conftest import assert_stock_is_consistent, place_paid_order
from .test_cycle_counts import start, submit
from .test_order_flow import next_job, scan_all


def level(client, sku_id: int, store_id: int = 1) -> tuple[int, int]:
    row = client.get(f"/v1/stores/{store_id}/availability", params={"skus": str(sku_id)}).json()[0]
    return row["on_hand"], row["reserved"]


def item_of(db, sku_id: int) -> StockItem:
    return db.scalar(select(StockItem).where(StockItem.store_id == 1, StockItem.sku_id == sku_id))


# --------------------------------------------------------------------------- #58 a reused order id


def hold(client, order_id: str, store_id: int, lines: list[tuple[int, int]]):
    return client.post("/v1/reservations", json={"order_id": order_id, "store_id": store_id,
                                                 "lines": [{"sku_id": s, "qty": q} for s, q in lines]})


def test_an_order_id_used_for_another_order_is_refused_and_shows_nothing_of_the_first(client, sku):
    bread, rice = sku("White bread"), sku("Basmati rice")
    first = hold(client, "ORD-X", 1, [(bread, 1)])

    other_store = hold(client, "ORD-X", 2, [(rice, 5)])
    other_lines = hold(client, "ORD-X", 1, [(bread, 2)])
    other_item = hold(client, "ORD-X", 1, [(rice, 1)])

    assert first.status_code == 201
    for res in (other_store, other_lines, other_item):
        assert res.status_code == 409 and res.json()["error"]["code"] == "order_id_reused"
        assert first.json()["reservation_id"] not in res.text
    assert level(client, rice, store_id=2)[1] == 0 and level(client, bread)[1] == 1
    assert_stock_is_consistent()


def test_a_true_retry_still_returns_the_first_hold(client, sku):
    bread, rice = sku("White bread"), sku("Basmati rice")
    first = hold(client, "ORD-Y", 1, [(bread, 1), (rice, 2)])

    again = hold(client, "ORD-Y", 1, [(rice, 1), (bread, 1), (rice, 1)])     # the same cart, written another way

    assert again.status_code == 201
    assert again.json()["reservation_id"] == first.json()["reservation_id"]
    assert level(client, rice) == (30, 2)


# --------------------------------------------------------------------------- #57 stock adjustments


def adjust(client, sku_id: int, delta: int, **extra):
    return client.post("/v1/stock-adjustments", json={"store_id": 1, "sku_id": sku_id, "delta": delta,
                                                      "reason": "damage", **extra})


def test_an_adjustment_leaves_units_in_a_tote_so_the_order_still_packs(client, sku):
    rice = sku("Basmati rice")
    place_paid_order(client, "t-1", 1, [(rice, 3)])
    job = next_job(client)
    scan_all(client, job)                                          # 3 of the 30 are in the picker's tote

    whole_bin = adjust(client, rice, -30)
    the_shelf = adjust(client, rice, -27)

    assert whole_bin.status_code == 409 and whole_bin.json()["error"]["code"] == "in_tote"
    assert whole_bin.json()["error"]["details"] == {"on_shelf": 27}
    assert the_shelf.status_code == 200 and level(client, rice) == (3, 3)
    packed = client.post(f"/v1/fulfilment-jobs/{job['job_id']}/packed", json={"crate_slot": "S-01"})
    assert packed.status_code == 200, packed.text
    assert level(client, rice) == (0, 0)
    assert_stock_is_consistent()


def test_more_than_the_books_hold_is_still_not_enough_stock(client, sku):
    res = adjust(client, sku("Basmati rice"), -31)

    assert res.status_code == 409 and res.json()["error"]["code"] == "not_enough_stock"


def test_found_units_go_only_into_a_bin_of_the_right_zone_with_room(client, db, sku):
    rice, ice_cream = sku("Basmati rice"), sku("Vanilla ice cream")
    rice_bin = db.get(Bin, item_of(db, rice).bin_id)
    room = rice_bin.capacity - 30

    wrong_zone = adjust(client, ice_cream, 5, bin_id=rice_bin.id)
    too_many = adjust(client, rice, room + 1, bin_id=rice_bin.id)
    fits = adjust(client, rice, room, bin_id=rice_bin.id)

    assert wrong_zone.status_code == 422 and wrong_zone.json()["error"]["code"] == "wrong_zone"
    assert too_many.status_code == 409 and too_many.json()["error"]["code"] == "bin_full"
    assert fits.status_code == 200 and level(client, rice) == (rice_bin.capacity, 0)
    assert level(client, ice_cream) == (1, 0)
    assert_stock_is_consistent()


def test_a_cycle_count_still_records_what_is_on_the_shelf_wherever_it_is(client, client_as, db, sku):
    """A count is not an instruction to put stock somewhere: frozen peas found in an ambient bin are counted there."""
    rice, peas = sku("Basmati rice"), sku("Frozen green peas")
    count = start(client, rice)

    done = submit(client, count, {rice: 30, peas: 1})
    if done.json()["status"] == "submitted":
        done = client_as("manager", 1).post(f"/v1/cycle-counts/{count['count_id']}/approve")

    assert done.status_code == 200, done.text
    assert level(client, peas) == (31, 0)
    assert_stock_is_consistent()


# --------------------------------------------------------------------------- #56 a batch means one expiry date


def receive(db, sku_id: int, bin_id: int, batch_no: str, expires_in: int, qty: int = 5) -> StockItem:
    return inventory.receive(db, 1, sku_id, qty, bin_id, batch_no, date.today() + timedelta(days=expires_in))


def test_a_named_batch_arriving_with_another_expiry_date_is_refused(client, db, sku):
    sugar = sku("Sugar")
    bin_id = item_of(db, sugar).bin_id
    receive(db, sugar, bin_id, "LOT-7", expires_in=2)
    db.commit()

    with pytest.raises(DomainError) as refused:
        receive(db, sugar, bin_id, "LOT-7", expires_in=300)
    db.rollback()
    same_date = receive(db, sugar, bin_id, "LOT-7", expires_in=2)
    db.commit()

    assert (refused.value.status, refused.value.code) == (409, "batch_expiry_mismatch")
    assert refused.value.details == {"batch_no": "LOT-7",
                                     "expiry_date": (date.today() + timedelta(days=2)).isoformat()}
    assert (same_date.qty, same_date.expiry_date) == (10, date.today() + timedelta(days=2))
    assert level(client, sugar) == (40, 0)
    assert_stock_is_consistent()


def test_the_same_batch_in_another_bin_must_have_the_same_date_too(db, sku):
    sugar = sku("Sugar")
    first_bin = item_of(db, sugar).bin_id
    other_bin = db.scalar(select(Bin.id).where(Bin.store_id == 1, Bin.zone == "ambient", Bin.id != first_bin))
    receive(db, sugar, first_bin, "LOT-8", expires_in=2)
    db.commit()

    with pytest.raises(DomainError) as refused:
        receive(db, sugar, other_bin, "LOT-8", expires_in=300)

    assert refused.value.code == "batch_expiry_mismatch"


def test_an_emptied_batch_number_can_come_round_again_with_a_new_date(client, db, sku):
    sugar = sku("Sugar")
    bin_id = item_of(db, sugar).bin_id
    receive(db, sugar, bin_id, "LOT-9", expires_in=2)
    db.commit()
    assert adjust(client, sugar, -35).status_code == 200            # everything sold or written off: the bin is empty

    again = receive(db, sugar, bin_id, "LOT-9", expires_in=300)
    db.commit()

    assert (again.qty, again.expiry_date) == (5, date.today() + timedelta(days=300))
    assert_stock_is_consistent()


# --------------------------------------------------------------------------- #60 who did it


def test_the_ledger_names_the_logged_in_user_not_a_name_in_the_request(client, client_as, sku):
    rice = sku("Basmati rice")

    res = client_as("manager", 1).post("/v1/stock-adjustments", json={
        "store_id": 1, "sku_id": rice, "delta": -1, "reason": "damage", "actor": "admin"})

    assert res.status_code == 200
    newest = client.get("/v1/stores/1/ledger", params={"limit": 1}).json()[0]
    assert (newest["delta"], newest["actor"]) == (-1, "test-manager")


# --------------------------------------------------------------------------- #59 reset keeps the tables


@pytest.mark.skipif(app_db.engine.dialect.name != "sqlite", reason="PostgreSQL makes other requests wait instead")
def test_reset_never_leaves_another_connection_without_its_tables(monkeypatch, db):
    def dropped(*args, **kwargs):
        raise AssertionError("reset dropped the tables: other requests would find none")
    monkeypatch.setattr(Base.metadata, "drop_all", dropped)
    db.add(Store(name="Left over", lat=1, lng=2, radius_km=3, status="open", hub_name="Hub"))
    db.commit()

    seed.reset()

    db.expire_all()
    stores = db.scalars(select(Store).order_by(Store.id)).all()
    assert "Left over" not in [store.name for store in stores]
    assert stores[0].id == 1                                       # ids start at 1 again, as the demo expects
    assert_stock_is_consistent()


def test_calling_an_adjustment_a_count_does_not_get_round_the_shelf_rules(client, db, sku):
    rice_bin = item_of(db, sku("Basmati rice")).bin_id

    res = client.post("/v1/stock-adjustments", json={"store_id": 1, "sku_id": sku("Vanilla ice cream"), "delta": 5,
                                                     "reason": "count", "bin_id": rice_bin})

    assert res.status_code == 422 and res.json()["error"]["code"] == "wrong_zone"
