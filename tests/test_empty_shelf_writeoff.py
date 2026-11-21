"""Issue #61: a bin the picker found empty is empty. `report_short` used to write off only the units the picker's own
line needed, so the rest of the bin stayed in ATP and customers kept buying stock that was not there until the bin
was counted.

Now the rest of the bin goes too (`inventory.write_off_unpromised`), except units another unpacked order is waiting
for, and never below what is held on the item, so the stock audit stays clean all the way through. Those units go
when their own picker reports the same empty bin.

Store 1 has 30 sugar in one bin; milk is 6 in batch M-OLD and 24 in M-NEW, in two bins."""
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import StockItem, StockLedger

from .conftest import assert_stock_is_consistent, place_paid_order
from .test_order_flow import next_job


def stock(client, sku_id: int) -> tuple[int, int, int]:
    """(on hand, held, ATP)"""
    row = client.get("/v1/stores/1/availability", params={"skus": str(sku_id)}).json()[0]
    return row["on_hand"], row["reserved"], row["atp"]


def short(client, picker: str = "picker-1", found: int | None = None) -> dict:
    """Claim the oldest paid order and report its first line short."""
    line = next_job(client, picker)["tasks"][0]
    res = client.post(f"/v1/pick-tasks/{line['task_id']}/short", json={"found": found} if found is not None else None)
    assert res.status_code == 200, res.text
    return res.json()


def hold(client, order_id: str, sku_id: int, qty: int):
    return client.post("/v1/reservations", json={"order_id": order_id, "store_id": 1,
                                                 "lines": [{"sku_id": sku_id, "qty": qty}]})


def not_found_rows(sku_id: int) -> list[tuple[int, str, int]]:
    """(bin, reference, units) of every `not_found` ledger row for this item."""
    with SessionLocal() as session:
        rows = session.execute(select(StockLedger.bin_id, StockLedger.reference, StockLedger.delta)
                               .where(StockLedger.store_id == 1, StockLedger.sku_id == sku_id,
                                      StockLedger.reason == "not_found")).all()
    return [tuple(row) for row in rows]


def written_off(sku_id: int) -> int:
    return -sum(delta for _bin, _reference, delta in not_found_rows(sku_id))


def batch_qty(batch_no: str) -> int:
    with SessionLocal() as session:
        return session.scalar(select(func.sum(StockItem.qty))
                              .where(StockItem.store_id == 1, StockItem.batch_no == batch_no))


def test_the_issue_one_sugar_not_on_shelf_and_no_more_sugar_can_be_bought(client, sku):
    sugar = sku("Sugar")
    place_paid_order(client, "o-1", 1, [(sugar, 1)])

    result = short(client)

    assert (result["reallocated"], result["dropped"]) == (0, 1)
    assert stock(client, sugar) == (0, 0, 0)                     # was (29, 0, 29) before the fix
    refused = hold(client, "o-2", sugar, 1)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "insufficient_stock"

    # Every unit is accounted for in the ledger, on the bin and job the count of that bin will look at.
    rows = not_found_rows(sugar)
    assert written_off(sugar) == 30
    assert {reference for _bin, reference, _delta in rows} == {f"job:{result['job']['job_id']}"}
    assert len({bin_id for bin_id, _reference, _delta in rows}) == 1
    assert_stock_is_consistent()


def test_units_another_paid_order_is_waiting_for_stay_until_its_picker_reports_them(client, sku):
    sugar = sku("Sugar")
    place_paid_order(client, "o-1", 1, [(sugar, 1)])
    place_paid_order(client, "o-2", 1, [(sugar, 2)])             # same bin, not claimed yet

    short(client, "picker-1")                                    # o-1

    # Taking o-2's 2 as well would leave 2 held and 0 on hand: the audit's reserved_over_on_hand.
    assert stock(client, sugar) == (2, 2, 0)
    assert_stock_is_consistent()

    second = short(client, "picker-2")                           # o-2's picker finds the same empty bin
    assert (second["reallocated"], second["dropped"]) == (0, 2)
    assert stock(client, sugar) == (0, 0, 0)
    assert written_off(sugar) == 30
    assert_stock_is_consistent()


def test_a_partial_find_keeps_the_found_units_and_the_rest_of_the_bin_goes(client, sku):
    sugar = sku("Sugar")
    place_paid_order(client, "o-1", 1, [(sugar, 3)])

    result = short(client, found=1)

    assert (result["picked"], result["reallocated"], result["dropped"]) == (1, 0, 2)
    assert stock(client, sugar) == (1, 1, 0)                     # only the unit in the tote is left, still held
    packed = client.post(f"/v1/fulfilment-jobs/{result['job']['job_id']}/packed", json={"crate_slot": "S-01"})
    assert packed.status_code == 200, packed.text
    assert stock(client, sugar) == (0, 0, 0)
    assert written_off(sugar) == 29                              # 30 - the 1 sold
    assert_stock_is_consistent()


def test_an_unpaid_hold_is_never_left_holding_more_than_is_on_hand(client, sku):
    """Unpaid holds are on the item, not on a bin, so their units stay in the count (ATP 0) until that order is paid
    and its picker finds the bin empty too."""
    sugar = sku("Sugar")
    place_paid_order(client, "o-1", 1, [(sugar, 1)])
    cart = hold(client, "cart", sugar, 5)
    assert cart.status_code == 201, cart.text

    short(client)

    assert stock(client, sugar) == (5, 5, 0)
    assert_stock_is_consistent()

    paid = client.post(f"/v1/reservations/{cart.json()['reservation_id']}/confirm")
    assert paid.status_code == 200, paid.text
    later = short(client, "picker-2")
    assert (later["reallocated"], later["dropped"]) == (0, 5)
    assert stock(client, sugar) == (0, 0, 0)
    assert_stock_is_consistent()


def test_a_line_moved_to_another_bin_still_empties_the_bin_it_left(client, sku):
    milk = sku("Toned milk")
    place_paid_order(client, "o-1", 1, [(milk, 4)])              # all 4 from M-OLD

    result = short(client)

    assert (result["reallocated"], result["dropped"]) == (4, 0)
    assert (batch_qty("M-OLD"), batch_qty("M-NEW")) == (0, 24)   # M-OLD's other 2 were not there either
    assert stock(client, milk)[:2] == (24, 4)
    assert_stock_is_consistent()
