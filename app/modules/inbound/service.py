"""Inbound Service (HLD service 9): receive a truck and put the stock away (HLD 6.3).

A receipt goes through five steps. Open it; scan each item off the truck onto the dock (nothing is sellable yet, and
damaged units or batches with too little shelf life left are recorded but never stocked); get putaway tasks; put each
line away, which makes it sellable at once; close it, which marks the transfer order received and reports shortfalls.
`receive_goods` is the older one-call receipt.

Every write step takes `commit`: the router passes False and lets run_once commit, so a request retried with the same
Idempotency-Key gets its saved answer instead of being done twice.
"""
from datetime import date, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ... import config, events
from ...errors import DomainError, not_found
from ...models import SKU, Bin, GoodsReceipt, GoodsReceiptLine, TransferOrder, TransferOrderLine, utcnow
from ..inventory import service as inventory
from ..slotting import service as slotting


def get_receipt(db: Session, receipt_id: int) -> GoodsReceipt:
    receipt = db.get(GoodsReceipt, receipt_id)
    if receipt is None:
        raise not_found("Goods receipt", receipt_id)
    return receipt


def receipt_view(receipt: GoodsReceipt) -> dict:
    """The receipt and its lines, JSON-ready (run_once saves it as the answer to a retried request). Units still on
    the dock when the receipt was closed or cancelled went back to the hub."""
    closed = receipt.status != "open"
    return {"receipt_id": receipt.id, "store_id": receipt.store_id, "transfer_order_id": receipt.transfer_order_id,
            "status": receipt.status, "created_at": receipt.created_at.isoformat(),
            "closed_at": receipt.closed_at.isoformat() if receipt.closed_at else None,
            "lines": [{"sku_id": line.sku_id, "sku": line.sku.name, "batch_no": line.batch_no,
                       "expiry_date": line.expiry_date.isoformat(), "scanned": line.qty_scanned,
                       "damaged": line.qty_damaged, "rejected": line.qty_rejected, "put_away": line.qty_put_away,
                       "on_dock": 0 if closed else line.qty_scanned - line.qty_put_away,
                       "sent_back": line.qty_scanned - line.qty_put_away if closed else 0} for line in receipt.lines]}


def _lock_open(db: Session, receipt_id: int) -> GoodsReceipt:
    """The receipt, if it is open, locked to this transaction. The update changes nothing, but other steps on the same
    receipt (on SQLite, every other write) wait at it until this transaction ends and then see what it wrote: two
    putaways of one line cannot both take the same units, and a closing receipt cannot take a late scan.
    404; 409 receipt_closed (closed or cancelled)."""
    locked = db.execute(update(GoodsReceipt).where(GoodsReceipt.id == receipt_id, GoodsReceipt.status == "open")
                        .values(status="open").execution_options(synchronize_session=False)).rowcount
    if not locked:
        receipt = db.get(GoodsReceipt, receipt_id, populate_existing=True)
        if receipt is None:
            raise not_found("Goods receipt", receipt_id)
        raise DomainError(409, "receipt_closed", f"Goods receipt {receipt_id} is {receipt.status}")
    receipt = db.get(GoodsReceipt, receipt_id, populate_existing=True)
    db.scalars(select(GoodsReceiptLine).where(GoodsReceiptLine.receipt_id == receipt_id)
               .execution_options(populate_existing=True)).all()   # this session may hold older copies of the lines
    db.expire(receipt, ["lines"])
    return receipt


def open_receipt(db: Session, store_id: int, transfer_order_id: int | None = None, commit: bool = True) -> dict:
    """Start receiving a truck (HLD 6.3, step 1). Changes no stock. 404 for a transfer order of another store,
    409 already_received for one received before, 409 receipt_open while another receipt of it is open. The order is
    locked first, so two receipts of one truck (a double-click on Receive truck) take turns and the second sees the
    first."""
    inventory.get_store(db, store_id)
    if transfer_order_id is not None:
        locked = db.execute(update(TransferOrder).where(TransferOrder.id == transfer_order_id,
                                                        TransferOrder.store_id == store_id,
                                                        TransferOrder.status != "received")
                            .values(status=TransferOrder.status).execution_options(synchronize_session=False)).rowcount
        order = db.get(TransferOrder, transfer_order_id, populate_existing=True)
        if order is None or order.store_id != store_id:
            raise not_found("Transfer order", transfer_order_id)
        if not locked:
            raise DomainError(409, "already_received", f"Transfer order {order.id} was already received")
        still_open = db.scalar(select(GoodsReceipt.id).where(GoodsReceipt.transfer_order_id == order.id,
                                                             GoodsReceipt.status == "open"))
        if still_open is not None:
            raise DomainError(409, "receipt_open", f"Goods receipt {still_open} of transfer order {order.id} is "
                                                   f"still open", {"receipt_id": still_open})
    receipt = GoodsReceipt(store_id=store_id, transfer_order_id=transfer_order_id, status="open")
    db.add(receipt)
    db.flush()
    view = receipt_view(receipt)
    if commit:
        db.commit()
    return view


def scan(db: Session, receipt_id: int, barcode: str, qty: int, batch_no: str | None = None,
         expiry_date: date | None = None, damaged: bool = False, commit: bool = True) -> dict:
    """Scan units off the truck (HLD 6.3, step 2). Changes no stock: good units wait on the dock until they are put
    away. Damaged units, and a batch with less than MIN_REMAINING_SHELF_LIFE of its shelf life left, are recorded on
    the receipt and never stocked. The API requires batch and expiry; the one-call receipt may leave them out (full
    shelf life, and a batch named after its expiry date, so units with different dates never share a batch: #56).
    422 unknown_barcode; 422 batch_mismatch for a batch already scanned with another expiry; 409 receipt_closed."""
    receipt = _lock_open(db, receipt_id)
    if qty <= 0:
        raise DomainError(422, "bad_quantity", "Scanned quantity must be at least 1")
    sku = db.scalar(select(SKU).where(SKU.barcode == barcode))
    if sku is None:
        raise DomainError(422, "unknown_barcode", f"No product has barcode {barcode}")
    expiry = expiry_date or date.today() + timedelta(days=sku.shelf_life_days)
    batch_no = batch_no or f"B{expiry:%y%m%d}"
    line = next((ln for ln in receipt.lines if ln.sku_id == sku.id and ln.batch_no == batch_no), None)
    if line is None:
        line = GoodsReceiptLine(sku_id=sku.id, sku=sku, batch_no=batch_no, expiry_date=expiry, qty_scanned=0,
                                qty_damaged=0, qty_rejected=0, qty_put_away=0)
        receipt.lines.append(line)
    elif line.expiry_date != expiry:
        raise DomainError(422, "batch_mismatch", f"Batch {batch_no} of {sku.name} was scanned with expiry "
                                                 f"{line.expiry_date}, not {expiry}")
    days_left = (expiry - date.today()).days
    if damaged:
        line.qty_damaged += qty
        outcome, reason = "damaged", "Damaged on arrival: recorded, never stocked"
    elif days_left < config.MIN_REMAINING_SHELF_LIFE * sku.shelf_life_days:
        line.qty_rejected += qty
        outcome, reason = "rejected", (f"Refused: {days_left} of {sku.shelf_life_days} days of shelf life left, "
                                       f"at least {config.MIN_REMAINING_SHELF_LIFE:.0%} is needed")
    else:
        line.qty_scanned += qty
        outcome, reason = "on_dock", None
    db.flush()
    view = receipt_view(receipt)
    view["scan"] = {"sku_id": sku.id, "sku": sku.name, "batch_no": batch_no, "qty": qty, "outcome": outcome,
                    "reason": reason}
    if commit:
        db.commit()
    return view


def putaway_tasks(db: Session, receipt_id: int) -> list[dict]:
    """Where to put what waits on the dock (HLD 6.3, step 3): one task per bin portion from slotting.place, the SKU's
    planned home bin first, then its own bins, never past a bin's capacity. A line that cannot be placed gets one task
    with bin_code None and
    the reason. Tasks reflect the bins as they are now: put lines away one at a time. None once the receipt is closed
    or cancelled: what was left on the dock went back. Read-only."""
    receipt = get_receipt(db, receipt_id)
    if receipt.status != "open":
        return []
    tasks = []
    for line in receipt.lines:
        on_dock = line.qty_scanned - line.qty_put_away
        if on_dock <= 0:
            continue
        task = {"sku_id": line.sku_id, "sku": line.sku.name, "batch_no": line.batch_no,
                "expiry_date": line.expiry_date.isoformat()}
        try:
            portions = slotting.place(db, receipt.store_id, line.sku_id, on_dock)
        except DomainError as error:                # no bin of that zone, or not enough room in it
            tasks.append({**task, "qty": on_dock, "bin_code": None, "problem": error.message})
            continue
        tasks += [{**task, "qty": qty, "bin_code": bin_.code} for bin_, qty in portions]
    return tasks


def put_away(db: Session, receipt_id: int, sku_id: int, batch_no: str, bin_code: str, qty: int,
             actor: str = "receiver", commit: bool = True) -> dict:
    """Move units from the dock into a bin (HLD 6.3, step 4). They are sellable as soon as this commits, whatever is
    still on the dock. 422 not_scanned, too_many (more than is on the dock), bad_bin, wrong_zone; 409 bin_full when
    the bin has no room, checked again right after writing; 409 receipt_closed."""
    receipt = _lock_open(db, receipt_id)
    if qty <= 0:
        raise DomainError(422, "bad_quantity", "Quantity to put away must be at least 1")
    line = next((ln for ln in receipt.lines if ln.sku_id == sku_id and ln.batch_no == batch_no), None)
    if line is None or line.qty_scanned == 0:
        raise DomainError(422, "not_scanned", f"No good units of SKU {sku_id}, batch {batch_no}, were scanned onto "
                                              f"goods receipt {receipt.id}")
    bin_ = db.scalar(select(Bin).where(Bin.store_id == receipt.store_id, Bin.code == bin_code))
    # Take the units off the dock in one conditional update, so they can be taken only once even without the lock.
    on_the_dock = GoodsReceiptLine.qty_scanned - GoodsReceiptLine.qty_put_away
    taken = db.execute(update(GoodsReceiptLine).where(GoodsReceiptLine.id == line.id, on_the_dock >= qty)
                       .values(qty_put_away=GoodsReceiptLine.qty_put_away + qty)
                       .execution_options(synchronize_session=False)).rowcount
    db.refresh(line)
    if not taken:
        on_dock = line.qty_scanned - line.qty_put_away
        raise DomainError(422, "too_many", f"Only {on_dock} of {line.sku.name}, batch {batch_no}, are on the dock, "
                                           f"not {qty}", {"on_dock": on_dock})
    if bin_ is None:
        raise DomainError(422, "bad_bin", f"Store {receipt.store_id} has no bin {bin_code}")
    slotting.lock_bin(db, bin_.id)
    slotting.ensure_room(db, receipt.store_id, bin_.id, qty)
    inventory.receive(db, receipt.store_id, sku_id, qty, bin_.id, batch_no, line.expiry_date, _reference(receipt),
                      actor)
    slotting.ensure_not_overfilled(db, bin_.id)
    if receipt.transfer_order_id:           # on the shelf now, so no longer on its way (replenishment.in_transit)
        db.execute(update(TransferOrderLine).where(TransferOrderLine.transfer_order_id == receipt.transfer_order_id,
                                                   TransferOrderLine.sku_id == sku_id)
                   .values(qty_received=TransferOrderLine.qty_received + qty)
                   .execution_options(synchronize_session=False))
    db.flush()
    view = receipt_view(receipt)
    view["put"] = {"sku_id": sku_id, "sku": line.sku.name, "batch_no": batch_no, "qty": qty, "bin": bin_.code}
    if commit:
        db.commit()
    return view


def _reference(receipt: GoodsReceipt) -> str:
    """The ledger reference for what a receipt puts away: its transfer order, otherwise the receipt itself."""
    return f"to:{receipt.transfer_order_id}" if receipt.transfer_order_id else f"grn:{receipt.id}"


def close_receipt(db: Session, receipt_id: int, send_back: bool = False, commit: bool = True) -> dict:
    """Finish a receipt (HLD 6.3, step 5): the transfer order is marked received with what was put away, every SKU
    ordered or scanned is reported as ordered versus received (with damaged, rejected and sent-back units), and
    goods.received is published. Refused (409 still_on_dock) while good units wait on the dock, unless send_back:
    then they go back to the hub and are never stocked (a miscount, or no room for them). 409 nothing_scanned for a
    receipt with nothing scanned on it, which would mark the truck received at 0 units: cancel it instead.
    409 receipt_closed."""
    receipt = _lock_open(db, receipt_id)
    if not receipt.lines:
        raise DomainError(409, "nothing_scanned", f"Nothing was scanned on goods receipt {receipt_id}: scan the truck, "
                                                  "or cancel the receipt")
    waiting = [f"{line.qty_scanned - line.qty_put_away} {line.sku.name} (batch {line.batch_no})"
               for line in receipt.lines if line.qty_scanned > line.qty_put_away]
    if waiting and not send_back:
        raise DomainError(409, "still_on_dock", f"Put these away, or close with send_back to return them to the "
                                                f"hub: {', '.join(waiting)}")
    rows: dict[int, dict] = {}

    def row(sku: SKU) -> dict:
        return rows.setdefault(sku.id, {"sku_id": sku.id, "sku": sku.name, "ordered": 0, "received": 0,
                                        "damaged": 0, "rejected": 0, "sent_back": 0})

    order = db.get(TransferOrder, receipt.transfer_order_id) if receipt.transfer_order_id else None
    for ordered in order.lines if order else []:
        row(ordered.sku)["ordered"] += ordered.qty_ordered
    for line in receipt.lines:
        counts = row(line.sku)
        counts["received"] += line.qty_put_away
        counts["damaged"] += line.qty_damaged
        counts["rejected"] += line.qty_rejected
        counts["sent_back"] += line.qty_scanned - line.qty_put_away
    if order is not None:
        received = db.execute(update(TransferOrder)
                              .where(TransferOrder.id == order.id, TransferOrder.status != "received")
                              .values(status="received", received_at=utcnow())
                              .execution_options(synchronize_session=False)).rowcount
        if not received:
            raise DomainError(409, "already_received", f"Transfer order {order.id} was received by another receipt")
        db.refresh(order, ["status", "received_at"])
        for ordered in order.lines:
            ordered.qty_received = rows[ordered.sku_id]["received"]
    receipt.status, receipt.closed_at = "closed", utcnow()
    report = list(rows.values())
    shortfalls = [counts for counts in report if counts["received"] < counts["ordered"]]
    events.publish(db, "goods.received", receipt.store_id,
                   {"receipt_id": receipt.id, "transfer_order_id": receipt.transfer_order_id, "lines": len(report),
                    "discrepancies": sum(counts["received"] != counts["ordered"] for counts in report)})
    db.flush()
    view = receipt_view(receipt)
    view.update(report=report, shortfalls=shortfalls)
    if commit:
        db.commit()
    return view


def cancel_receipt(db: Session, receipt_id: int, commit: bool = True) -> dict:
    """Drop a receipt opened or scanned by mistake, before anything from it is put away. Its lines stay as the record
    (what was on the dock goes back, as with send_back), and the transfer order is left as it was, so the real truck can
    still be received. 409 already_put_away once any unit of it is sellable: close it instead. 409 receipt_closed.
    The receipt is locked first, so a putaway already running finishes before this decides."""
    receipt = _lock_open(db, receipt_id)
    stocked = [f"{line.qty_put_away} {line.sku.name}" for line in receipt.lines if line.qty_put_away]
    if stocked:
        raise DomainError(409, "already_put_away", f"Goods receipt {receipt_id} already put away {', '.join(stocked)}: "
                                                   "close it instead (send_back=true returns what waits on the dock)")
    receipt.status, receipt.closed_at = "cancelled", utcnow()
    db.flush()
    view = receipt_view(receipt)
    if commit:
        db.commit()
    return view


def receive_goods(db: Session, store_id: int, lines: list[dict], transfer_order_id: int | None = None,
                  actor: str = "receiver", commit: bool = True) -> dict:
    """The one-call goods receipt: the five steps in order. Each line (sku_id, qty, and optionally batch_no,
    expiry_date, bin_id) is scanned, then put away: into its bin_id if given, otherwise over the bins Slotting picks,
    never past a bin's capacity. A batch refused for too little shelf life is listed under "rejected" (and in
    "discrepancies" when ordered); any other refusal, such as 409 bin_full, keeps nothing of the receipt. The store's
    bins are locked first, in the order re-slotting locks them: one bin per line, in line order, could deadlock with
    it on PostgreSQL."""
    receipt_id = open_receipt(db, store_id, transfer_order_id, commit=False)["receipt_id"]
    slotting.lock_store_bins(db, store_id)
    received, rejected = [], []
    for line in lines:
        sku = inventory.get_sku(db, line["sku_id"])
        view = scan(db, receipt_id, sku.barcode, line["qty"], line.get("batch_no"), line.get("expiry_date"),
                    commit=False)
        scanned = view["scan"]
        if scanned["outcome"] != "on_dock":
            rejected.append({"sku_id": sku.id, "sku": sku.name, "batch_no": scanned["batch_no"], "qty": line["qty"],
                             "reason": scanned["reason"]})
            continue
        expiry = next(row["expiry_date"] for row in view["lines"]
                      if row["sku_id"] == sku.id and row["batch_no"] == scanned["batch_no"])
        if line.get("bin_id"):
            bin_ = db.get(Bin, line["bin_id"])
            if bin_ is None or bin_.store_id != store_id:
                raise DomainError(422, "bad_bin", f"Bin {line['bin_id']} is not in store {store_id}")
            portions = [(bin_, line["qty"])]
        else:                       # as many bins as it takes, never over capacity
            portions = slotting.place(db, store_id, sku.id, line["qty"])
        for bin_, qty in portions:
            put_away(db, receipt_id, sku.id, scanned["batch_no"], bin_.code, qty, actor, commit=False)
            received.append({"sku_id": sku.id, "sku": sku.name, "qty": qty, "bin": bin_.code,
                             "batch_no": scanned["batch_no"], "expiry_date": expiry})
    report = close_receipt(db, receipt_id, commit=False)["report"]
    discrepancies = [{"sku_id": row["sku_id"], "sku": row["sku"], "ordered": row["ordered"],
                      "received": row["received"]}
                     for row in report if transfer_order_id is not None and row["received"] != row["ordered"]]
    if commit:
        db.commit()
    return {"store_id": store_id, "transfer_order_id": transfer_order_id, "receipt_id": receipt_id,
            "received": received, "discrepancies": discrepancies, "rejected": rejected}
