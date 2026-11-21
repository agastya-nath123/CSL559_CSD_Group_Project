from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from ...auth import check_store, current_staff, require_role
from ...db import get_db
from ...idempotency import run_once
from ...ids import MAX_ID, PathId
from . import service

router = APIRouter(tags=["slotting"])


@router.get("/v1/stores/{store_id}/bins")
def bins(store_id: PathId, db: Session = Depends(get_db),
         staff: dict = Depends(current_staff)):
    check_store(staff, store_id)
    return service.list_bins(db, store_id)


@router.get("/v1/slotting/suggest")
def suggest(store_id: int = Query(ge=1, le=MAX_ID), sku_id: int = Query(ge=1, le=MAX_ID),
            db: Session = Depends(get_db), staff: dict = Depends(current_staff)):
    check_store(staff, store_id)
    b = service.suggest_bin(db, store_id, sku_id)
    return {"bin_id": b.id, "code": b.code, "zone": b.zone}


@router.get("/v1/slotting/plan", summary="Where each SKU should live: fast movers near the packing desk (look only)")
def reslotting_plan(store_id: int = Query(ge=1, le=MAX_ID),
                    days: int = Query(service.VELOCITY_DAYS, ge=1, le=90,
                                      description="Whole days of sales to rank the SKUs by"),
                    db: Session = Depends(get_db), staff: dict = Depends(current_staff)):
    """Per zone, the fastest-selling SKU gets the bin nearest the packing desk, the next one the next bin (`homes`),
    and `moves` lists the batches to move there. SKUs that did not sell stay where they are unless a seller needs
    their bin. Changes nothing: `POST /v1/slotting/apply` does the moves."""
    check_store(staff, store_id)
    return service.plan(db, store_id, days)


@router.post("/v1/slotting/apply", summary="Move the stock as the plan says (managers)")
def apply_reslotting(store_id: int = Query(ge=1, le=MAX_ID),
                     days: int = Query(service.VELOCITY_DAYS, ge=1, le=90,
                                       description="The same days as the plan you looked at"),
                     db: Session = Depends(get_db), staff: dict = Depends(require_role("manager")),
                     idempotency_key: str | None = Header(
                         None, min_length=1, max_length=80,
                         description="Any unique string. A retry with the same key moves nothing again; it returns "
                                     "the first answer.")):
    """Moves each planned batch with inventory.move (two ledger rows, stock unchanged), never past a bin's capacity.
    `skipped` lists what stayed, with the reason: a batch a picker has been sent to (`promised_to_picker`), one with
    no room even after the other moves (`no_room`), or any other refusal; one skipped batch never stops the others.
    Records the moves at once, so carry the stock straight away, at a quiet time."""
    check_store(staff, store_id)
    return run_once(db, "slotting-apply", idempotency_key, {"store_id": store_id, "days": days, "by": staff["sub"]},
                    lambda: service.apply(db, store_id, days, actor=staff["sub"], commit=False))
