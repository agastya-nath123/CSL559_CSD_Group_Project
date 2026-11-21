"""Limits on the ids sent to Supply (forecasting, replenishment, transfer orders, slotting, assortment), as PR 52 did
for Inventory with app/ids.py: an impossible id is a 422 that never reaches a query, while an id that is merely
unknown is still looked up. Receiving has its own test in tests/test_receiving.py."""
import pytest

from app.ids import MAX_ID

from .test_id_limits import BAD_IDS, api  # noqa: F401  (api: the admin client, a server error comes back as a 500)

ROUTES = [
    ("POST", "/v1/forecasting/run?store_id={id}"),
    ("GET", "/v1/forecasts?store_id={id}"),
    ("GET", "/v1/forecasting/backtest?store_id={id}"),
    ("GET", "/v1/forecasting/compare?store_id={id}"),
    ("POST", "/v1/replenishment/run?store_id={id}"),
    ("PUT", "/v1/stores/{id}/reorder-settings/1"),
    ("PUT", "/v1/stores/1/reorder-settings/{id}"),
    ("GET", "/v1/transfer-orders?store_id={id}"),
    ("POST", "/v1/transfer-orders/{id}/dispatch"),
    ("GET", "/v1/stores/{id}/supply-kpis"),
    ("GET", "/v1/stores/{id}/bins"),
    ("GET", "/v1/slotting/suggest?store_id={id}&sku_id=1"),
    ("GET", "/v1/slotting/suggest?store_id=1&sku_id={id}"),
    ("GET", "/v1/slotting/plan?store_id={id}"),
    ("POST", "/v1/slotting/apply?store_id={id}"),
    ("GET", "/v1/stores/{id}/assortment"),
    ("POST", "/v1/stores/{id}/assortment"),
    ("GET", "/v1/stores/{id}/assortment/recommendations"),
    ("DELETE", "/v1/stores/{id}/assortment/1"),
    ("DELETE", "/v1/stores/1/assortment/{id}"),
]
# A valid body where the route takes one, so a 422 can only come from the id.
BODIES = {("PUT", "/v1/stores/{id}/reorder-settings/1"): {"reorder_stopped": True},
          ("PUT", "/v1/stores/1/reorder-settings/{id}"): {"reorder_stopped": True},
          ("POST", "/v1/stores/{id}/assortment"): {"sku_id": 1}}


@pytest.mark.parametrize("bad", BAD_IDS)
@pytest.mark.parametrize("method, route", ROUTES)
def test_an_impossible_id_is_refused(api, method, route, bad):
    response = api.request(method, route.format(id=bad), json=BODIES.get((method, route)))
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("bad", BAD_IDS)
def test_an_impossible_sku_id_in_the_assortment_body_is_refused(api, bad):
    assert api.post("/v1/stores/1/assortment", json={"sku_id": bad}).status_code == 422


@pytest.mark.parametrize("method, route", ROUTES)
def test_the_largest_possible_id_is_still_looked_up(api, method, route):
    response = api.request(method, route.format(id=MAX_ID), json=BODIES.get((method, route)))
    assert response.status_code not in (422, 500), response.text
