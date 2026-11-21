"""Slotting Service (HLD service 6): decide which bin a SKU should live in.

Fast movers live nearest the packing desk (task 3.6): `velocity` reads two weeks of sales from the ledger, `plan`
gives each selling SKU a home bin, fastest nearest, and lists the batches to move, and `apply` moves them through
inventory.move. New deliveries go to the planned home bin first; otherwise a SKU is kept together, then the
emptiest bin in the right temperature zone is used, and a bin is never filled past its capacity.
"""
from datetime import date

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ...errors import DomainError
from ...models import SKU, Bin, StockItem, StockLevel
from ..forecasting import service as forecasting
from ..inventory import service as inventory

VELOCITY_DAYS = 14                     # how many whole days of sales the plan ranks SKUs by (guide task 3.6)


def velocity(db: Session, store_id: int, days: int = VELOCITY_DAYS) -> dict[int, float]:
    """Units sold a day per SKU over the last `days` whole days, from the ledger (today is only partly over). SKUs
    that sold nothing are left out. Read-only."""
    sold = {sku_id: sum(by_day.values()) for sku_id, by_day in forecasting.daily_sales(db, store_id, days).items()}
    return {sku_id: units / days for sku_id, units in sold.items() if units > 0}


def _homes(db: Session, store_id: int, speeds: dict[int, float]) -> tuple[list[Bin], list[tuple[SKU, Bin]]]:
    """The store's bins, nearest the packing desk first, and the home bin of each SKU that sells: per zone, the
    fastest SKU gets the nearest bin, the next one the next bin, and so on. A SKU that sold nothing has no home, and
    with more sellers than bins the slowest have none either. Read-only."""
    bins = db.scalars(select(Bin).where(Bin.store_id == store_id).order_by(Bin.walk_seq)).all()
    carried = db.scalars(select(SKU).join(StockLevel, StockLevel.sku_id == SKU.id)
                         .where(StockLevel.store_id == store_id)).all()
    homes: list[tuple[SKU, Bin]] = []
    for zone in dict.fromkeys(bin_.zone for bin_ in bins):
        sellers = sorted((sku for sku in carried if sku.storage_type == zone and sku.id in speeds),
                         key=lambda sku: (-speeds[sku.id], sku.name))
        homes += zip(sellers, [bin_ for bin_ in bins if bin_.zone == zone])
    return bins, homes


def home_bins(db: Session, store_id: int, days: int = VELOCITY_DAYS) -> dict[int, Bin]:
    """{sku_id: planned home bin} for the SKUs that sell. Read-only."""
    return {sku.id: bin_ for sku, bin_ in _homes(db, store_id, velocity(db, store_id, days))[1]}


def plan(db: Session, store_id: int, days: int = VELOCITY_DAYS) -> dict:
    """The re-slotting plan: fast movers near the packing station (HLD service 6). Read-only.

    homes: each selling SKU's home bin (see _homes). moves: whole batches, first every batch of a seller that is not
    in its home, then anything else in a seller's home, out to the furthest bin of that SKU's own zone that no seller
    has: one already holding that SKU first, and only one with room for it (a batch counted into a bin of the wrong
    zone goes back to its own). Other SKUs that did not sell stay where they are: moving them would not shorten any
    walk. Expired batches stay for the write-off: picking never sends anyone to them."""
    inventory.get_store(db, store_id)
    speeds = velocity(db, store_id, days)
    bins, homes = _homes(db, store_id, speeds)
    by_id = {bin_.id: bin_ for bin_ in bins}
    items = sorted(db.scalars(select(StockItem).where(StockItem.store_id == store_id, StockItem.qty > 0)
                              .execution_options(populate_existing=True)).all(),
                   key=lambda item: (by_id[item.bin_id].walk_seq, item.expiry_date, item.id))
    held: dict[int, dict[int, int]] = {bin_.id: {} for bin_ in bins}     # bin -> sku -> units, as the plan goes
    for item in items:
        held[item.bin_id][item.sku_id] = held[item.bin_id].get(item.sku_id, 0) + item.qty
    moves: list[dict] = []
    fresh = [item for item in items if item.expiry_date >= date.today()]       # what inventory.allocate would pick

    def move(item: StockItem, to: Bin) -> None:
        moves.append({"stock_item_id": item.id, "sku_id": item.sku_id, "sku": item.sku.name,
                      "batch_no": item.batch_no, "qty": item.qty, "from_bin": by_id[item.bin_id].code,
                      "to_bin": to.code})
        held[item.bin_id][item.sku_id] -= item.qty
        held[to.id][item.sku_id] = held[to.id].get(item.sku_id, 0) + item.qty

    home_of = {sku.id: bin_ for sku, bin_ in homes}
    for sku, home in homes:
        for item in fresh:
            if item.sku_id == sku.id and item.bin_id != home.id:
                move(item, home)
    taken = {home.id for home in home_of.values()}
    for item in fresh:
        if item.bin_id not in taken or item.sku_id in home_of:
            continue
        spare = [bin_ for bin_ in reversed(bins) if bin_.zone == item.sku.storage_type and bin_.id not in taken
                 and sum(held[bin_.id].values()) + item.qty <= bin_.capacity]
        target = next((bin_ for bin_ in spare if held[bin_.id].get(item.sku_id)), None) or next(iter(spare), None)
        if target is not None:
            move(item, target)
    return {"store_id": store_id, "days": days,
            "homes": [{"sku_id": sku.id, "sku": sku.name, "zone": home.zone, "units_per_day": round(speeds[sku.id], 2),
                       "bin": home.code} for sku, home in homes],
            "moves": moves}


def _load(db: Session, store_id: int) -> dict[int, int]:
    """Units stored per bin in one store."""
    return {bin_id: int(units) for bin_id, units in db.execute(
        select(StockItem.bin_id, func.sum(StockItem.qty)).where(StockItem.store_id == store_id)
        .group_by(StockItem.bin_id)).all()}


def room_in(db: Session, bin_: Bin) -> int:
    """Units `bin_` can still take: its capacity less what it holds (negative if it is already over). Read-only."""
    stored = db.scalar(select(func.coalesce(func.sum(StockItem.qty), 0)).where(StockItem.bin_id == bin_.id))
    return bin_.capacity - int(stored)


def suggest_bin(db: Session, store_id: int, sku_id: int, qty: int = 0) -> Bin:
    """The bin for `qty` more units of a SKU: its planned home bin (task 3.6) if that has room, then a bin already
    holding it, otherwise the emptiest bin with room in the right temperature zone, nearest the packing desk.
    Read-only.

    409 no_bin when the store has no bin of that zone, 409 bin_full when none has room for `qty`.
    """
    sku = inventory.get_sku(db, sku_id)
    load = _load(db, store_id)
    home = home_bins(db, store_id).get(sku_id)
    if home is not None and home.capacity - load.get(home.id, 0) >= qty:
        return home
    same_sku = db.scalars(
        select(Bin).join(StockItem, StockItem.bin_id == Bin.id)
        .where(StockItem.store_id == store_id, StockItem.sku_id == sku_id, StockItem.qty > 0,
               Bin.zone == sku.storage_type)
        .distinct().order_by(Bin.walk_seq)
    ).all()
    for bin_ in same_sku:
        if bin_.capacity - load.get(bin_.id, 0) >= qty:
            return bin_
    bins = db.scalars(
        select(Bin).where(Bin.store_id == store_id, Bin.zone == sku.storage_type).order_by(Bin.walk_seq)
    ).all()
    if not bins:
        raise DomainError(409, "no_bin", f"Store {store_id} has no {sku.storage_type} bin for {sku.name}")
    with_room = [b for b in bins if b.capacity - load.get(b.id, 0) >= qty]
    if not with_room:
        raise DomainError(409, "bin_full", f"No {sku.storage_type} bin in store {store_id} has room for {qty} more "
                                           f"{sku.name}", {"zone": sku.storage_type, "qty": qty})
    return min(with_room, key=lambda b: (load.get(b.id, 0), b.walk_seq))


def place(db: Session, store_id: int, sku_id: int, qty: int) -> list[tuple[Bin, int]]:
    """Where `qty` more units of a SKU go, split over as many bins as it takes: its planned home bin first (task
    3.6: new stock re-slots a fast mover before anyone applies the plan), then the bins already holding it (nearest
    the packing desk first), then the emptiest bins of its zone, each only up to its capacity. Read-only.

    409 no_bin when the store has no bin of that zone, 409 bin_full when the zone cannot take them all.
    """
    if qty <= 0:
        raise DomainError(422, "bad_quantity", "Received quantity must be at least 1")
    sku = inventory.get_sku(db, sku_id)
    bins = db.scalars(
        select(Bin).where(Bin.store_id == store_id, Bin.zone == sku.storage_type).order_by(Bin.walk_seq)
    ).all()
    if not bins:
        raise DomainError(409, "no_bin", f"Store {store_id} has no {sku.storage_type} bin for {sku.name}")
    load = _load(db, store_id)
    own = set(db.scalars(select(StockItem.bin_id).where(
        StockItem.store_id == store_id, StockItem.sku_id == sku_id, StockItem.qty > 0)))
    home = home_bins(db, store_id).get(sku_id)
    first = [b for b in bins if home is not None and b.id == home.id]
    then = [b for b in bins if b.id in own and b not in first]
    rest = sorted((b for b in bins if b not in first and b not in then), key=lambda b: (load.get(b.id, 0), b.walk_seq))
    portions, left = [], qty
    for bin_ in first + then + rest:
        take = min(left, bin_.capacity - load.get(bin_.id, 0))
        if take > 0:
            portions.append((bin_, take))
            left -= take
        if left == 0:
            return portions
    raise DomainError(409, "bin_full", f"The {sku.storage_type} bins of store {store_id} have room for {qty - left} "
                                       f"more {sku.name}, not {qty}", {"zone": sku.storage_type, "room": qty - left})


def lock_bin(db: Session, bin_id: int) -> None:
    """Hold the bin's row until this transaction ends, so putaways into one bin take turns. Without it, on PostgreSQL
    two putaways of different SKUs would each check the room without seeing the other's units (READ COMMITTED); SQLite
    allows one writer at a time anyway."""
    db.execute(update(Bin).where(Bin.id == bin_id).values(capacity=Bin.capacity)
               .execution_options(synchronize_session=False))


def lock_store_bins(db: Session, store_id: int) -> list[Bin]:
    """Lock every bin of the store, in id order, until this transaction ends, and return them. Re-slotting and the
    one-call receipt both take them in this one order, so on PostgreSQL neither can hold a bin the other waits for
    while it waits for one the other holds (a deadlock)."""
    bins = db.scalars(select(Bin).where(Bin.store_id == store_id).order_by(Bin.id)).all()
    for bin_ in bins:
        lock_bin(db, bin_.id)
    return bins


def apply(db: Session, store_id: int, days: int = VELOCITY_DAYS, actor: str = "manager", commit: bool = True) -> dict:
    """Do the re-slotting plan (over the same `days` as the plan shown) through inventory.move: two `move` ledger rows
    per batch, nothing gained or lost.

    The store's bins are locked first, so a putaway waits and the plan is made from what the bins hold now. A batch
    moves only when its new bin has room for it right then, so no bin is ever over capacity, even between two moves;
    one that has to wait is tried again while other moves free some room. Skipped, and left where it is: a batch
    inventory.move refuses, such as one a picker has been sent to (`promised_to_picker`, guide step 4), and one still
    without room at the end (`no_room`, such as two full bins that would have to swap). One skipped batch never stops
    the others. `commit=False` leaves the transaction open for run_once."""
    inventory.get_store(db, store_id)
    bins = {bin_.code: bin_ for bin_ in lock_store_bins(db, store_id)}
    waiting = plan(db, store_id, days)["moves"]
    moved: list[dict] = []
    skipped: list[dict] = []
    while waiting:
        still = []
        for move in waiting:
            to = bins[move["to_bin"]]
            if move["qty"] > room_in(db, to):
                still.append(move)
                continue
            try:
                inventory.move(db, db.get(StockItem, move["stock_item_id"]), to.id, move["qty"], reference="reslot",
                               actor=actor)
            except DomainError as error:
                skipped.append({**move, "code": error.code, "reason": error.message})
                continue
            moved.append(move)
        if len(still) == len(waiting):                  # a whole pass and nothing moved or was skipped: stuck
            break
        waiting = still
    skipped += [{**move, "code": "no_room",
                 "reason": f"Bin {move['to_bin']} has no room for {move['qty']} more units, even after the other moves"}
                for move in waiting]
    db.flush()
    result = {"store_id": store_id, "moved": moved, "skipped": skipped}
    if commit:
        db.commit()
    return result


def ensure_room(db: Session, store_id: int, bin_id: int, qty: int) -> None:
    """409 bin_full when `qty` more units would overfill the bin. A bin that is not in the store is left for
    inventory.receive to refuse. Read-only."""
    bin_ = db.get(Bin, bin_id)
    if bin_ is None or bin_.store_id != store_id:
        return
    room = room_in(db, bin_)
    if qty > room:
        raise DomainError(409, "bin_full", f"Bin {bin_.code} has room for {max(room, 0)} more units, not {qty}",
                          {"bin": bin_.code, "capacity": bin_.capacity, "room": max(room, 0)})


def ensure_not_overfilled(db: Session, bin_id: int) -> None:
    """409 bin_full when a bin now holds more than its capacity. Called right after units are written, in the same
    transaction: another receipt may have filled the bin since the room was checked. Read-only."""
    bin_ = db.get(Bin, bin_id)
    if bin_ is not None and room_in(db, bin_) < 0:
        raise DomainError(409, "bin_full", f"Bin {bin_.code} was filled by another receipt first; "
                                           f"nothing in this receipt was kept", {"bin": bin_.code})


def list_bins(db: Session, store_id: int) -> list[dict]:
    inventory.get_store(db, store_id)
    bins = db.scalars(select(Bin).where(Bin.store_id == store_id).order_by(Bin.walk_seq)).all()
    return [{"bin_id": b.id, "code": b.code, "zone": b.zone, "walk_seq": b.walk_seq} for b in bins]
