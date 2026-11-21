"""Cycle counts with manager approval (FR-11, HLD 10) and the inventory accuracy KPI (HLD 12).

A blind count: the counter is told which bin and which SKUs to look for, but never how many the system expects,
so they count what is there instead of confirming a number.

    start  -> open       (SKUs to look for, no quantities)
    submit -> approved   (differences worth <= COUNT_AUTO_APPROVE_VALUE are applied at once)
           -> submitted  (bigger ones wait for a manager)
    approve / reject     (a manager decides; approving applies the differences)

Differences are applied with inventory.adjust(reason="count"), so the ledger explains every change.
"""
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ... import config, events
from ...errors import DomainError, not_found
from ...models import Bin, CycleCount, CycleCountLine, FulfilmentJob, PickTask, StockItem, utcnow
from . import service as inventory

OPEN_STATES = ("open", "submitted")


def get_count(db: Session, count_id: int) -> CycleCount:
    count = db.get(CycleCount, count_id)
    if count is None:
        raise not_found("Cycle count", count_id)
    return count


def _get_bin(db: Session, store_id: int, bin_id: int) -> Bin:
    inventory.get_store(db, store_id)
    bin_ = db.get(Bin, bin_id)
    if bin_ is None or bin_.store_id != store_id:
        raise DomainError(422, "bad_bin", f"Bin {bin_id} is not in store {store_id}")
    return bin_


def _skus_in_bin(db: Session, bin_id: int) -> list[int]:
    return list(db.scalars(
        select(StockItem.sku_id).where(StockItem.bin_id == bin_id, StockItem.qty > 0)
        .distinct().order_by(StockItem.sku_id)))


def _open_count_for(db: Session, bin_id: int) -> CycleCount | None:
    return db.scalar(select(CycleCount).where(CycleCount.bin_id == bin_id, CycleCount.status.in_(OPEN_STATES)))


def start(db: Session, store_id: int, bin_id: int, created_by: str, reason: str = "manual",
          also_sku_ids: tuple[int, ...] = (), commit: bool = True) -> CycleCount:
    """Open a count of one bin. A bin has at most one unfinished count: asking again returns that one, with any
    SKUs in `also_sku_ids` added (a SKU the system thinks has gone can still be on the shelf)."""
    _get_bin(db, store_id, bin_id)
    count = _open_count_for(db, bin_id)
    if count is None:
        count = CycleCount(store_id=store_id, bin_id=bin_id, status="open", reason=reason, created_by=created_by)
        db.add(count)
    if count.status == "open":
        wanted = set(_skus_in_bin(db, bin_id)) | set(also_sku_ids)
        for sku_id in sorted(wanted - {line.sku_id for line in count.lines}):
            count.lines.append(CycleCountLine(sku_id=sku_id))
    if commit:
        db.commit()
    else:
        db.flush()
    return count


def _on_the_shelf(db: Session, bin_id: int, sku_id: int) -> int:
    """What the records say is physically in the bin right now. Units promised to a pick task but not scanned yet
    are on the shelf and are counted; units already scanned into a tote for an unpacked order are not, including
    the first few of a line that is being scanned one at a time."""
    in_bin = db.scalar(select(func.coalesce(func.sum(StockItem.qty), 0))
                       .where(StockItem.bin_id == bin_id, StockItem.sku_id == sku_id))
    in_totes = db.scalar(
        select(func.coalesce(func.sum(inventory.TOTE_UNITS), 0))
        .join(FulfilmentJob, FulfilmentJob.id == PickTask.job_id)
        .join(StockItem, StockItem.id == PickTask.stock_item_id)
        .where(StockItem.bin_id == bin_id, StockItem.sku_id == sku_id, PickTask.status.in_(inventory.TOTE_STATUSES),
               FulfilmentJob.state.in_(("created", "picking"))))
    return in_bin - in_totes


def submit(db: Session, count_id: int, counted: dict[int, int], submitted_by: str) -> CycleCount:
    """Record what was found, compare it with the records at this moment, and apply it at once if the difference is
    small. Every SKU the count asked about needs a number (0 if the shelf is empty); a SKU nobody expected may be
    added."""
    count = get_count(db, count_id)
    if count.status != "open":
        raise DomainError(409, "count_closed", f"This count is already {count.status}")
    if any(qty < 0 for qty in counted.values()):
        raise DomainError(422, "bad_quantity", "A counted quantity cannot be negative")
    missing = sorted({line.sku_id for line in count.lines} - set(counted))
    if missing:
        raise DomainError(422, "lines_missing", "Give a number for every SKU on the list, 0 if none were there",
                          {"sku_ids": missing})
    for sku_id in sorted(set(counted) - {line.sku_id for line in count.lines}):
        inventory.get_sku(db, sku_id)
        count.lines.append(CycleCountLine(sku_id=sku_id))

    for line in count.lines:
        line.system_qty = _on_the_shelf(db, count.bin_id, line.sku_id)
        line.counted_qty = counted[line.sku_id]
    db.flush()
    count.difference_value = round(sum(abs(line.counted_qty - line.system_qty) * line.sku.price
                                       for line in count.lines), 2)
    count.status, count.submitted_by, count.submitted_at = "submitted", submitted_by, utcnow()
    db.flush()
    if count.difference_value <= config.COUNT_AUTO_APPROVE_VALUE:
        _apply(db, count, decided_by="auto")
    else:
        events.publish(db, "count.needs_approval", count.store_id,
                       {"count_id": count.id, "bin_id": count.bin_id, "difference_value": count.difference_value})
    db.commit()
    return count


def _decide(db: Session, count: CycleCount, new_status: str, decided_by: str) -> None:
    """Move a submitted count to approved or rejected, once: if two managers press at the same moment, only the
    first changes it (the same conditional update as reservations use)."""
    changed = db.execute(
        update(CycleCount).where(CycleCount.id == count.id, CycleCount.status == "submitted")
        .values(status=new_status, decided_by=decided_by, decided_at=utcnow())
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed == 0:
        db.rollback()
        db.refresh(count)
        raise DomainError(409, "count_decided", f"This count is already {count.status}")
    db.refresh(count)


def _apply(db: Session, count: CycleCount, decided_by: str) -> None:
    """Approve and write every difference to stock, in the caller's transaction. If the bin can no longer give up
    the units (they were sold since the count), adjust refuses and nothing is applied: count the bin again."""
    _decide(db, count, "approved", decided_by)
    for line in count.lines:
        delta = line.counted_qty - line.system_qty
        if delta:
            inventory.adjust(db, count.store_id, line.sku_id, delta, "count", bin_id=count.bin_id,
                             actor=decided_by, reference=f"count:{count.id}", commit=False,
                             shelf_rules=False)


def approve(db: Session, count_id: int, decided_by: str, may_approve_own: bool = False) -> CycleCount:
    count = get_count(db, count_id)
    if count.status != "submitted":
        raise DomainError(409, "count_not_waiting", f"This count is {count.status}, not waiting for approval")
    if count.submitted_by == decided_by and not may_approve_own:
        raise DomainError(403, "own_count", "Someone other than the counter must approve this count")
    _apply(db, count, decided_by)
    db.commit()
    return count


def reject(db: Session, count_id: int, decided_by: str) -> CycleCount:
    """The manager does not trust the count: stock stays as it is. Open a new count to try again."""
    count = get_count(db, count_id)
    if count.status != "submitted":
        raise DomainError(409, "count_not_waiting", f"This count is {count.status}, not waiting for approval")
    _decide(db, count, "rejected", decided_by)
    db.commit()
    return count


def list_counts(db: Session, store_id: int, status: str | None = None) -> list[CycleCount]:
    inventory.get_store(db, store_id)
    query = select(CycleCount).where(CycleCount.store_id == store_id)
    if status:
        query = query.where(CycleCount.status == status)
    return list(db.scalars(query.order_by(CycleCount.id.desc())))


def count_view(count: CycleCount) -> dict:
    """While a count is open it shows only what to look for, never a quantity: that is what makes it blind."""
    view = {"count_id": count.id, "store_id": count.store_id, "bin_id": count.bin_id, "bin": count.bin.code,
            "status": count.status, "reason": count.reason, "created_by": count.created_by,
            "created_at": count.created_at.isoformat()}
    if count.status == "open":
        view["lines"] = [{"sku_id": line.sku_id, "sku": line.sku.name, "barcode": line.sku.barcode}
                         for line in count.lines]
        return view
    view["lines"] = [{"sku_id": line.sku_id, "sku": line.sku.name, "system_qty": line.system_qty,
                      "counted_qty": line.counted_qty, "difference": line.counted_qty - line.system_qty}
                     for line in count.lines]
    view.update(submitted_by=count.submitted_by, difference_value=count.difference_value,
                decided_by=count.decided_by)
    return view


# --------------------------------------------------------------------------- inventory accuracy KPI (HLD 12)


def inventory_accuracy(db: Session, store_id: int) -> dict:
    """Share of counted lines where the count matched the records, over the last KPI_WINDOW_DAYS days.
    Rejected counts are left out (the manager judged the count wrong, not the records). Target: at least 99.5%."""
    since = utcnow() - timedelta(days=config.KPI_WINDOW_DAYS)
    lines = db.scalars(
        select(CycleCountLine).join(CycleCount, CycleCount.id == CycleCountLine.count_id)
        .where(CycleCount.store_id == store_id, CycleCount.status.in_(("submitted", "approved")),
               CycleCount.submitted_at >= since)
    ).all()
    matching = sum(1 for line in lines if line.counted_qty == line.system_qty)
    return {"value": round(matching / len(lines), 4) if lines else None, "lines_counted": len(lines),
            "lines_matching": matching, "days": config.KPI_WINDOW_DAYS}


# --------------------------------------------------------------------------- count a bin a picker found short


@events.subscribe("stock.changed")
def count_bin_found_short(db: Session, store_id: int | None, payload: dict) -> None:
    """An empty shelf usually means the whole bin's record is wrong, not just the units one order needed.
    Pick & Pack's empty-shelf report includes the bin, so open a count of it (once; an open count is reused)."""
    if payload.get("cause") != "not_found" or payload.get("bin_id") is None or store_id is None:
        return
    start(db, store_id, payload["bin_id"], created_by="system", reason="not_found",
          also_sku_ids=(payload["sku_id"],), commit=False)
