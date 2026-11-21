"""HTTP endpoints for the Inventory Service (HLD section 8.1)."""
from typing import Annotated

import h3
from fastapi import APIRouter, Depends, Header, Path, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ... import config
from ...idempotency import run_once
from ...auth import check_store, current_staff, require_role
from ...db import get_db
from ...errors import DomainError
from . import counts, service

router = APIRouter(tags=["inventory"])

# Upper limits on quantities people type or send. Without them a huge number (10**20) reached the database and came
# back as a 500 (an integer overflow); now it is a 422 that says which field is wrong.
MAX_LINE_UNITS = 1000          # one line of a customer's cart
MAX_BIN_UNITS = 100_000        # what one bin can hold: a counted quantity, or one stock adjustment

# The same for ids. Every id is a 32-bit database integer starting at 1, so anything outside that range cannot
# exist; it is refused here (422) and never reaches a query. `Id` is for request bodies, `PathId` for the URL.
MAX_ID = 2**31 - 1
Id = Annotated[int, Field(ge=1, le=MAX_ID)]
PathId = Annotated[int, Path(ge=1, le=MAX_ID)]
MAX_LEDGER_ROWS = 500          # most rows one ledger request returns


class LineIn(BaseModel):
    sku_id: Id
    qty: int = Field(gt=0, le=MAX_LINE_UNITS)


class ReservationIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=60)
    store_id: Id
    lines: list[LineIn] = Field(min_length=1)
    h3_cell: str | None = Field(default=None, max_length=16,
                                description="H3 cell of the delivery address, from GET /v1/serviceability")

    @field_validator("h3_cell")
    @classmethod
    def _cell_of_the_zone_size(cls, cell: str | None) -> str | None:
        if cell is not None and not (h3.is_valid_cell(cell) and h3.get_resolution(cell) == config.H3_RESOLUTION):
            raise ValueError(f"must be an H3 cell at resolution {config.H3_RESOLUTION}")
        return None if cell is None else h3.int_to_str(h3.str_to_int(cell))   # one spelling, so counts add up


class StockSettingsIn(BaseModel):
    safety_buffer: int = Field(ge=0, le=1000, description="Units hidden from customers to absorb small count errors")


class AdjustmentIn(BaseModel):
    store_id: Id
    sku_id: Id
    delta: int = Field(ge=-MAX_BIN_UNITS, le=MAX_BIN_UNITS)
    reason: str = Field(pattern="^(damage|expiry|theft|count)$")
    bin_id: Id | None = None


@router.put("/v1/stores/{store_id}/stock-settings/{sku_id}",
            summary="Set the safety buffer for one SKU in one store")
def update_stock_settings(store_id: PathId, sku_id: PathId, body: StockSettingsIn, db: Session = Depends(get_db),
                          staff: dict = Depends(require_role("manager"))):
    """ATP becomes on hand - reserved - safety buffer. Existing holds are not affected."""
    check_store(staff, store_id)
    return service.set_safety_buffer(db, store_id, sku_id, body.safety_buffer)


@router.get("/v1/stores/{store_id}/availability")
def get_availability(store_id: PathId,
                     skus: str | None = Query(None, max_length=2000, pattern=r"^[0-9,\s]*$",
                                              description="Comma-separated SKU ids; omit for all"),
                     db: Session = Depends(get_db)):
    sku_ids = [int(s) for s in skus.split(",") if s.strip()] if skus else None
    if sku_ids and not all(1 <= sku_id <= MAX_ID for sku_id in sku_ids):
        raise DomainError(422, "bad_sku_id", f"SKU ids are between 1 and {MAX_ID}")
    return service.availability(db, store_id, sku_ids)


@router.post("/v1/reservations", status_code=201)
def create_reservation(body: ReservationIn, db: Session = Depends(get_db)):
    res = service.reserve(db, body.order_id, body.store_id, [(line.sku_id, line.qty) for line in body.lines],
                          body.h3_cell)
    return service.reservation_view(res)


@router.get("/v1/reservations/{reservation_id}")
def read_reservation(reservation_id: str, db: Session = Depends(get_db)):
    return service.reservation_view(service.get_reservation(db, reservation_id))


@router.post("/v1/reservations/{reservation_id}/confirm")
def confirm_reservation(reservation_id: str, db: Session = Depends(get_db)):
    return service.reservation_view(service.confirm(db, reservation_id))


@router.delete("/v1/reservations/{reservation_id}")
def release_reservation(reservation_id: str, db: Session = Depends(get_db)):
    return service.reservation_view(service.release(db, reservation_id))


@router.post("/v1/stock-adjustments")
def create_adjustment(body: AdjustmentIn, db: Session = Depends(get_db),
                      staff: dict = Depends(require_role("manager")),
                      idempotency_key: str | None = Header(
                          None, min_length=1, max_length=80,
                          description="Any unique string. A retry with the same key does not change stock again; "
                                      "it returns the first answer.")):
    """Correct the count. Send an Idempotency-Key so a double-click or a network retry writes off only once.
    The stock ledger records the logged-in user as the one who did it."""
    check_store(staff, body.store_id)
    return run_once(db, "stock-adjustment", idempotency_key, body.model_dump(),
                    lambda: service.adjust(db, body.store_id, body.sku_id, body.delta, body.reason, body.bin_id,
                                           actor=staff["sub"], commit=False, shelf_rules=True))


@router.get("/v1/stores/{store_id}/stock")
def read_stock_by_bin(store_id: PathId, db: Session = Depends(get_db),
                      staff: dict = Depends(current_staff)):
    check_store(staff, store_id)
    return service.stock_by_bin(db, store_id)


@router.get("/v1/stores/{store_id}/ledger")
def read_ledger(store_id: PathId, limit: int = Query(50, ge=1, le=MAX_LEDGER_ROWS), db: Session = Depends(get_db),
                staff: dict = Depends(current_staff)):
    check_store(staff, store_id)
    return service.ledger(db, store_id, limit)


@router.post("/v1/admin/sweep-reservations", dependencies=[Depends(require_role("admin"))])
def sweep(db: Session = Depends(get_db)):
    """Release holds that have run out. In production this runs on a timer (see Member 1 guide)."""
    return {"released": service.sweep_expired(db)}


@router.post("/v1/admin/expire-stock", dependencies=[Depends(require_role("admin"))],
             summary="Write off every batch past its expiry date")
def expire_stock(store_id: int | None = Query(None, ge=1, le=MAX_ID, description="Omit for every store"),
                 db: Session = Depends(get_db)):
    """Also runs on its own every EXPIRY_INTERVAL_SECONDS (an hour by default)."""
    return {"units_written_off": service.expire_stock(db, store_id)}


@router.get("/v1/admin/stock-audit", dependencies=[Depends(require_role("admin"))],
            summary="Check that stock levels, bins and the ledger agree")
def stock_audit(store_id: int | None = Query(None, ge=1, le=MAX_ID, description="Omit for every store"),
                db: Session = Depends(get_db)):
    """`mismatches` has one entry per store and SKU whose records disagree; an empty list means stock is consistent.
    Run it after a load test (scripts/load_checkout.py does)."""
    mismatches = service.audit(db, store_id)
    return {"consistent": not mismatches, "mismatches": mismatches}


@router.get("/v1/stores/{store_id}/near-expiry", summary="Batches that expire within the next few days")
def read_near_expiry(store_id: PathId, days: int = Query(2, ge=0, le=30, description="0 means expiring today"),
                     db: Session = Depends(get_db), staff: dict = Depends(current_staff)):
    check_store(staff, store_id)
    return service.near_expiry(db, store_id, days)


# --------------------------------------------------------------------------- cycle counts (task 1.5, FR-11)


class CountStartIn(BaseModel):
    store_id: Id
    bin_id: Id


class CountedLineIn(BaseModel):
    sku_id: Id
    counted_qty: int = Field(ge=0, le=MAX_BIN_UNITS)


class CountSubmitIn(BaseModel):
    lines: list[CountedLineIn] = Field(min_length=1)

    @field_validator("lines")
    @classmethod
    def _one_line_per_sku(cls, lines: list[CountedLineIn]) -> list[CountedLineIn]:
        if len({line.sku_id for line in lines}) != len(lines):
            raise ValueError("give each SKU once")
        return lines


COUNTERS = ("picker", "receiver", "manager")


def _count_in_my_store(db: Session, count_id: int, staff: dict):
    count = counts.get_count(db, count_id)
    check_store(staff, count.store_id)
    return count


@router.post("/v1/cycle-counts", status_code=201, summary="Start a blind count of one bin")
def start_count(body: CountStartIn, db: Session = Depends(get_db), staff: dict = Depends(require_role(*COUNTERS))):
    """Returns the SKUs to look for, without quantities. A bin already being counted returns that count."""
    check_store(staff, body.store_id)
    return counts.count_view(counts.start(db, body.store_id, body.bin_id, created_by=staff["sub"]))


@router.get("/v1/cycle-counts/{count_id}", summary="Read a count (no quantities while it is open)")
def read_count(count_id: PathId, db: Session = Depends(get_db), staff: dict = Depends(current_staff)):
    return counts.count_view(_count_in_my_store(db, count_id, staff))


@router.post("/v1/cycle-counts/{count_id}/submit", summary="Hand in what was counted")
def submit_count(count_id: PathId, body: CountSubmitIn, db: Session = Depends(get_db),
                 staff: dict = Depends(require_role(*COUNTERS))):
    """Small differences (worth at most COUNT_AUTO_APPROVE_VALUE) are applied at once; bigger ones wait for a
    manager."""
    _count_in_my_store(db, count_id, staff)
    counted = {line.sku_id: line.counted_qty for line in body.lines}
    return counts.count_view(counts.submit(db, count_id, counted, submitted_by=staff["sub"]))


@router.post("/v1/cycle-counts/{count_id}/approve", summary="Apply a count's differences to stock")
def approve_count(count_id: PathId, db: Session = Depends(get_db), staff: dict = Depends(require_role("manager"))):
    """A manager may not approve a count they submitted themselves (an admin may)."""
    _count_in_my_store(db, count_id, staff)
    return counts.count_view(counts.approve(db, count_id, decided_by=staff["sub"],
                                            may_approve_own=staff["role"] == "admin"))


@router.post("/v1/cycle-counts/{count_id}/reject", summary="Leave stock as it is")
def reject_count(count_id: PathId, db: Session = Depends(get_db), staff: dict = Depends(require_role("manager"))):
    _count_in_my_store(db, count_id, staff)
    return counts.count_view(counts.reject(db, count_id, decided_by=staff["sub"]))


@router.get("/v1/stores/{store_id}/cycle-counts", summary="A store's counts, newest first")
def list_counts(store_id: PathId, status: str | None = Query(None, pattern="^(open|submitted|approved|rejected)$",
                                                           description="submitted = waiting for a manager"),
                db: Session = Depends(get_db), staff: dict = Depends(require_role("manager"))):
    check_store(staff, store_id)
    return [counts.count_view(count) for count in counts.list_counts(db, store_id, status)]


@router.get("/v1/stores/{store_id}/inventory-accuracy",
            summary="Share of counted lines that matched the records (HLD 12 target: 99.5%)")
def read_inventory_accuracy(store_id: PathId, db: Session = Depends(get_db), staff: dict = Depends(current_staff)):
    check_store(staff, store_id)
    service.get_store(db, store_id)
    return counts.inventory_accuracy(db, store_id)

