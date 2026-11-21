"""Inventory Service - the single source of truth for stock (HLD section 5, service 3).

Every change to StockLevel or StockItem goes through a function in this file, and every
change writes a StockLedger row. Other modules call these functions; they never write to
the stock tables themselves.

Functions whose name starts with an underscore, and the ones documented as "does not commit",
leave the transaction open so the caller can combine several steps and commit once.
"""
import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ... import config, events, metrics
from ...cache import atp_cache
from ...errors import DomainError, not_found
from ...models import (SKU, Bin, CycleCount, EventLog, FulfilmentJob, PickTask, Reservation, ReservationLine, StockItem,
                       StockLedger, StockLevel, Store, utcnow)

# --------------------------------------------------------------------------- lookups


def get_store(db: Session, store_id: int) -> Store:
    store = db.get(Store, store_id)
    if store is None:
        raise not_found("Store", store_id)
    return store


def get_sku(db: Session, sku_id: int) -> SKU:
    sku = db.get(SKU, sku_id)
    if sku is None:
        raise not_found("SKU", sku_id)
    return sku


def get_level(db: Session, store_id: int, sku_id: int) -> StockLevel | None:
    """Read the StockLevel row straight from the database, ignoring any cached copy."""
    return db.scalar(
        select(StockLevel)
        .where(StockLevel.store_id == store_id, StockLevel.sku_id == sku_id)
        .execution_options(populate_existing=True)
    )


def get_reservation(db: Session, reservation_id: str) -> Reservation:
    res = db.get(Reservation, reservation_id)
    if res is None:
        raise not_found("Reservation", reservation_id)
    return res


# --------------------------------------------------------------------------- availability (FR-2)


def atp_of(level: StockLevel) -> int:
    """What customers may buy (HLD 7.1): on hand - reserved - safety buffer, never below 0.
    Use this wherever a number means "can a customer have it". Replenishment and Pick & Pack use the physical
    stock instead (on hand - reserved), because the buffer units are really on the shelf."""
    return max(0, level.qty_on_hand - level.qty_reserved - level.safety_buffer)


def availability(db: Session, store_id: int, sku_ids: list[int] | None = None) -> list[dict]:
    """ATP (available to promise) per SKU for one store. ATP = on hand - reserved - safety buffer.

    The whole-store list (no `sku_ids`) is served from the ATP cache for up to ATP_CACHE_SECONDS; it is cleared
    whenever that store's stock changes (app/cache.py). For showing only: `reserve` always checks the database."""
    if sku_ids is None:
        cached = atp_cache.get(store_id)
        if cached is not None:
            metrics.ATP_CACHE.labels("hit").inc()
            return [dict(row) for row in cached]          # copies, so a caller cannot change the cached list
        metrics.ATP_CACHE.labels("miss").inc()
    get_store(db, store_id)
    query = select(StockLevel).where(StockLevel.store_id == store_id)
    if sku_ids:
        query = query.where(StockLevel.sku_id.in_(sku_ids))
    rows = db.scalars(query.execution_options(populate_existing=True)).all()
    out = [
        {
            "sku_id": r.sku_id,
            "name": r.sku.name,
            "category": r.sku.category,
            "barcode": r.sku.barcode,
            "price": r.sku.price,
            "on_hand": r.qty_on_hand,
            "reserved": r.qty_reserved,
            "safety_buffer": r.safety_buffer,
            "atp": atp_of(r),
            "reorder_point": r.reorder_point,
            "target_stock": r.target_stock,
        }
        for r in rows
    ]
    out.sort(key=lambda row: row["name"])
    if sku_ids is None:
        atp_cache.set(store_id, [dict(row) for row in out])
    return out


def set_safety_buffer(db: Session, store_id: int, sku_id: int, safety_buffer: int) -> dict:
    """Change how many units of one SKU are hidden from customers. Moves no stock, so it writes no ledger row.
    Holds that already exist are kept even if ATP is now below them; only new holds see the new buffer."""
    get_store(db, store_id)
    get_sku(db, sku_id)
    changed = db.execute(
        update(StockLevel)
        .where(StockLevel.store_id == store_id, StockLevel.sku_id == sku_id)
        .values(safety_buffer=safety_buffer, version=StockLevel.version + 1)
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed == 0:
        raise DomainError(404, "not_stocked", f"Store {store_id} does not stock SKU {sku_id}")
    db.commit()
    level = get_level(db, store_id, sku_id)
    return {"store_id": store_id, "sku_id": sku_id, "on_hand": level.qty_on_hand, "reserved": level.qty_reserved,
            "safety_buffer": level.safety_buffer, "atp": atp_of(level)}


# --------------------------------------------------------------------------- reservations (FR-3)


def _retry_of(existing: Reservation, store_id: int, wanted: dict[int, int]) -> Reservation:
    """The hold an order id already has, if this call asks for that same order again (a retry). Anything else is a
    different order using the id, and gets no look at the first one: its reply would carry the reservation id, which
    is all that confirm and release ask for."""
    if existing.store_id != store_id or {line.sku_id: line.qty for line in existing.lines} != wanted:
        raise DomainError(409, "order_id_reused", "This order id already belongs to a different order; use a new one")
    return existing


def reserve(db: Session, order_id: str, store_id: int, lines: list[tuple[int, int]],
            h3_cell: str | None = None) -> Reservation:
    """Hold stock for a whole cart, or for none of it. `h3_cell` (optional) is the H3 cell of the delivery
    address, kept for network planning.

    The no-overselling rule from HLD 9.1 lives here: each line is one conditional UPDATE that
    only succeeds if enough unreserved stock exists at that instant. If any line fails, the whole
    transaction is rolled back, so a cart is held whole or not at all.

    `order_id` is the idempotency key: calling this twice with the same order returns the first hold. "The same
    order" means the same store and the same lines; an order id that is already used for anything else is refused
    (409 order_id_reused), so nobody is handed another customer's hold.
    """
    wanted: dict[int, int] = {}
    for sku_id, qty in lines:
        if qty <= 0:
            raise DomainError(422, "bad_quantity", "Every line needs a quantity of at least 1")
        wanted[sku_id] = wanted.get(sku_id, 0) + qty
    if not wanted:
        raise DomainError(422, "empty_cart", "A reservation needs at least one line")

    existing = db.scalar(select(Reservation).where(Reservation.order_id == order_id))
    if existing is not None:
        return _retry_of(existing, store_id, wanted)
    get_store(db, store_id)

    failed: list[int] = []
    for sku_id, qty in wanted.items():
        result = db.execute(
            update(StockLevel)
            .where(
                StockLevel.store_id == store_id,
                StockLevel.sku_id == sku_id,
                StockLevel.qty_on_hand - StockLevel.qty_reserved - StockLevel.safety_buffer >= qty,
            )
            .values(qty_reserved=StockLevel.qty_reserved + qty, version=StockLevel.version + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:      # 0 rows updated = not enough stock -> reject
            failed.append(sku_id)

    if failed:
        db.rollback()
        metrics.RESERVATIONS.labels("insufficient_stock").inc()
        details = []
        for sku_id in failed:
            level = get_level(db, store_id, sku_id)
            details.append({
                "sku_id": sku_id,
                "requested": wanted[sku_id],
                "available": atp_of(level) if level else 0,
            })
        raise DomainError(409, "insufficient_stock", "Not enough stock for one or more items",
                          {"lines": details})

    reservation = Reservation(
        id=str(uuid.uuid4()),
        order_id=order_id,
        store_id=store_id,
        h3_cell=h3_cell,
        status="held",
        expires_at=utcnow() + timedelta(minutes=config.RESERVATION_TTL_MINUTES),
        lines=[ReservationLine(sku_id=s, qty=q) for s, q in wanted.items()],
    )
    db.add(reservation)
    try:
        db.flush()
    except IntegrityError:            # the same order_id arrived twice at the same moment
        db.rollback()
        return _retry_of(db.scalar(select(Reservation).where(Reservation.order_id == order_id)), store_id, wanted)
    for sku_id in wanted:
        events.publish(db, "stock.changed", store_id, {"sku_id": sku_id, "cause": "reserved"})
    db.commit()
    metrics.RESERVATIONS.labels("held").inc()
    return reservation


def confirm(db: Session, reservation_id: str) -> Reservation:
    """Payment succeeded: turn the hold into a paid order. Publishes order.paid, which the
    Fulfilment Orchestrator listens to."""
    res = get_reservation(db, reservation_id)
    if res.status in ("confirmed", "fulfilled"):
        return res                                            # safe to retry
    if res.status != "held":
        raise DomainError(409, "not_held", f"Reservation is {res.status}, so it cannot be confirmed")
    if res.expires_at < utcnow():
        if _release(db, res, "expired"):
            db.commit()
            raise DomainError(409, "hold_expired", "The 10-minute hold ran out; the items are back on sale")
        return _lost_race_to_confirm(db, res)
    # Conditional, like `reserve`: if the sweeper or a cancel changed the hold since we read it, this changes nothing,
    # so a payment landing at the same instant as the expiry either wins cleanly or loses cleanly.
    if not _change_status(db, res, "held", "confirmed"):
        return _lost_race_to_confirm(db, res)
    events.publish(db, "order.paid", res.store_id, {"order_id": res.order_id, "reservation_id": res.id})
    db.commit()
    return res


def _lost_race_to_confirm(db: Session, res: Reservation) -> Reservation:
    """Another request changed the hold while `confirm` was working. A payment that already went through is fine
    (retries are safe); anything else is reported as the state the hold ended in."""
    db.rollback()
    db.refresh(res)
    if res.status in ("confirmed", "fulfilled"):
        return res
    if res.status == "expired":
        raise DomainError(409, "hold_expired", "The 10-minute hold ran out; the items are back on sale")
    raise DomainError(409, "not_held", f"Reservation is {res.status}, so it cannot be confirmed")


# A reservation in one of these states has nothing held any more: released or expired before payment, or a paid
# order cancelled before packing (fulfilment.cancel). Releasing it again changes nothing.
GIVEN_BACK = ("released", "expired", "cancelled")


def release(db: Session, reservation_id: str) -> Reservation:
    """Payment failed or the customer cancelled before paying: give the stock back."""
    res = get_reservation(db, reservation_id)
    if res.status in GIVEN_BACK:
        return res                                            # safe to retry
    if res.status != "held":
        raise DomainError(409, "already_paid",
                          "This order is already paid. Cancel it with POST /v1/fulfilment-jobs/{job_id}/cancel.")
    if not _release(db, res, "released"):
        db.rollback()                         # someone else got there first; report what they did
        db.refresh(res)
        if res.status not in GIVEN_BACK:
            raise DomainError(409, "already_paid",
                              "This order is already paid. Cancel it with POST /v1/fulfilment-jobs/{job_id}/cancel.")
        return res
    db.commit()
    return res


def sweep_expired(db: Session) -> int:
    """Release every hold whose time has run out. Returns how many this call released; holds that a cancel,
    a payment or another sweeper got to first are not counted. Runs on a timer (app/background.py)."""
    stale = db.scalars(
        select(Reservation).where(Reservation.status == "held", Reservation.expires_at < utcnow())
    ).all()
    released = sum(1 for res in stale if _release(db, res, "expired"))
    db.commit()
    return released


def _change_status(db: Session, res: Reservation, old: str, new: str) -> bool:
    """Move a reservation from `old` to `new` only if it is still `old` in the database. Returns False, and changes
    nothing, when another request changed it first. Does not commit."""
    changed = db.execute(
        update(Reservation)
        .where(Reservation.id == res.id, Reservation.status == old)
        .values(status=new)
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed:
        res.status = new
    return changed == 1


def _release(db: Session, res: Reservation, new_status: str) -> bool:
    """Give a held reservation's stock back. Returns False, and gives nothing back, if the hold was already released,
    expired or confirmed by someone else (the sweeper and a customer's cancel can race). Does not commit."""
    if not _change_status(db, res, "held", new_status):
        return False
    for line in res.lines:
        unreserve(db, res.store_id, line.sku_id, line.qty)
    db.flush()
    for line in res.lines:
        events.publish(db, "stock.changed", res.store_id, {"sku_id": line.sku_id, "cause": new_status})
    return True


def unreserve(db: Session, store_id: int, sku_id: int, qty: int) -> None:
    """Give `qty` held units back to ATP. Does not commit."""
    db.execute(
        update(StockLevel)
        .where(StockLevel.store_id == store_id, StockLevel.sku_id == sku_id)
        .values(qty_reserved=StockLevel.qty_reserved - qty, version=StockLevel.version + 1)
        .execution_options(synchronize_session=False)
    )


def swap_line(db: Session, reservation: Reservation, old_sku_id: int, new_sku_id: int, qty: int,
              old_still_held: bool = False) -> ReservationLine:
    """Replace `qty` units of one SKU on a paid order with a substitute (Pick & Pack, task 2.4). Does not commit,
    so the caller can add the pick tasks for the substitute in the same transaction.

    The substitute is held with the same conditional UPDATE as `reserve`, so it can never be oversold: if it is
    no longer available this raises 409 insufficient_stock before anything else has changed. Then the order's
    lines are rewritten: `qty` fewer of the old SKU, `qty` more of the new one.

    The hold on the old units is normally gone already (Pick & Pack frees it when it drops a line). Pass
    `old_still_held=True` to give it back here instead. Returns the substitute's reservation line."""
    if qty <= 0:
        raise DomainError(422, "bad_quantity", "A substitution needs a quantity of at least 1")
    if old_sku_id == new_sku_id:
        raise DomainError(422, "same_sku", "The substitute must be a different product")
    if reservation.status != "confirmed":
        raise DomainError(409, "not_paid", f"Reservation is {reservation.status}; only a paid order that is not "
                                           "packed yet can take a substitute")
    get_sku(db, new_sku_id)
    old_line = next((line for line in reservation.lines if line.sku_id == old_sku_id), None)
    if old_line is None or old_line.qty < qty:
        raise DomainError(409, "not_on_order", f"The order does not have {qty} units of SKU {old_sku_id} to replace")

    held = db.execute(
        update(StockLevel)
        .where(StockLevel.store_id == reservation.store_id, StockLevel.sku_id == new_sku_id,
               StockLevel.qty_on_hand - StockLevel.qty_reserved - StockLevel.safety_buffer >= qty)
        .values(qty_reserved=StockLevel.qty_reserved + qty, version=StockLevel.version + 1)
        .execution_options(synchronize_session=False)
    ).rowcount
    if held == 0:
        level = get_level(db, reservation.store_id, new_sku_id)
        raise DomainError(409, "insufficient_stock", "The substitute is no longer available",
                          {"lines": [{"sku_id": new_sku_id, "requested": qty,
                                      "available": atp_of(level) if level else 0}]})
    if old_still_held:
        unreserve(db, reservation.store_id, old_sku_id, qty)
        events.publish(db, "stock.changed", reservation.store_id, {"sku_id": old_sku_id, "cause": "substituted"})

    old_line.qty -= qty
    if old_line.qty == 0:
        reservation.lines.remove(old_line)
    new_line = next((line for line in reservation.lines if line.sku_id == new_sku_id), None)
    if new_line is None:
        new_line = ReservationLine(sku_id=new_sku_id, qty=0)
        reservation.lines.append(new_line)
    new_line.qty += qty
    db.flush()
    events.publish(db, "stock.changed", reservation.store_id, {"sku_id": new_sku_id, "cause": "substituted"})
    return new_line


# --------------------------------------------------------------------------- stock movements


def _ledger(db: Session, store_id: int, sku_id: int, bin_id: int | None, delta: int, reason: str,
            reference: str | None, actor: str) -> None:
    db.add(StockLedger(store_id=store_id, sku_id=sku_id, bin_id=bin_id, delta=delta, reason=reason,
                       reference=reference, actor=actor))


def _change_on_hand(db: Session, store_id: int, sku_id: int, delta: int, reserved_delta: int = 0) -> None:
    """Apply a change to the StockLevel totals, creating the row the first time a SKU is stocked."""
    if get_level(db, store_id, sku_id) is None:
        db.add(StockLevel(store_id=store_id, sku_id=sku_id, qty_on_hand=0, qty_reserved=0))
        db.flush()
    db.execute(
        update(StockLevel)
        .where(StockLevel.store_id == store_id, StockLevel.sku_id == sku_id)
        .values(
            qty_on_hand=StockLevel.qty_on_hand + delta,
            qty_reserved=StockLevel.qty_reserved + reserved_delta,
            version=StockLevel.version + 1,
        )
        .execution_options(synchronize_session=False)
    )


def _change_item_qty(db: Session, item: StockItem, delta: int) -> None:
    """Add `delta` units to one batch in one bin (a negative delta removes them). Does not commit.

    One UPDATE that computes the new quantity inside the database, like the hold in `reserve`. Reading item.qty,
    changing it in Python and writing it back would lose one of two changes made at the same moment (two packers
    taking from the same bin): PostgreSQL lets both read the old number. Taking more units than the bin holds
    changes nothing and raises 409 stock_mismatch."""
    db.flush()                                   # a batch created a moment ago needs its row first
    changed = db.execute(
        update(StockItem)
        .where(StockItem.id == item.id, StockItem.qty + delta >= 0)
        .values(qty=StockItem.qty + delta)
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed == 0:
        raise DomainError(409, "stock_mismatch",
                          f"The bin no longer holds {-delta} units of batch {item.batch_no}; read the stock again")
    db.refresh(item, ["qty"])


def receive(db: Session, store_id: int, sku_id: int, qty: int, bin_id: int, batch_no: str,
            expiry_date: date, reference: str | None = None, actor: str = "receiver") -> StockItem:
    """Put received stock into a bin (FR-9). The units are sellable as soon as this returns.
    Does not commit.

    A batch number means one expiry date. If units of this batch are already in the store with another date, the
    delivery is refused (409 batch_expiry_mismatch): added to that batch, the new units would be sold (FEFO) and
    written off by the old date. An emptied batch whose number comes round again takes the new date."""
    if qty <= 0:
        raise DomainError(422, "bad_quantity", "Received quantity must be at least 1")
    sku = get_sku(db, sku_id)
    bin_ = db.get(Bin, bin_id)
    if bin_ is None or bin_.store_id != store_id:
        raise DomainError(422, "bad_bin", f"Bin {bin_id} is not in store {store_id}")
    if bin_.zone != sku.storage_type:
        raise DomainError(422, "wrong_zone",
                          f"{sku.name} needs {sku.storage_type} storage but bin {bin_.code} is {bin_.zone}")
    other_date = db.scalar(
        select(StockItem.expiry_date).where(
            StockItem.store_id == store_id, StockItem.sku_id == sku_id, StockItem.batch_no == batch_no,
            StockItem.qty > 0, StockItem.expiry_date != expiry_date,
        ).limit(1)
    )
    if other_date is not None:
        raise DomainError(409, "batch_expiry_mismatch",
                          f"Batch {batch_no} of {sku.name} is already in this store with expiry date "
                          f"{other_date.isoformat()}, not {expiry_date.isoformat()}; give this delivery its own "
                          "batch number", {"batch_no": batch_no, "expiry_date": other_date.isoformat()})
    item = db.scalar(
        select(StockItem).where(
            StockItem.store_id == store_id, StockItem.sku_id == sku_id,
            StockItem.bin_id == bin_id, StockItem.batch_no == batch_no,
        )
    )
    if item is None:
        item = StockItem(store_id=store_id, sku_id=sku_id, bin_id=bin_id, batch_no=batch_no,
                         expiry_date=expiry_date, qty=0)
        db.add(item)
    else:
        item.expiry_date = expiry_date           # the same date, or an emptied batch number used again
    _change_item_qty(db, item, +qty)
    _change_on_hand(db, store_id, sku_id, +qty)
    _ledger(db, store_id, sku_id, bin_id, +qty, "receipt", reference, actor)
    events.publish(db, "stock.changed", store_id, {"sku_id": sku_id, "cause": "receipt"})
    return item


def move(db: Session, item: StockItem, to_bin_id: int, qty: int, reference: str | None = None,
         actor: str = "system") -> StockItem:
    """Move `qty` units of one batch to another bin in the same store (re-slotting, Slotting task 3.6). Does not
    commit. Nothing is gained or lost: two ledger rows with reason `move` (out of one bin, into the other) that sum
    to zero, and qty_on_hand is untouched, so ATP does not change.

    Refused: a bin of the wrong temperature zone (422 wrong_zone), units a picker has been sent to this bin for
    (409 promised_to_picker), and a move out of or into a bin with an open cycle count (409 bin_being_counted): the
    counter's numbers are compared with the records when they are handed in, so units moved in between would be
    added a second time or written off. Bin capacity is Slotting's rule, so the caller checks it. Returns the
    batch's row in the new bin."""
    if qty <= 0:
        raise DomainError(422, "bad_quantity", "Moved quantity must be at least 1")
    to_bin = db.get(Bin, to_bin_id)
    if to_bin is None or to_bin.store_id != item.store_id:
        raise DomainError(422, "bad_bin", f"Bin {to_bin_id} is not in store {item.store_id}")
    if to_bin.id == item.bin_id:
        raise DomainError(422, "same_bin", "The units are already in that bin")
    sku = get_sku(db, item.sku_id)
    if to_bin.zone != sku.storage_type:
        raise DomainError(422, "wrong_zone",
                          f"{sku.name} needs {sku.storage_type} storage but bin {to_bin.code} is {to_bin.zone}")
    counted = db.scalar(select(Bin.code).join(CycleCount, CycleCount.bin_id == Bin.id)
                        .where(CycleCount.bin_id.in_((item.bin_id, to_bin.id)), CycleCount.status == "open").limit(1))
    if counted is not None:
        raise DomainError(409, "bin_being_counted",
                          f"Bin {counted} is being counted; move the stock after the count is handed in")
    db.refresh(item, ["qty"])
    free = item.qty - _promised(db, item)
    if qty > free:
        raise DomainError(409, "promised_to_picker",
                          f"Only {max(free, 0)} units in this bin are free to move; the rest are in an order "
                          "being picked or are not there")
    target = db.scalar(
        select(StockItem).where(StockItem.store_id == item.store_id, StockItem.sku_id == item.sku_id,
                                StockItem.bin_id == to_bin.id, StockItem.batch_no == item.batch_no))
    if target is None:
        target = StockItem(store_id=item.store_id, sku_id=item.sku_id, bin_id=to_bin.id, batch_no=item.batch_no,
                           expiry_date=item.expiry_date, qty=0)
        db.add(target)
    _change_item_qty(db, item, -qty)
    _change_item_qty(db, target, +qty)
    _ledger(db, item.store_id, item.sku_id, item.bin_id, -qty, "move", reference, actor)
    _ledger(db, item.store_id, item.sku_id, to_bin.id, +qty, "move", reference, actor)
    return target


def adjust(db: Session, store_id: int, sku_id: int, delta: int, reason: str, bin_id: int | None = None,
           actor: str = "manager", reference: str | None = None, commit: bool = True,
           shelf_rules: bool | None = None) -> dict:
    """Correct the stock count (FR-11): damage, expiry, theft, or a cycle-count difference.

    Negative delta removes units (from `bin_id` if given, otherwise earliest-expiry first). Units already scanned
    into a tote are never taken, as in expire_stock and write_off_missing: they are off the shelf, and their order
    could not be packed without them (409 in_tote when the shelf alone does not have enough).
    Positive delta adds found units and needs a `bin_id`. The bin must be of the SKU's temperature zone
    (422 wrong_zone) and have room (409 bin_full), as for a goods receipt. A cycle count is exempt: it records what
    is really on the shelf, even where it should not be. So `shelf_rules` is on by default for every reason but
    `count`; the stock-adjustment endpoint always passes True, whatever reason the request gives.
    `commit=False` leaves the transaction open, so the caller can save an idempotency record with it.
    """
    if delta == 0:
        raise DomainError(422, "bad_quantity", "delta cannot be 0")
    if shelf_rules is None:
        shelf_rules = reason != "count"
    get_store(db, store_id)
    sku = get_sku(db, sku_id)

    if delta > 0:
        if bin_id is None:
            raise DomainError(422, "bin_required", "Adding stock needs a bin_id")
        bin_ = db.get(Bin, bin_id)
        if bin_ is None or bin_.store_id != store_id:
            raise DomainError(422, "bad_bin", f"Bin {bin_id} is not in store {store_id}")
        if shelf_rules:
            if bin_.zone != sku.storage_type:
                raise DomainError(422, "wrong_zone",
                                  f"{sku.name} needs {sku.storage_type} storage but bin {bin_.code} is {bin_.zone}")
            from ..slotting import service as slotting      # imported here: slotting imports this module
            slotting.ensure_room(db, store_id, bin_id, delta)
        item = db.scalar(
            select(StockItem).where(StockItem.store_id == store_id, StockItem.sku_id == sku_id,
                                    StockItem.bin_id == bin_id).order_by(StockItem.expiry_date)
        )
        if item is None:
            item = StockItem(store_id=store_id, sku_id=sku_id, bin_id=bin_id, batch_no="ADJ",
                             expiry_date=date.today() + timedelta(days=sku.shelf_life_days), qty=0)
            db.add(item)
        _change_item_qty(db, item, delta)
        _ledger(db, store_id, sku_id, bin_id, delta, reason, reference, actor)
    else:
        to_remove = -delta
        query = select(StockItem).where(StockItem.store_id == store_id, StockItem.sku_id == sku_id,
                                        StockItem.qty > 0)
        if bin_id is not None:
            query = query.where(StockItem.bin_id == bin_id)
        items = db.scalars(query.order_by(StockItem.expiry_date)).all()
        if sum(i.qty for i in items) < to_remove:
            raise DomainError(409, "not_enough_stock", "Cannot remove more units than are on the shelf")
        on_shelf = {i.id: max(0, i.qty - _in_totes(db, i)) for i in items}
        if sum(on_shelf.values()) < to_remove:
            raise DomainError(409, "in_tote",
                              f"Only {sum(on_shelf.values())} of those units are still on the shelf; the rest are in "
                              "a picker's tote for an order that is not packed yet",
                              {"on_shelf": sum(on_shelf.values())})
        for item in items:
            take = min(on_shelf[item.id], to_remove)
            if take == 0:
                continue
            _remove_from_item(db, item, take, reason, reference, actor, change_on_hand=False)
            to_remove -= take

    db.flush()
    _change_on_hand(db, store_id, sku_id, delta)
    events.publish(db, "stock.changed", store_id, {"sku_id": sku_id, "cause": reason})
    if commit:
        db.commit()
    level = get_level(db, store_id, sku_id)
    return {"sku_id": sku_id, "on_hand": level.qty_on_hand, "reserved": level.qty_reserved,
            "atp": atp_of(level)}


def _remove_from_item(db: Session, item: StockItem, qty: int, reason: str, reference: str | None, actor: str,
                      change_on_hand: bool = True) -> None:
    """Take `qty` units out of one exact batch in one bin and write the ledger row. With `change_on_hand=False` the
    caller lowers StockLevel itself (once, for several items). Does not commit."""
    _change_item_qty(db, item, -qty)
    _ledger(db, item.store_id, item.sku_id, item.bin_id, -qty, reason, reference, actor)
    if change_on_hand:
        _change_on_hand(db, item.store_id, item.sku_id, -qty)


# --------------------------------------------------------------------------- expiry (FR-10)


# Units of a pick task that are already in a tote: all of a picked line, and the units scanned so far of a line
# that is still being scanned one at a time (Pick & Pack task 2.2). Used with PickTask.status in TOTE_STATUSES.
TOTE_UNITS = case((PickTask.status == "picked", PickTask.qty), else_=PickTask.scanned_qty)
TOTE_STATUSES = ("pending", "picked")


def _in_totes(db: Session, item: StockItem) -> int:
    """Units of this batch already scanned into a tote for an order that is not packed yet. They are off the shelf
    and leave the count when the order is packed, so an expiry write-off must not take them too."""
    return db.scalar(
        select(func.coalesce(func.sum(TOTE_UNITS), 0))
        .join(FulfilmentJob, FulfilmentJob.id == PickTask.job_id)
        .where(PickTask.stock_item_id == item.id, PickTask.status.in_(TOTE_STATUSES),
               FulfilmentJob.state.in_(("created", "picking")))
    )


def expire_stock(db: Session, store_id: int | None = None, actor: str = "system") -> int:
    """Write off every batch whose expiry date has passed (all stores when `store_id` is None) and return the units
    removed. Each batch gets its own `expiry` ledger row and a stock.changed event.

    Holds are not touched: a paid order whose units expired may be left with reserved > on hand until the picker
    reaches the line, finds the shelf empty, and the usual empty-shelf path drops the line and frees its hold."""
    query = select(StockItem).where(StockItem.expiry_date < date.today(), StockItem.qty > 0)
    if store_id is not None:
        get_store(db, store_id)
        query = query.where(StockItem.store_id == store_id)
    removed = 0
    for item in db.scalars(query.order_by(StockItem.id).execution_options(populate_existing=True)).all():
        qty = item.qty - _in_totes(db, item)
        if qty <= 0:
            continue
        _remove_from_item(db, item, qty, "expiry", f"batch:{item.batch_no}", actor)
        events.publish(db, "stock.changed", item.store_id, {"sku_id": item.sku_id, "cause": "expiry"})
        removed += qty
    db.commit()
    return removed


def near_expiry(db: Session, store_id: int, days: int = 2) -> list[dict]:
    """Batches on the shelf that expire within `days` days (today counts as 0), soonest first."""
    get_store(db, store_id)
    today = date.today()
    items = db.scalars(
        select(StockItem).join(Bin, Bin.id == StockItem.bin_id)
        .where(StockItem.store_id == store_id, StockItem.qty > 0,
               StockItem.expiry_date >= today, StockItem.expiry_date <= today + timedelta(days=days))
        .order_by(StockItem.expiry_date, Bin.walk_seq)
        .execution_options(populate_existing=True)
    ).all()
    return [
        {"stock_item_id": i.id, "sku_id": i.sku_id, "sku": i.sku.name, "bin_id": i.bin_id, "bin": i.bin.code,
         "batch_no": i.batch_no, "qty": i.qty, "expiry_date": i.expiry_date.isoformat(),
         "days_left": (i.expiry_date - today).days}
        for i in items
    ]


def flag_near_expiry(db: Session, days: int = 2) -> int:
    """Publish item.near_expiry (HLD 8.2) for every store's near-expiry batches, at most once per batch per day.
    Returns how many events were published."""
    start_of_day = datetime.combine(utcnow().date(), time.min)
    already = {e.payload.get("stock_item_id") for e in db.scalars(
        select(EventLog).where(EventLog.type == "item.near_expiry", EventLog.ts >= start_of_day))}
    published = 0
    for store in db.scalars(select(Store).order_by(Store.id)).all():
        for row in near_expiry(db, store.id, days):
            if row["stock_item_id"] in already:
                continue
            events.publish(db, "item.near_expiry", store.id, row)
            published += 1
    db.commit()
    return published


def commit_sale(db: Session, task: PickTask, reference: str, actor: str) -> None:
    """A picked line leaves the store: take the units off the shelf and off the hold.
    Called by Pick & Pack when an order is packed. Does not commit."""
    item = task.stock_item
    if item is None or item.qty < task.qty:
        raise DomainError(409, "stock_mismatch", f"Bin no longer holds {task.qty} units for task {task.id}")
    _change_item_qty(db, item, -task.qty)
    _change_on_hand(db, item.store_id, task.sku_id, -task.qty, reserved_delta=-task.qty)
    _ledger(db, item.store_id, task.sku_id, item.bin_id, -task.qty, "sale", reference, actor)


def write_off_missing(db: Session, item: StockItem, qty: int, reference: str, actor: str) -> None:
    """The picker could not find units the system thought were in a bin. Remove them from the
    count; the hold on them stays until the caller re-allocates or unreserves. Does not commit.

    Units already scanned into a tote are off the shelf but still in `item.qty` until their order is packed, so they
    are never written off here (as in expire_stock). Without this, a write-off after an expiry write-off took the tote
    units a second time, and packing that order failed with stock_mismatch."""
    take = min(item.qty - _in_totes(db, item), qty)
    if take <= 0:
        return
    _change_item_qty(db, item, -take)
    _change_on_hand(db, item.store_id, item.sku_id, -take)
    _ledger(db, item.store_id, item.sku_id, item.bin_id, -take, "not_found", reference, actor)


# --------------------------------------------------------------------------- allocation (FEFO, FR-10)


def _promised(db: Session, item: StockItem) -> int:
    """Units of this batch promised to an order that is not packed yet: lines still to pick, and lines already
    scanned into a tote (they only leave item.qty when the order is packed)."""
    return db.scalar(
        select(func.coalesce(func.sum(PickTask.qty), 0))
        .join(FulfilmentJob, FulfilmentJob.id == PickTask.job_id)
        .where(PickTask.stock_item_id == item.id,
               PickTask.status.in_(TOTE_STATUSES),
               FulfilmentJob.state.in_(("created", "picking")))
    )


def allocate(db: Session, store_id: int, sku_id: int, qty: int,
             exclude_item_ids: tuple[int, ...] = ()) -> tuple[list[tuple[StockItem, int]], int]:
    """Choose which bins to pick `qty` units from: first-expiry-first-out, then nearest bin. Expired batches are
    never chosen, even before the hourly write-off has removed them.

    Units already promised to an unpacked order (pending or already scanned) are skipped.
    Returns (list of (stock item, quantity), quantity that could not be found).
    """
    items = db.scalars(
        select(StockItem)
        .join(Bin, Bin.id == StockItem.bin_id)
        .where(StockItem.store_id == store_id, StockItem.sku_id == sku_id, StockItem.qty > 0,
               StockItem.expiry_date >= date.today())                 # never pick a batch that has expired
        .order_by(StockItem.expiry_date, Bin.walk_seq)
        .execution_options(populate_existing=True)
    ).all()
    picks: list[tuple[StockItem, int]] = []
    remaining = qty
    for item in items:
        if remaining == 0:
            break
        if item.id in exclude_item_ids:
            continue
        free = item.qty - _promised(db, item)
        if free <= 0:
            continue
        take = min(free, remaining)
        picks.append((item, take))
        remaining -= take
    return picks, remaining


# --------------------------------------------------------------------------- stock audit (task 1.8)


def audit(db: Session, store_id: int | None = None) -> list[dict]:
    """Check that the three records of stock agree, for every store and SKU (or one store). Read-only.

    Returns one entry per (store, SKU) that breaks a rule, with the values and the rules broken in `problems`:
      on_hand_vs_bins        StockLevel.qty_on_hand is not the sum of StockItem.qty
      on_hand_vs_ledger      StockLevel.qty_on_hand is not the sum of StockLedger.delta
      negative_on_hand       fewer than 0 units on hand
      negative_reserved      fewer than 0 units held
      reserved_over_on_hand  more units held than exist (normal only between an expiry write-off and the picker
                             reaching that line, see `expire_stock`)
    An empty list means stock is consistent."""
    def totals(table, column) -> dict[tuple[int, int], int]:
        query = select(table.store_id, table.sku_id, func.coalesce(func.sum(column), 0)) \
            .group_by(table.store_id, table.sku_id)
        if store_id is not None:
            query = query.where(table.store_id == store_id)
        return {(store, sku): int(total) for store, sku, total in db.execute(query)}

    if store_id is not None:
        get_store(db, store_id)
    in_bins, in_ledger = totals(StockItem, StockItem.qty), totals(StockLedger, StockLedger.delta)
    query = select(StockLevel.store_id, StockLevel.sku_id, StockLevel.qty_on_hand, StockLevel.qty_reserved)
    if store_id is not None:
        query = query.where(StockLevel.store_id == store_id)
    levels = {(store, sku): (on_hand, reserved) for store, sku, on_hand, reserved in db.execute(query)}

    mismatches = []
    for key in sorted(levels.keys() | in_bins.keys() | in_ledger.keys()):
        on_hand, reserved = levels.get(key, (0, 0))      # stock recorded for a SKU with no StockLevel row counts as 0
        bins, ledger_sum = in_bins.get(key, 0), in_ledger.get(key, 0)
        problems = [name for name, broken in (
            ("on_hand_vs_bins", on_hand != bins),
            ("on_hand_vs_ledger", on_hand != ledger_sum),
            ("negative_on_hand", on_hand < 0),
            ("negative_reserved", reserved < 0),
            ("reserved_over_on_hand", reserved > on_hand),
        ) if broken]
        if problems:
            mismatches.append({"store_id": key[0], "sku_id": key[1], "problems": problems, "on_hand": on_hand,
                               "in_bins": bins, "in_ledger": ledger_sum, "reserved": reserved})
    return mismatches


# --------------------------------------------------------------------------- read models for the ops screen


def stock_by_bin(db: Session, store_id: int) -> list[dict]:
    get_store(db, store_id)
    items = db.scalars(
        select(StockItem).join(Bin, Bin.id == StockItem.bin_id)
        .where(StockItem.store_id == store_id, StockItem.qty > 0)
        .order_by(Bin.walk_seq, StockItem.expiry_date)
        .execution_options(populate_existing=True)
    ).all()
    today = date.today()
    return [
        {
            "stock_item_id": i.id, "sku_id": i.sku_id, "sku": i.sku.name, "bin_id": i.bin_id,
            "bin": i.bin.code, "zone": i.bin.zone, "batch_no": i.batch_no,
            "expiry_date": i.expiry_date.isoformat(), "days_to_expiry": (i.expiry_date - today).days,
            "qty": i.qty,
        }
        for i in items
    ]


def ledger(db: Session, store_id: int, limit: int = 50) -> list[dict]:
    rows = db.scalars(
        select(StockLedger).where(StockLedger.store_id == store_id)
        .order_by(StockLedger.id.desc()).limit(limit)
    ).all()
    return [
        {"id": r.id, "sku_id": r.sku_id, "bin_id": r.bin_id, "delta": r.delta, "reason": r.reason,
         "reference": r.reference, "actor": r.actor, "ts": r.ts.isoformat()}
        for r in rows
    ]


def reservation_view(res: Reservation) -> dict:
    return {
        "reservation_id": res.id,
        "order_id": res.order_id,
        "store_id": res.store_id,
        "status": res.status,
        "expires_at": res.expires_at.isoformat() + "Z",
        "lines": [{"sku_id": line.sku_id, "qty": line.qty} for line in res.lines],
    }
