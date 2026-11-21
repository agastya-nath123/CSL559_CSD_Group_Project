"""Velocity-based slotting (Member 3 guide, task 3.6): how far does a picker walk for the same orders before and after
the re-slotting plan is applied?

HLD service 6 places fast movers near the packing station. This measures it with Member 2's distance model (task 2.6,
scripts/compare_picking.py): a walk from the packing desk out to the shelves and back costs twice the highest walk_seq
it visits (bin 1 is next to the desk).

On a fresh copy of the demo data with 60 days of generated sales, it draws random orders of 1-3 different items, each
item as likely as its share of the sales (velocity over the last 14 days), and finds the bin each item would be picked
from: first expiry first, then nearest, as inventory.allocate sends a picker. Then it applies the plan through
POST /v1/slotting/apply's service and measures the same orders again. Nothing is picked: the stock audit still holds.

    python scripts/compare_slotting.py [--orders 200] [--seed 7]

It never touches darkstore.db: it works on a temporary database.
"""
import argparse
import os
import random
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):          # run as a file: make `import app` work
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def draw_orders(speeds: dict[int, float], count: int, rng: random.Random) -> list[list[int]]:
    """`count` orders of 1-3 different SKUs, each SKU drawn in proportion to how fast it sells."""
    skus, weights = list(speeds), list(speeds.values())
    orders = []
    for _ in range(count):
        wanted = min(rng.randint(1, 3), len(skus))
        order: set[int] = set()
        while len(order) < wanted:
            order.add(rng.choices(skus, weights)[0])
        orders.append(sorted(order))
    return orders


def walk(db, store_id: int, order: list[int]) -> int:
    """Out to the furthest bin this order's items would be picked from, and back."""
    from app.modules.inventory import service as inventory

    furthest = 0
    for sku_id in order:
        picks, _short = inventory.allocate(db, store_id, sku_id, 1)
        furthest = max([furthest] + [item.bin.walk_seq for item, _qty in picks])
    return 2 * furthest


def _average(walks: list[int]) -> float:
    return sum(walks) / len(walks) if walks else 0.0


def _saving(group: dict) -> str:
    """The share of the walk saved, as a whole percentage ("0%", never "-0%")."""
    return f"{round(100 * (1 - group['after'] / group['before'])) if group['before'] else 0}%"


def measure(db, store_id: int, orders: int, random_seed: int) -> dict:
    """Draw `orders` random orders from the store's sales, measure the average walk, apply the plan (it commits)
    and measure the same orders again: for all orders, and for those of ambient items only."""
    from sqlalchemy import select

    from app.models import SKU
    from app.modules.slotting import service as slotting

    zone_of = dict(db.execute(select(SKU.id, SKU.storage_type)).all())
    drawn = draw_orders(slotting.velocity(db, store_id), orders, random.Random(random_seed))
    groups = {"all": drawn, "ambient_only": [order for order in drawn if all(zone_of[s] == "ambient" for s in order)]}
    before = {name: _average([walk(db, store_id, order) for order in group]) for name, group in groups.items()}
    applied = slotting.apply(db, store_id, actor="compare_slotting")
    after = {name: _average([walk(db, store_id, order) for order in group]) for name, group in groups.items()}
    result = {name: {"orders": len(group), "before": before[name], "after": after[name]}
              for name, group in groups.items()}
    return {"orders": len(drawn), "moved": len(applied["moved"]), "skipped": len(applied["skipped"]), **result}


def compare(orders: int = 200, random_seed: int = 7, store_id: int = 1) -> dict:
    """measure() on a fresh copy of the demo data with 60 days of generated sales."""
    from app import seed
    from app.db import SessionLocal
    from scripts import generate_sales

    seed.reset()
    with SessionLocal() as db:
        generate_sales.generate(store_id, db=db)
        return measure(db, store_id, orders, random_seed)


def report(result: dict) -> str:
    rows = [("All orders", result["all"]), ("Ambient items only", result["ambient_only"])]
    return "\n".join([
        f"{result['orders']} random orders in store 1, each item drawn by how fast it sold over the last 14 days.",
        "Distance: 2 x the furthest walk_seq visited per order, in bin steps (Member 2's model).", "",
        f"{'':24}{'orders':>7}{'before':>9}{'after':>8}{'saving':>8}",
        *[f"{label:24}{group['orders']:>7}{group['before']:>9.1f}{group['after']:>8.1f}{_saving(group):>8}"
          for label, group in rows], "",
        f"{result['moved']} batches moved, {result['skipped']} skipped. "
        f"Before re-slotting {result['all']['before']:.1f}, after re-slotting {result['all']['after']:.1f}: "
        f"the plan saves {_saving(result['all'])} of the walking.",
        "Slotting reorders bins within a temperature zone; the chilled and frozen bins are at the far end of the walk",
        "(walk_seq 13-18), so an order with any chilled or frozen item walks to them whatever the plan."])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--orders", type=int, default=200)
    parser.add_argument("--seed", type=int, default=7, help="random seed, so a run can be repeated")
    args = parser.parse_args()
    # Before anything imports app: a throwaway database, and no timed jobs.
    os.environ["DARKSTORE_DB"] = f"sqlite:///{tempfile.mkdtemp()}/compare_slotting.db"
    os.environ["BACKGROUND_JOBS"] = "off"
    print(report(compare(args.orders, args.seed)))


if __name__ == "__main__":
    main()
