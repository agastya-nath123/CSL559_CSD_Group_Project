"""Two inventory functions other modules were promised (Member 1 guide, section 5):
swap_line, for Pick & Pack's substitutes (task 2.4), and move, for Slotting's re-slotting (task 3.6).

Store 1 has 30 rice and 30 sugar (ambient), 1 vanilla ice cream and 30 frozen peas (frozen)."""
import pytest
from sqlalchemy import func, select

from app.errors import DomainError
from app.models import Bin, Reservation, StockItem, StockLedger
from app.modules.inventory import service as inventory

from .conftest import assert_stock_is_consistent, place_paid_order
from .test_cycle_counts import start, submit
from .test_order_flow import next_job


def level(client, sku_id: int) -> tuple[int, int, int]:
    row = client.get("/v1/stores/1/availability", params={"skus": str(sku_id)}).json()[0]
    return row["on_hand"], row["reserved"], row["atp"]


def paid(client, db, lines: list[tuple[int, int]]) -> Reservation:
    return db.get(Reservation, place_paid_order(client, "swap-1", 1, lines)["reservation_id"])


def lines_of(db, reservation: Reservation) -> dict[int, int]:
    db.refresh(reservation)
    return {line.sku_id: line.qty for line in reservation.lines}


# --------------------------------------------------------------------------- swap_line


def test_a_substitute_is_held_and_replaces_the_line_on_the_order(client, db, sku):
    ice_cream, peas = sku("Vanilla ice cream"), sku("Frozen green peas")
    reservation = paid(client, db, [(ice_cream, 1)])

    line = inventory.swap_line(db, reservation, ice_cream, peas, 1, old_still_held=True)
    db.commit()

    assert (line.sku_id, line.qty) == (peas, 1)
    assert lines_of(db, reservation) == {peas: 1}
    assert level(client, ice_cream) == (1, 0, 1)             # its hold was given back
    assert level(client, peas) == (30, 1, 29)
    assert_stock_is_consistent()


def test_by_default_the_old_hold_is_left_alone_because_a_dropped_line_already_freed_it(client, db, sku):
    rice, sugar = sku("Basmati rice"), sku("Sugar")
    reservation = paid(client, db, [(rice, 3)])

    inventory.swap_line(db, reservation, rice, sugar, 1)
    db.commit()

    assert lines_of(db, reservation) == {rice: 2, sugar: 1}   # only part of the line was replaced
    assert level(client, rice) == (30, 3, 27)
    assert level(client, sugar) == (30, 1, 29)


def test_a_substitute_that_just_sold_out_is_refused_and_nothing_changes(client, db, sku):
    ice_cream, peas = sku("Vanilla ice cream"), sku("Frozen green peas")
    reservation = paid(client, db, [(peas, 2)])
    client.post("/v1/reservations", json={"order_id": "last-ice-cream", "store_id": 1,
                                          "lines": [{"sku_id": ice_cream, "qty": 1}]})

    with pytest.raises(DomainError) as refused:
        inventory.swap_line(db, reservation, peas, ice_cream, 1, old_still_held=True)
    db.rollback()

    assert (refused.value.status, refused.value.code) == (409, "insufficient_stock")
    assert refused.value.details == {"lines": [{"sku_id": ice_cream, "requested": 1, "available": 0}]}
    assert lines_of(db, reservation) == {peas: 2}
    assert level(client, peas) == (30, 2, 28)
    assert_stock_is_consistent()


def test_only_a_paid_order_and_only_units_it_has_can_be_swapped(client, db, sku):
    rice, sugar = sku("Basmati rice"), sku("Sugar")
    held = client.post("/v1/reservations", json={"order_id": "unpaid", "store_id": 1,
                                                 "lines": [{"sku_id": rice, "qty": 1}]}).json()
    reservation = paid(client, db, [(rice, 1)])

    def refusal(*args) -> str:
        with pytest.raises(DomainError) as refused:
            inventory.swap_line(db, *args)
        return refused.value.code

    assert refusal(db.get(Reservation, held["reservation_id"]), rice, sugar, 1) == "not_paid"
    assert refusal(reservation, rice, sugar, 2) == "not_on_order"
    assert refusal(reservation, sugar, rice, 1) == "not_on_order"
    assert refusal(reservation, rice, rice, 1) == "same_sku"
    assert refusal(reservation, rice, sugar, 0) == "bad_quantity"
    assert level(client, sugar) == (30, 0, 30)


# --------------------------------------------------------------------------- move


def rice_and_bins(db, sku) -> tuple[StockItem, list[Bin]]:
    item = db.scalar(select(StockItem).where(StockItem.store_id == 1, StockItem.sku_id == sku("Basmati rice")))
    others = db.scalars(select(Bin).where(Bin.store_id == 1, Bin.zone == "ambient", Bin.id != item.bin_id)
                        .order_by(Bin.walk_seq)).all()
    return item, others


def test_moving_units_changes_the_bins_but_not_the_stock(client, db, sku):
    item, others = rice_and_bins(db, sku)
    before = level(client, item.sku_id)

    target = inventory.move(db, item, others[0].id, 10, reference="reslot", actor="manager1")
    again = inventory.move(db, item, others[0].id, 5)             # the batch is already there: same row
    db.commit()

    assert (item.qty, target.qty, again.id) == (15, 15, target.id)
    assert (target.batch_no, target.expiry_date) == (item.batch_no, item.expiry_date)
    assert level(client, item.sku_id) == before
    moves = db.execute(select(StockLedger.bin_id, func.sum(StockLedger.delta))
                       .where(StockLedger.reason == "move").group_by(StockLedger.bin_id)).all()
    assert sorted(moves) == sorted([(item.bin_id, -15), (others[0].id, 15)])
    assert_stock_is_consistent()


def test_a_move_to_the_wrong_zone_or_another_store_is_refused(db, sku):
    item, _others = rice_and_bins(db, sku)
    frozen = db.scalar(select(Bin.id).where(Bin.store_id == 1, Bin.zone == "frozen"))
    elsewhere = db.scalar(select(Bin.id).where(Bin.store_id == 2, Bin.zone == "ambient"))

    def refusal(bin_id: int, qty: int = 1) -> str:
        with pytest.raises(DomainError) as refused:
            inventory.move(db, item, bin_id, qty)
        return refused.value.code

    assert refusal(frozen) == "wrong_zone"
    assert refusal(elsewhere) == "bad_bin"
    assert refusal(item.bin_id) == "same_bin"
    assert refusal(frozen, 0) == "bad_quantity"
    assert item.qty == 30


def test_units_a_picker_has_been_sent_for_cannot_be_moved_away(client, db, sku):
    item, others = rice_and_bins(db, sku)
    place_paid_order(client, "pick-me", 1, [(item.sku_id, 25)])
    next_job(client)                                            # a picker is now walking to this bin for 25

    with pytest.raises(DomainError) as refused:
        inventory.move(db, item, others[0].id, 6)
    assert refused.value.code == "promised_to_picker"

    inventory.move(db, item, others[0].id, 5)                   # the 5 nobody is waiting for
    db.commit()
    assert item.qty == 25
    assert_stock_is_consistent()


def test_stock_cannot_move_out_of_or_into_a_bin_that_is_being_counted(client, db, sku):
    """Found in the review of #70: a count taken before a move, handed in after it, added the moved units a second
    time (30 rice became 60). A count compares with the records at the moment it is handed in."""
    rice, sugar = sku("Basmati rice"), sku("Sugar")
    item, others = rice_and_bins(db, sku)
    sugar_item = db.scalar(select(StockItem).where(StockItem.store_id == 1, StockItem.sku_id == sugar))
    count = start(client, rice)                                  # someone is counting the rice bin

    def refusal(moving: StockItem, to_bin_id: int) -> str:
        with pytest.raises(DomainError) as refused:
            inventory.move(db, moving, to_bin_id, 5)
        return refused.value.code

    assert refusal(item, others[0].id) == "bin_being_counted"    # out of it
    assert refusal(sugar_item, item.bin_id) == "bin_being_counted"   # into it
    assert submit(client, count, {rice: 30}).status_code == 200  # handed in: nothing differed
    inventory.move(db, item, others[0].id, 5)
    db.commit()
    assert level(client, rice) == (30, 0, 30)
    assert_stock_is_consistent()
