"""
refresh_parcels.py — CLI: 读取 tracking_numbers.txt 或命令行参数，调用 track_bulk 灌库。
用法:
    python refresh_parcels.py                      # 读取 tracking_numbers.txt（本地Chrome）
    python refresh_parcels.py --mode bit --bit-id xxx   # 使用Bit浏览器
    python refresh_parcels.py 9300110990413301433581 9300110990413301436223
"""
import argparse
import os
import sys
import time

from usps_direct_tracker import (
    track_bulk, load_tracking_numbers,
    BROWSER_MODE_LOCAL, BROWSER_MODE_BIT,
)
import parcel_store


def main():
    parser = argparse.ArgumentParser(description="Refresh parcels: import + track_bulk → parcels.json")
    parser.add_argument("tracking_numbers", nargs="*", help="Tracking numbers to refresh")
    parser.add_argument("--file", "-f", help="File with tracking numbers (one per line)")
    parser.add_argument("--import-only", action="store_true", help="Only import, skip tracking")
    parser.add_argument(
        "--mode", "-m",
        choices=[BROWSER_MODE_LOCAL, BROWSER_MODE_BIT],
        default=BROWSER_MODE_LOCAL,
        help="浏览器模式: local=本地无头Chrome(默认), bit=Bit浏览器",
    )
    parser.add_argument(
        "--bit-id",
        default=os.environ.get("BIT_BROWSER_ID", ""),
        help="Bit 浏览器窗口 ID (mode=bit 时必填)",
    )
    args = parser.parse_args()

    numbers = list(args.tracking_numbers)
    if args.file:
        numbers.extend(load_tracking_numbers(args.file))
    if not numbers:
        default_file = os.path.join(os.path.dirname(__file__), "tracking_numbers.txt")
        if os.path.exists(default_file):
            print(f"Loading from {default_file}...")
            numbers = load_tracking_numbers(default_file)
    if not numbers:
        print("No tracking numbers provided.", file=sys.stderr)
        sys.exit(1)

    seen = set()
    numbers = [n for n in numbers if n not in seen and not seen.add(n)]

    print(f"[1/3] Importing {len(numbers)} tracking number(s) into store...")
    added = parcel_store.merge_tracking_numbers(numbers)
    print(f"       -> {added} new, {len(numbers) - added} already existed")

    if args.import_only:
        print("[Done] Import only mode, skipping tracking.")
        return

    if args.mode == BROWSER_MODE_BIT and not args.bit_id:
        print("Error: --bit-id is required when --mode=bit (or set BIT_BROWSER_ID env var)", file=sys.stderr)
        sys.exit(1)

    mode_label = "Bit Browser" if args.mode == BROWSER_MODE_BIT else "Local Chrome"
    print(f"[2/3] Running track_bulk for {len(numbers)} number(s) ({mode_label})...")
    start = time.time()
    results = track_bulk(
        numbers,
        browser_mode=args.mode,
        bit_browser_id=args.bit_id or None,
    )
    elapsed = time.time() - start
    print(f"       -> Tracking completed in {elapsed:.1f}s")

    print("[3/3] Merging results into store...")
    parcel_store.apply_track_results(results)

    stats = parcel_store.stats_by_status()
    print(f"\n{'='*40}")
    print("Store stats:")
    for status, count in stats.items():
        if count > 0 or status == "全部":
            print(f"  {status}: {count}")
    print(f"{'='*40}")
    print("[Done]")


if __name__ == "__main__":
    main()
