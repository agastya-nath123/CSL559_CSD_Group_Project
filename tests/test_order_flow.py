"""Order to dispatch (HLD 6.1): paid order -> pick -> scan -> pack -> rider."""
from .conftest import assert_stock_is_consistent, atp, place_paid_order


def next_job(client, picker="picker-1", store_id=1):
    return client.get(f"/v1/pickers/{picker}/next-task", params={"store_id": store_id}).json()["job"]


def scan_all(client, job):
    for task in job["tasks"]:
        if task["status"] == "pending":
            res = client.post(f"/v1/pick-tasks/{task['task_id']}/scans",
                              json={"barcode": task["barcode"], "qty": task["qty"]})
            assert res.json()["match"] is True


def test_paid_order_is_picked_packed_and_handed_over(client, sku):
    rice, tea = sku("Basmati rice"), sku("Tea")
    place_paid_order(client, "order-1", 1, [(rice, 2), (tea, 1)])

    job = next_job(client)
    assert job["state"] == "picking" and len(job["tasks"]) == 2
    assert [t["seq"] for t in job["tasks"]] == [1, 2]

    scan_all(client, job)
    packed = client.post(f"/v1/fulfilment-jobs/{job['job_id']}/packed", json={"crate_slot": "S-07"}).json()
    assert packed["state"] == "packed" and packed["crate_slot"] == "S-07"

    # The sale is committed at packing: stock leaves on-hand and the hold ends.
    row = client.get("/v1/stores/1/availability", params={"skus": str(rice)}).json()[0]
    assert (row["on_hand"], row["reserved"]) == (28, 0)

    rider = packed["rider"]["rider_id"]                     # the rider system sent someone when it was packed
    handed = client.post(f"/v1/fulfilment-jobs/{job['job_id']}/handover",
                         json={"rider_id": rider, "scanned_order_id": "order-1"}).json()
    assert handed["state"] == "handed_over"
    event_types = [e["type"] for e in client.get("/v1/events", params={"limit": 50}).json()]
    assert "order.paid" in event_types and "order.ready_for_pickup" in event_types
    assert_stock_is_consistent()


def test_oldest_batch_is_picked_first(client, sku):
    """FEFO: store 1 has 6 milk expiring tomorrow and 24 expiring later."""
    place_paid_order(client, "order-1", 1, [(sku("Toned milk"), 8)])
    tasks = next_job(client)["tasks"]
    assert [(t["batch_no"], t["qty"]) for t in tasks] == [("M-OLD", 6), ("M-NEW", 2)]


def test_wrong_barcode_changes_nothing(client, sku):
    place_paid_order(client, "order-1", 1, [(sku("Basmati rice"), 1)])
    job = next_job(client)
    task = job["tasks"][0]
    res = client.post(f"/v1/pick-tasks/{task['task_id']}/scans", json={"barcode": "0000", "qty": 1}).json()
    assert res["match"] is False
    blocked = client.post(f"/v1/fulfilment-jobs/{job['job_id']}/packed", json={"crate_slot": "S-01"})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "lines_pending"


def test_missing_item_is_picked_from_another_bin(client, sku):
    milk = sku("Toned milk")
    place_paid_order(client, "order-1", 1, [(milk, 4)])
    job = next_job(client)
    first = job["tasks"][0]
    assert first["batch_no"] == "M-OLD"

    result = client.post(f"/v1/pick-tasks/{first['task_id']}/short").json()
    assert (result["reallocated"], result["dropped"]) == (4, 0)
    pending = [t for t in result["job"]["tasks"] if t["status"] == "pending"]
    assert [(t["batch_no"], t["qty"]) for t in pending] == [("M-NEW", 4)]

    scan_all(client, result["job"])
    client.post(f"/v1/fulfilment-jobs/{job['job_id']}/packed", json={"crate_slot": "S-01"})
    row = client.get("/v1/stores/1/availability", params={"skus": str(milk)}).json()[0]
    assert (row["on_hand"], row["reserved"]) == (20, 0)        # 30 - all 6 of the empty M-OLD written off - 4 sold
    assert_stock_is_consistent()


def test_missing_item_with_no_other_stock_is_dropped(client, sku):
    ice_cream, rice = sku("Vanilla ice cream"), sku("Basmati rice")
    place_paid_order(client, "order-1", 1, [(ice_cream, 1), (rice, 1)])
    job = next_job(client)
    ice_task = next(t for t in job["tasks"] if t["sku_id"] == ice_cream)

    result = client.post(f"/v1/pick-tasks/{ice_task['task_id']}/short").json()
    assert (result["reallocated"], result["dropped"]) == (0, 1)

    scan_all(client, result["job"])
    packed = client.post(f"/v1/fulfilment-jobs/{job['job_id']}/packed", json={"crate_slot": "S-01"}).json()
    assert packed["state"] == "packed"
    row = client.get("/v1/stores/1/availability", params={"skus": str(ice_cream)}).json()[0]
    assert (row["on_hand"], row["reserved"]) == (0, 0)
    assert_stock_is_consistent()


def test_two_pickers_get_different_orders(client, sku):
    rice = sku("Basmati rice")
    place_paid_order(client, "order-1", 1, [(rice, 1)])
    place_paid_order(client, "order-2", 1, [(rice, 1)])
    a, b = next_job(client, "picker-a"), next_job(client, "picker-b")
    assert a["order_id"] != b["order_id"]
    assert next_job(client, "picker-a")["job_id"] == a["job_id"]     # asking again returns the same order
    assert next_job(client, "picker-c") is None                      # nothing left to claim


def test_store_turns_busy_when_too_many_orders_wait(client, sku):
    rice = sku("Basmati rice")
    home = {"lat": 12.975, "lng": 77.60}
    calm = client.get("/v1/serviceability", params=home).json()
    for n in range(6):                                               # threshold is 5 waiting orders
        place_paid_order(client, f"order-{n}", 1, [(rice, 1)])
    rushed = client.get("/v1/serviceability", params=home).json()
    assert (calm["status"], rushed["status"]) == ("open", "busy")
    assert rushed["eta_minutes"] == calm["eta_minutes"] + 5
    assert atp(client, 1, rice) == 24


def test_units_already_in_the_tote_are_not_handed_out_again(client, sku):
    """Scan the 6 old-batch milk, then report the newer bin empty. The 6 scanned units must not
    be offered as the replacement: they are already in this order's tote."""
    milk = sku("Toned milk")
    place_paid_order(client, "order-1", 1, [(milk, 8)])
    job = next_job(client)
    old_batch, new_batch = job["tasks"]
    client.post(f"/v1/pick-tasks/{old_batch['task_id']}/scans",
                json={"barcode": old_batch["barcode"], "qty": old_batch["qty"]})

    result = client.post(f"/v1/pick-tasks/{new_batch['task_id']}/short").json()
    assert (result["reallocated"], result["dropped"]) == (0, 2)

    packed = client.post(f"/v1/fulfilment-jobs/{job['job_id']}/packed", json={"crate_slot": "S-01"})
    assert packed.status_code == 200 and packed.json()["state"] == "packed"
    row = client.get("/v1/stores/1/availability", params={"skus": str(milk)}).json()[0]
    assert (row["on_hand"], row["reserved"]) == (0, 0)         # 30 - 6 sold - all 24 of the empty M-NEW written off
    assert_stock_is_consistent()
