"""Substitutes for dropped lines (Member 2 guide, task 2.4): the offer, the refund event, and accepting the offer
(POST /v1/fulfilment-jobs/{id}/substitutions, on Member 1's inventory.swap_line).

Store 1 has one vanilla ice cream (Rs 120, Frozen) and 30 frozen green peas (Rs 95, Frozen); Staples are rice (Rs 140),
oil (Rs 155), sugar (Rs 48) and atta (Rs 245), 30 of each, each in one bin."""
from datetime import date, timedelta

from sqlalchemy import select

from app.models import StockItem
from app.modules.pickpack import service as pickpack

from .conftest import assert_stock_is_consistent, atp, place_paid_order
from .test_order_flow import next_job


def events_of(client, event_type: str) -> list[dict]:
    return [e["payload"] for e in client.get("/v1/events", params={"limit": 200}).json() if e["type"] == event_type]


def hold(client, order_id: str, sku_id: int, qty: int) -> None:
    """Another customer's unpaid hold, to take stock out of what customers can buy."""
    res = client.post("/v1/reservations", json={"order_id": order_id, "store_id": 1,
                                                "lines": [{"sku_id": sku_id, "qty": qty}]})
    assert res.status_code == 201, res.text


def report_empty(client, sku_id: int, order_qty: int = 1, found: int | None = None, order_id: str = "s-1") -> dict:
    """Pay for `order_qty` of one item, claim it, and report its first line short."""
    place_paid_order(client, order_id, 1, [(sku_id, order_qty)])
    return report_next(client, found)


def report_next(client, found: int | None = None) -> dict:
    """Claim the oldest paid order and report its first line short."""
    line = next_job(client)["tasks"][0]
    res = client.post(f"/v1/pick-tasks/{line['task_id']}/short", json={"found": found} if found is not None else None)
    assert res.status_code == 200, res.text
    return res.json()


def test_the_last_ice_cream_is_missing_so_peas_are_offered_and_the_line_refunded(client, sku):
    ice_cream, peas = sku("Vanilla ice cream"), sku("Frozen green peas")
    peas_before = atp(client, 1, peas)

    result = report_empty(client, ice_cream)

    assert result["dropped"] == 1
    assert result["substitute_offer"] == {"sku_id": peas, "name": "Frozen green peas 500 g", "price": 95}
    assert result["message"].endswith("Possible substitute: Frozen green peas 500 g (Rs 95).")
    job_id = result["job"]["job_id"]
    assert events_of(client, "order.line_dropped") == [
        {"order_id": "s-1", "job_id": job_id, "sku_id": ice_cream, "qty": 1, "refund_amount": 120}]
    assert atp(client, 1, peas) == peas_before              # an offer holds nothing
    assert_stock_is_consistent()


def test_with_nothing_left_in_the_category_the_offer_is_null_but_the_refund_is_still_published(client, sku):
    ice_cream, peas = sku("Vanilla ice cream"), sku("Frozen green peas")
    hold(client, "all-the-peas", peas, atp(client, 1, peas))

    result = report_empty(client, ice_cream)

    assert result["dropped"] == 1 and result["substitute_offer"] is None
    assert "substitute" not in result["message"]
    assert [e["sku_id"] for e in events_of(client, "order.line_dropped")] == [ice_cream]
    assert_stock_is_consistent()


def test_the_nearest_price_wins_among_items_customers_can_still_buy_and_never_a_dearer_one(client, sku):
    atta, oil, rice, sugar = sku("Wheat atta"), sku("Sunflower oil"), sku("Basmati rice"), sku("Sugar")
    # Both atta orders are paid before the shelf is found empty: after that, atta cannot be bought any more.
    place_paid_order(client, "s-1", 1, [(atta, 6)])
    place_paid_order(client, "s-2", 1, [(atta, 3)])

    first = report_next(client, found=4)                                       # s-1: 2 atta dropped, Rs 245 each
    assert first["substitute_offer"]["sku_id"] == oil                          # Rs 155: 90 away (rice 105, sugar 197)
    assert events_of(client, "order.line_dropped")[0]["refund_amount"] == 490

    hold(client, "most-of-the-oil", oil, atp(client, 1, oil) - 1)              # 1 oil left; 2 are needed
    client.post(f"/v1/fulfilment-jobs/{first['job']['job_id']}/packed", json={"crate_slot": "S-01"})
    second = report_next(client, found=1)                                      # s-2: 2 more atta dropped
    assert second["substitute_offer"]["sku_id"] == rice                        # Rs 140: 105 away

    client.post(f"/v1/fulfilment-jobs/{second['job']['job_id']}/packed", json={"crate_slot": "S-02"})
    third = report_empty(client, rice, order_qty=6, found=4, order_id="s-3")   # rice is Rs 140; oil (155) is dearer
    assert third["substitute_offer"]["sku_id"] == sugar                        # Rs 48: the only one not dearer
    assert_stock_is_consistent()


def test_a_line_moved_to_another_bin_is_not_refunded_and_gets_no_offer(client, sku):
    result = report_empty(client, sku("Toned milk"), order_qty=4)   # M-OLD empty; M-NEW has plenty

    assert (result["reallocated"], result["dropped"], result["substitute_offer"]) == (4, 0, None)
    assert events_of(client, "order.line_dropped") == []
    assert_stock_is_consistent()


def test_units_that_cannot_be_found_when_the_order_is_paid_are_refunded_too(client, db, sku):
    """Batch M-OLD expired after the hold was placed but before the hourly write-off: only 24 milk can be picked."""
    milk = sku("Toned milk")
    hold_id = client.post("/v1/reservations", json={"order_id": "s-9", "store_id": 1,
                                                    "lines": [{"sku_id": milk, "qty": 26}]}).json()["reservation_id"]
    db.scalar(select(StockItem).where(StockItem.batch_no == "M-OLD")).expiry_date = date.today() - timedelta(days=1)
    db.commit()

    assert client.post(f"/v1/reservations/{hold_id}/confirm").status_code == 200

    tasks = next_job(client)["tasks"]
    assert sorted((t["status"], t["qty"]) for t in tasks) == [("pending", 24), ("short", 2)]
    [dropped] = events_of(client, "order.line_dropped")
    assert (dropped["order_id"], dropped["sku_id"], dropped["qty"], dropped["refund_amount"]) == ("s-9", milk, 2, 56)
    assert_stock_is_consistent()


def test_find_substitute_looks_only_in_the_same_category_and_never_offers_a_dearer_item(db, sku):
    assert pickpack.find_substitute(db, 1, sku("White bread"), 1) is None       # the only Bakery item
    assert pickpack.find_substitute(db, 1, sku("Tea"), 1) is None               # the only Beverage
    assert pickpack.find_substitute(db, 1, sku("Vanilla ice cream"), 1)["sku_id"] == sku("Frozen green peas")
    assert pickpack.find_substitute(db, 1, sku("Frozen green peas"), 1) is None  # ice cream (Rs 120) is dearer


def test_dropped_lines_and_a_later_cancel_never_refund_more_than_the_order(client, sku):
    """Dropped units are refunded by order.line_dropped; a cancel afterwards refunds only what was still held."""
    rice, ice_cream = sku("Basmati rice"), sku("Vanilla ice cream")
    place_paid_order(client, "s-10", 1, [(rice, 6), (ice_cream, 1)])          # 6 x 140 + 120 = Rs 960
    job = next_job(client)
    rice_line = next(t for t in job["tasks"] if t["sku_id"] == rice)
    ice_line = next(t for t in job["tasks"] if t["sku_id"] == ice_cream)
    client.post(f"/v1/pick-tasks/{ice_line['task_id']}/short")               # Rs 120 dropped
    client.post(f"/v1/pick-tasks/{rice_line['task_id']}/short", json={"found": 4})   # 2 rice dropped: Rs 280

    client.post(f"/v1/fulfilment-jobs/{job['job_id']}/cancel", json={"reason": "Customer changed their mind"})

    dropped = sum(e["refund_amount"] for e in events_of(client, "order.line_dropped"))
    [cancelled] = events_of(client, "order.cancelled")
    assert (dropped, cancelled["refund_amount"]) == (400, 560)                 # the 4 rice in the tote
    assert dropped + cancelled["refund_amount"] == 6 * 140 + 120
    assert_stock_is_consistent()



# --------------------------------------------------------------------------- accepting the offer (second half)

def accept(client, job_id: int, dropped: int, substitute: int, qty: int = 1, **extra):
    return client.post(f"/v1/fulfilment-jobs/{job_id}/substitutions",
                       json={"dropped_sku_id": dropped, "substitute_sku_id": substitute, "qty": qty, **extra})


def stock(client, sku_id: int) -> tuple[int, int]:
    row = client.get("/v1/stores/1/availability", params={"skus": str(sku_id)}).json()[0]
    return row["on_hand"], row["reserved"]


def test_the_customer_takes_the_peas_for_the_missing_ice_cream(client, sku):
    """The guide's check: accepting creates a pending task for peas and lowers the peas ATP by 1."""
    ice_cream, peas = sku("Vanilla ice cream"), sku("Frozen green peas")
    peas_before = atp(client, 1, peas)
    offer = report_empty(client, ice_cream)["substitute_offer"]
    job_id = next_job(client)["job_id"]

    res = accept(client, job_id, ice_cream, offer["sku_id"])

    assert res.status_code == 200, res.text
    assert res.json()["message"] == "1 × Frozen green peas 500 g added to the order. Pick it on this walk."
    tasks = res.json()["job"]["tasks"]
    assert [(t["status"], t["qty"], t["bin"]) for t in tasks if t["sku_id"] == peas] == [("pending", 1, "F-01-02")]
    assert [t["status"] for t in tasks if t["sku_id"] == ice_cream] == ["substituted"]
    assert atp(client, 1, peas) == peas_before - 1
    [event] = events_of(client, "order.line_substituted")
    assert event == {"order_id": "s-1", "job_id": job_id, "dropped_sku_id": ice_cream, "substitute_sku_id": peas,
                     "qty": 1, "charge_amount": 95}
    peas_line = next(t for t in tasks if t["sku_id"] == peas)                     # pick and pack it as usual
    client.post(f"/v1/pick-tasks/{peas_line['task_id']}/scans", json={"barcode": peas_line["barcode"]})
    packed = client.post(f"/v1/fulfilment-jobs/{job_id}/packed", json={"crate_slot": "S-01"}).json()
    assert packed["state"] == "packed"
    assert stock(client, peas) == (29, 0)
    assert_stock_is_consistent()


def test_a_substitute_sold_out_since_the_offer_changes_nothing(client, sku):
    ice_cream, peas = sku("Vanilla ice cream"), sku("Frozen green peas")
    report_empty(client, ice_cream)
    job_id = next_job(client)["job_id"]
    hold(client, "someone-else", peas, atp(client, 1, peas))                   # every pea is held by another customer

    res = accept(client, job_id, ice_cream, peas)

    assert res.status_code == 409 and res.json()["error"]["code"] == "not_offered"
    assert res.json()["error"]["details"]["offered"] is None                     # nothing similar is in stock now
    tasks = client.get(f"/v1/fulfilment-jobs/{job_id}").json()["tasks"]
    assert [t["status"] for t in tasks] == ["short"]                             # still dropped, no peas line
    assert events_of(client, "order.line_substituted") == []
    assert_stock_is_consistent()


def test_only_units_the_order_dropped_can_be_replaced(client, sku):
    rice, sugar = sku("Basmati rice"), sku("Sugar")
    job = report_empty(client, rice, order_qty=6, found=4)                     # 4 picked, 2 dropped; sugar offered
    job_id = job["job"]["job_id"]

    too_many = accept(client, job_id, rice, sugar, qty=3)
    one = accept(client, job_id, rice, sugar, qty=1)
    again = accept(client, job_id, rice, sugar, qty=2)                         # only 1 dropped unit is left

    assert too_many.status_code == 409 and too_many.json()["error"]["code"] == "not_dropped"
    assert one.status_code == 200
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_dropped"
    rice_lines = sorted((t["status"], t["qty"]) for t in one.json()["job"]["tasks"] if t["sku_id"] == rice)
    assert rice_lines == [("picked", 4), ("short", 1), ("substituted", 1)]
    assert stock(client, sugar) == (30, 1)
    assert_stock_is_consistent()


def test_a_line_that_was_picked_cannot_be_swapped(client, sku):
    rice, sugar = sku("Basmati rice"), sku("Sugar")
    place_paid_order(client, "s-5", 1, [(rice, 1)])
    job = next_job(client)
    line = job["tasks"][0]
    client.post(f"/v1/pick-tasks/{line['task_id']}/scans", json={"barcode": line["barcode"]})

    res = accept(client, job["job_id"], rice, sugar)

    assert res.status_code == 409 and res.json()["error"]["code"] == "not_dropped"
    assert stock(client, sugar) == (30, 0)


def test_a_substitute_joins_only_an_order_still_being_picked(client, sku):
    ice_cream, peas = sku("Vanilla ice cream"), sku("Frozen green peas")
    report_empty(client, ice_cream)
    job_id = next_job(client)["job_id"]
    client.post(f"/v1/fulfilment-jobs/{job_id}/cancel", json={"reason": "Customer changed their mind"})

    res = accept(client, job_id, ice_cream, peas)

    assert res.status_code == 409 and res.json()["error"]["code"] == "job_not_picking"
    assert stock(client, peas) == (30, 0)


def test_cancelling_after_a_substitution_gives_the_substitute_back_and_refunds_it(client, sku):
    ice_cream, peas = sku("Vanilla ice cream"), sku("Frozen green peas")
    report_empty(client, ice_cream)
    job_id = next_job(client)["job_id"]
    accept(client, job_id, ice_cream, peas)

    client.post(f"/v1/fulfilment-jobs/{job_id}/cancel", json={"reason": "Customer changed their mind"})

    assert stock(client, peas) == (30, 0)
    refunded = events_of(client, "order.line_dropped")[0]["refund_amount"]      # the ice cream, Rs 120
    charged = events_of(client, "order.line_substituted")[0]["charge_amount"]   # the peas, Rs 95
    cancelled = events_of(client, "order.cancelled")[0]["refund_amount"]        # the peas again, Rs 95
    assert refunded - charged + cancelled == 120                               # the customer gets back what they paid
    assert_stock_is_consistent()


def test_another_pickers_handheld_or_store_cannot_accept(client, client_as, sku):
    ice_cream, peas = sku("Vanilla ice cream"), sku("Frozen green peas")
    report_empty(client, ice_cream)
    job_id = next_job(client)["job_id"]                                        # claimed by picker-1

    assert accept(client, job_id, ice_cream, peas, picker_id="picker-2").json()["error"]["code"] == "not_your_order"
    assert accept(client_as("picker", 2), job_id, ice_cream, peas).status_code == 403
    assert accept(client, job_id, ice_cream, peas, qty=0).status_code == 422
    assert stock(client, peas) == (30, 0)



def test_only_the_offered_substitute_can_be_accepted(client, sku):
    """Found in the review of #49 (Member 3): the server took any item, so whoever accepted could swap a cheap missing
    item for a dear one and charge the customer for it. Now only the offer is accepted, and the offer is never dearer
    than the missing item, so the customer is never charged more than they were refunded."""
    rice, sugar, oil, atta, ice_cream = (sku("Basmati rice"), sku("Sugar"), sku("Sunflower oil"), sku("Wheat atta"),
                                         sku("Vanilla ice cream"))
    offer = report_empty(client, rice)["substitute_offer"]                     # 1 rice dropped, Rs 140
    job_id = next_job(client)["job_id"]
    assert offer["sku_id"] == sugar

    for not_offered in (oil, atta, ice_cream):                                 # dearer, dearer, another category
        res = accept(client, job_id, rice, not_offered)
        assert res.status_code == 409 and res.json()["error"]["code"] == "not_offered"
        assert res.json()["error"]["details"]["offered"]["sku_id"] == sugar
    assert [stock(client, s)[1] for s in (oil, atta, ice_cream)] == [0, 0, 0]  # nothing held for them

    assert accept(client, job_id, rice, sugar).status_code == 200
    refunded = events_of(client, "order.line_dropped")[0]["refund_amount"]
    charged = events_of(client, "order.line_substituted")[0]["charge_amount"]
    assert (refunded, charged) == (140, 48) and charged <= refunded
    assert_stock_is_consistent()


def test_a_substitute_whose_only_batch_has_expired_is_refunded_and_the_reply_says_so(client, db, sku):
    """Found in the review of #49: ATP still counts a batch that expired but is not written off yet; allocate never
    picks it, so the substitute is dropped again in the same call. The reply now says so, and the money nets out."""
    ice_cream, peas = sku("Vanilla ice cream"), sku("Frozen green peas")
    report_empty(client, ice_cream)
    job_id = next_job(client)["job_id"]
    db.scalar(select(StockItem).where(StockItem.sku_id == peas, StockItem.store_id == 1)).expiry_date = (
        date.today() - timedelta(days=1))
    db.commit()

    res = accept(client, job_id, ice_cream, peas)

    assert res.status_code == 200
    assert res.json()["added"] == 0
    assert res.json()["message"] == ("Frozen green peas 500 g could not be found on the shelves after all, "
                                     "so it is refunded too.")
    assert [t["status"] for t in res.json()["job"]["tasks"] if t["sku_id"] == peas] == ["short"]
    charged = events_of(client, "order.line_substituted")[0]["charge_amount"]
    refunded_peas = [e["refund_amount"] for e in events_of(client, "order.line_dropped") if e["sku_id"] == peas]
    assert (charged, refunded_peas) == (95, [95])                     # charged and refunded: nothing owed
    assert stock(client, peas)[1] == 0                                # no hold left on the peas
    assert_stock_is_consistent()
