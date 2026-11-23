from __future__ import annotations

import argparse
import os
import sys

from app import config  # noqa: F401


def reorder_check() -> int:
    from app.tools import run_reorder_check

    result = run_reorder_check(reason="daily reorder check")
    if not result.success:
        print(f"reorder check failed: {result.error_code} {result.message}")
        return 1
    low = [d for d in result.decisions if d.action != "enough_stock"]
    print(f"checked {len(result.decisions)} products, {len(low)} at or below their reorder point")
    for d in low:
        print(f"  {d.product_id} {d.name}: position {d.position} (physical {d.physical} - reserved {d.reserved} "
              f"+ on order {d.on_order}) <= reorder {d.reorder_level} -> {d.action} {d.qty or ''} "
              f"{d.supplier_name or ''} {d.po_number or ''}".rstrip())
    for po in result.purchase_orders:
        print(f"  {po.po_number} ({po.supplier_name}) waiting for approval, total Rs {po.total_cost:,.0f}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Scheduled jobs. Example: python -m app.jobs reorder-check")
    parser.add_argument("job", choices=["reorder-check"])
    parser.add_argument("--test-db", action="store_true", help="run against TEST_DATABASE_URL")
    args = parser.parse_args()
    if args.test_db:
        os.environ["USE_TEST_DB"] = "1"
    sys.exit(reorder_check())


if __name__ == "__main__":
    main()
