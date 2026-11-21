"""Checkout race (Member 1 guide, task 1.8): many buyers, few units, and proof that nothing was oversold.

The script resets the demo data, sets one product to exactly --units on hand, then lets --buyers customers each
try to hold 1 unit at the same instant. It passes only if
  * exactly `units` holds succeed (201) and every other buyer is told 409 insufficient_stock,
  * the store then shows `units` held and 0 available, and
  * the stock audit (GET /v1/admin/stock-audit) finds nothing.

Start the app with the rate limit off, because this is one client making hundreds of calls:

    RATE_LIMIT_PER_SECOND=0 python -m uvicorn app.main:app          (PowerShell: $env:RATE_LIMIT_PER_SECOND=0; ...)
    python scripts/load_checkout.py [--url http://127.0.0.1:8000] [--buyers 200] [--units 50]

(127.0.0.1, not localhost: on Windows every new connection to "localhost" first tries IPv6 and loses about 2 s.)

THIS WIPES THE DATA of the app it is pointed at. It refuses any URL that is not this machine unless you add
--allow-remote, and it logs in as `admin` with --password (default: DEMO_PASSWORD).
The times are for one laptop; the counts are the result that matters.
"""
import argparse
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import httpx

if __package__ in (None, ""):          # run as a file: make `import app` work
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from scripts.load_test import percentile  # noqa: E402

DEFAULT_PRODUCT = "Basmati rice"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _ok(res: httpx.Response) -> httpx.Response:
    if res.status_code >= 400:
        raise RuntimeError(f"{res.request.method} {res.request.url.path} answered {res.status_code}: {res.text}")
    return res


def _level(admin, store_id: int, sku_id: int) -> dict:
    return _ok(admin.get(f"/v1/stores/{store_id}/availability", params={"skus": str(sku_id)})).json()[0]


def prepare(admin, store_id: int, units: int, sku_id: int | None = None) -> int:
    """Reset the demo data and leave exactly `units` of one SKU on hand, none held and no safety buffer.
    `admin` is a client already logged in as admin. Returns the SKU id (rice unless `sku_id` is given)."""
    _ok(admin.post("/v1/admin/reset"))
    if sku_id is None:
        sku_id = next(row["sku_id"] for row in _ok(admin.get(f"/v1/stores/{store_id}/availability")).json()
                      if row["name"].startswith(DEFAULT_PRODUCT))
    level = _level(admin, store_id, sku_id)
    if level["safety_buffer"]:
        _ok(admin.put(f"/v1/stores/{store_id}/stock-settings/{sku_id}", json={"safety_buffer": 0}))
    delta = units - level["on_hand"]
    if delta:
        bins = [row["bin_id"] for row in _ok(admin.get(f"/v1/stores/{store_id}/stock")).json()
                if row["sku_id"] == sku_id]
        if delta > 0 and not bins:
            raise RuntimeError(f"SKU {sku_id} has no bin in store {store_id} to add stock to; pick another --sku")
        _ok(admin.post("/v1/stock-adjustments", json={
            "store_id": store_id, "sku_id": sku_id, "delta": delta, "reason": "count",
            "bin_id": bins[0] if delta > 0 else None}))
    return sku_id


def race(make_client, buyers: int, store_id: int, sku_id: int) -> list[tuple[int, str, float]]:
    """`buyers` threads, each with its own client from `make_client()`, all ask to hold 1 unit at the same instant.
    Returns (status code, error code or "", seconds) per buyer."""
    run_id = uuid.uuid4().hex[:8]
    start = threading.Barrier(buyers)
    results: list[tuple[int, str, float]] = []
    lock = threading.Lock()

    def buy(n: int) -> None:
        client = make_client()
        try:
            start.wait()
            began = time.perf_counter()
            try:
                res = client.post("/v1/reservations", json={"order_id": f"race-{run_id}-{n}", "store_id": store_id,
                                                            "lines": [{"sku_id": sku_id, "qty": 1}]})
                status = res.status_code
                code = res.json().get("error", {}).get("code", "") if status >= 400 else ""
            except (httpx.HTTPError, ValueError) as exc:      # no answer at all: counted as a failure, status 0
                status, code = 0, type(exc).__name__
            with lock:
                results.append((status, code, time.perf_counter() - began))
        finally:
            client.close()

    threads = [threading.Thread(target=buy, args=(n,)) for n in range(buyers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def check(admin, results: list[tuple[int, str, float]], units: int, store_id: int, sku_id: int) -> dict:
    """Count the outcomes, read the stock back and run the audit. `problems` is empty when the run passed."""
    buyers = len(results)
    held = sum(1 for status, _, _ in results if status == 201)
    refused = sum(1 for status, code, _ in results if status == 409 and code == "insufficient_stock")
    other = Counter(f"{status} {code}".strip() for status, code, _ in results
                    if status != 201 and not (status == 409 and code == "insufficient_stock"))
    level = _level(admin, store_id, sku_id)
    mismatches = _ok(admin.get("/v1/admin/stock-audit")).json()["mismatches"]
    expected = min(units, buyers)

    problems = []
    if any(key.startswith("429") for key in other):
        problems.append("some buyers were refused with 429 by the rate limit: start the app with "
                        "RATE_LIMIT_PER_SECOND=0")
    if other:
        problems.append(f"unexpected answers: {dict(other)}")
    if held != expected:
        problems.append(f"{held} holds succeeded, expected exactly {expected}"
                        + (" (OVERSOLD)" if held > units else ""))
    if refused != buyers - expected:
        problems.append(f"{refused} buyers got 409 insufficient_stock, expected {buyers - expected}")
    if level["reserved"] != held or level["on_hand"] != units:
        problems.append(f"the store shows {level['on_hand']} on hand and {level['reserved']} held, "
                        f"expected {units} and {held}")
    if mismatches:
        problems.append(f"the stock audit found {len(mismatches)} mismatches: {mismatches}")

    times_ms = [seconds * 1000 for _, _, seconds in results]
    return {"buyers": buyers, "units": units, "held": held, "refused": refused, "other": dict(other),
            "on_hand_after": level["on_hand"], "reserved_after": level["reserved"], "atp_after": level["atp"],
            "audit_mismatches": len(mismatches), "p50_ms": round(percentile(times_ms, 0.50), 1),
            "p99_ms": round(percentile(times_ms, 0.99), 1), "problems": problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Many buyers race for a few units; fail if anything is oversold.")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="base URL of the running app")
    parser.add_argument("--buyers", type=int, default=200, help="customers trying to buy 1 unit each (default: 200)")
    parser.add_argument("--units", type=int, default=50, help="units on the shelf (default: 50)")
    parser.add_argument("--store", type=int, default=1, help="store to buy from (default: 1)")
    parser.add_argument("--sku", type=int, default=None, help=f"SKU id to fight over (default: {DEFAULT_PRODUCT})")
    parser.add_argument("--password", default=config.DEMO_PASSWORD, help="the admin account's password")
    parser.add_argument("--allow-remote", action="store_true",
                        help="run against an app that is not on this machine (its data is wiped)")
    args = parser.parse_args(argv)
    if args.buyers < 1 or args.units < 0:
        parser.error("--buyers must be at least 1 and --units at least 0")
    if urlparse(args.url).hostname not in LOCAL_HOSTS and not args.allow_remote:
        print(f"{args.url} is not this machine. This script wipes the app's data first; add --allow-remote if "
              "that is really what you want.", file=sys.stderr)
        return 2

    try:
        with httpx.Client(base_url=args.url, timeout=60) as admin:
            token = _ok(admin.post("/v1/auth/login", json={"username": "admin", "password": args.password}))
            admin.headers["Authorization"] = f"Bearer {token.json()['access_token']}"
            sku_id = prepare(admin, args.store, args.units, args.sku)
            results = race(lambda: httpx.Client(base_url=args.url, timeout=60), args.buyers, args.store, sku_id)
            summary = check(admin, results, args.units, args.store, sku_id)
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"Could not run against {args.url}: {exc}", file=sys.stderr)
        return 1

    print(f"{summary['buyers']} buyers, {summary['units']} units of SKU {sku_id} in store {args.store}, "
          f"against {args.url}\n")
    print(f"{'held (201)':<28}{summary['held']:>6}")
    print(f"{'refused (409)':<28}{summary['refused']:>6}")
    print(f"{'other answers':<28}{sum(summary['other'].values()):>6}")
    print(f"{'held after / available':<28}{summary['reserved_after']:>6} / {summary['atp_after']}")
    print(f"{'audit mismatches':<28}{summary['audit_mismatches']:>6}")
    print(f"{'response time p50 / p99 ms':<28}{summary['p50_ms']:>6} / {summary['p99_ms']}")
    if summary["problems"]:
        print("\nFAILED:", file=sys.stderr)
        for problem in summary["problems"]:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"\nPASSED: exactly {summary['held']} of {summary['buyers']} buyers got a unit, and the audit is clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
