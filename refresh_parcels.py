"""
refresh_parcels.py — CLI: 读取 tracking_numbers.txt 或命令行参数，调用 track_bulk 灌库。
用法:
    python refresh_parcels.py                      # 读取 tracking_numbers.txt
    python refresh_parcels.py 9300110990413301433581 9300110990413301436223
"""
import argparse
import os
import sys
import time

from usps_direct_tracker import track_bulk, load_tracking_numbers
import parcel_store


def main():
    parser = argparse.ArgumentParser(description="Refresh parcels: import + track_bulk → parcels.json")
    parser.add_argument("tracking_numbers", nargs="*", help="Tracking numbers to refresh")
    parser.add_argument("--file", "-f", help="File with tracking numbers (one per line)")
    parser.add_argument("--import-only", action="store_true", help="Only import, skip tracking")
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

    print(f"[2/3] Running track_bulk for {len(numbers)} number(s)...")
    start = time.time()
    results = track_bulk(numbers)
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
