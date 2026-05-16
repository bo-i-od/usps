import argparse
import json
import os
import time
from typing import List

from selectolax.lexbor import LexborHTMLParser


BATCH_SIZE = 35
MAX_RETRY = 1
BULK_TRACKING_URL = (
    "https://tools.usps.com/go/TrackConfirmAction"
    "?tRef=fullpage&tLc={count}&text28777=&tLabels={labels}&tABt=false"
)


def get_webdriver():
    """初始化并返回无头浏览器实例"""
    import selenium.webdriver as webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from webdriver_manager.chrome import ChromeDriverManager
    # 移除 webdriver_manager 避免网络连不上的报错

    print("    -> Setting up Chrome...")
    init_start = time.time()  # 记录浏览器启动开始时间

    options = ChromeOptions()
    options.add_argument("--headless=new")  # 开启无头模式
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.page_load_strategy = "eager"

    service = ChromeService(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    init_elapsed = time.time() - init_start  # 计算浏览器启动耗时
    print(f"    -> Chrome launched successfully in {init_elapsed:.2f}s!")

    print("    -> Applying anti-detection scripts...")
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    driver.set_page_load_timeout(60)
    driver.implicitly_wait(10)
    return driver


def _parse_container(container) -> dict:
    """解析单个 .track-bar-container 中的物流信息"""
    tn_el = container.css_first("span.tracking-number")
    if not tn_el:
        return None
    tracking_number = tn_el.attributes.get("value", "") or tn_el.text(strip=True)

    banner_el = container.css_first(".banner-content")
    status = banner_el.text(strip=True) if banner_el else "Unknown"

    events = []
    for step in container.css(".tb-step:not(.toggle-history-container)"):
        date_el = step.css_first(".tb-date")
        location_el = step.css_first(".tb-location")
        detail_el = step.css_first(".tb-status-detail")

        date_str = ""
        if date_el:
            lines = [l.strip() for l in date_el.text().splitlines() if l.strip()]
            date_str = " ".join(lines[:2])

        events.append({
            "date": date_str,
            "location": location_el.text(strip=True) if location_el else "",
            "description": detail_el.text(strip=True) if detail_el else "",
        })

    est_delivery = ""
    date_el = container.css_first(".date")
    month_year_el = container.css_first(".month_year")
    if date_el and month_year_el:
        try:
            day = date_el.text().zfill(2)
            parts = month_year_el.text().splitlines()[0].strip().split(" ")
            if len(parts) >= 2:
                est_delivery = f"{parts[0]} {day}, {parts[1]}"
        except Exception:
            pass

    return {
        "tracking_number": tracking_number,
        "error": None,
        "data": {
            "status": status,
            "estimated_delivery": est_delivery,
            "events": events,
        },
    }


def _parse_bulk_html(html: str, expected_numbers: List[str]) -> List[dict]:
    """从包含多个单号的 USPS 页面中解析所有物流信息"""
    tree = LexborHTMLParser(html)
    containers = tree.css(".track-bar-container")

    parsed = {}
    for container in containers:
        result = _parse_container(container)
        if result:
            parsed[result["tracking_number"]] = result

    results = []
    for tn in expected_numbers:
        if tn in parsed:
            results.append(parsed[tn])
        else:
            results.append({
                "tracking_number": tn,
                "error": "Not found in page response",
                "data": None,
            })
    return results


def track_batch(driver, batch: List[str]) -> List[dict]:
    """一次请求查询最多 35 个单号"""
    labels = "%2C".join(batch) + "%2C"
    url = BULK_TRACKING_URL.format(labels=labels, count=len(batch))

    try:
        driver.get(url)

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CLASS_NAME, "tracking-number"))
            )
        except Exception:
            pass

        time.sleep(1)

        html = driver.page_source
        if "Akamai" in html or len(html) < 500:
            return [
                {"tracking_number": tn, "error": "Akamai blocked the browser", "data": None}
                for tn in batch
            ]

        return _parse_bulk_html(html, batch)

    except Exception as e:
        return [
            {"tracking_number": tn, "error": str(e), "data": None}
            for tn in batch
        ]


def track_bulk(tracking_numbers: List[str], max_retry: int = MAX_RETRY) -> List[dict]:
    """批量追踪: 每次最多 35 个单号, 分批请求, 失败的进入重试队列"""
    results_map = {}
    total = len(tracking_numbers)
    batches = [
        tracking_numbers[i: i + BATCH_SIZE]
        for i in range(0, total, BATCH_SIZE)
    ]

    print(f"  Total: {total} numbers, split into {len(batches)} batch(es) of up to {BATCH_SIZE}")
    print(f"  Max retry: {max_retry}")
    print("  Initializing browser...")
    driver = get_webdriver()

    try:
        retry_queue: List[str] = []

        for bi, batch in enumerate(batches):
            batch_start = time.time()
            print(f"\n  === Batch {bi + 1}/{len(batches)} ({len(batch)} numbers) ===")

            batch_results = track_batch(driver, batch)

            for r in batch_results:
                tn = r["tracking_number"]
                if r.get("error"):
                    print(f"    {tn}: ERROR - {r['error'][:50]}")
                    retry_queue.append(tn)
                else:
                    results_map[tn] = r
                    status = (r.get("data") or {}).get("status", "Unknown")
                    n_events = len((r.get("data") or {}).get("events", []))
                    print(f"    {tn}: {status[:60]} ({n_events} events)")

            batch_elapsed = time.time() - batch_start
            print(f"  -> [Batch {bi + 1} completed in {batch_elapsed:.2f} seconds]")

            if bi < len(batches) - 1:
                print("  Waiting before next batch...")
                time.sleep(1)

        for attempt in range(1, max_retry + 1):
            if not retry_queue:
                break

            print(f"\n  {'=' * 50}")
            print(f"  RETRY round {attempt}/{max_retry} — {len(retry_queue)} number(s) to retry")
            print(f"  {'=' * 50}")

            retry_batches = [
                retry_queue[i: i + BATCH_SIZE]
                for i in range(0, len(retry_queue), BATCH_SIZE)
            ]
            next_retry_queue: List[str] = []

            for bi, batch in enumerate(retry_batches):
                batch_start = time.time()
                print(f"\n  === Retry Batch {bi + 1}/{len(retry_batches)} ({len(batch)} numbers) ===")

                batch_results = track_batch(driver, batch)

                for r in batch_results:
                    tn = r["tracking_number"]
                    if r.get("error"):
                        print(f"    {tn}: ERROR - {r['error'][:50]}")
                        next_retry_queue.append(tn)
                    else:
                        results_map[tn] = r
                        status = (r.get("data") or {}).get("status", "Unknown")
                        n_events = len((r.get("data") or {}).get("events", []))
                        print(f"    {tn}: {status[:60]} ({n_events} events)")

                batch_elapsed = time.time() - batch_start
                print(f"  -> [Retry Batch {bi + 1} completed in {batch_elapsed:.2f} seconds]")

                if bi < len(retry_batches) - 1:
                    print("  Waiting before next batch...")
                    time.sleep(1)

            retry_queue = next_retry_queue

        if retry_queue:
            print(f"\n  [!] {len(retry_queue)} number(s) still failed after {max_retry} retry(ies)")
            for tn in retry_queue:
                results_map[tn] = {
                    "tracking_number": tn,
                    "error": f"Failed after {max_retry} retry(ies)",
                    "data": None,
                }
    finally:
        driver.quit()

    return [results_map.get(tn, {"tracking_number": tn, "error": "Unknown failure", "data": None})
            for tn in tracking_numbers]


def format_result(r: dict) -> str:
    tn = r["tracking_number"]
    if r.get("error"):
        return f"  {tn}: ERROR - {r['error']}"
    d = r.get("data") or {}
    lines = [f"  {tn}: {d.get('status', 'Unknown')}"]
    if d.get("estimated_delivery"):
        lines.append(f"    Est. Delivery: {d['estimated_delivery']}")
    events = d.get("events") or []
    if events:
        lines.append(f"    Events ({len(events)}):")
        for ev in events[:10]:
            date = ev.get("date", "")
            desc = ev.get("description", "")
            loc = ev.get("location", "")
            loc_str = f" | {loc}" if loc else ""
            lines.append(f"      {date}{loc_str}: {desc}")
        if len(events) > 10:
            lines.append(f"      ... +{len(events) - 10} more events")
    return chr(10).join(lines)


def load_tracking_numbers(filepath: str) -> List[str]:
    numbers = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                numbers.append(line)
    return numbers


def main():
    script_start = time.time()  # 记录整个脚本开始时间

    parser = argparse.ArgumentParser(description="USPS Direct Tracker (Pure Selenium)")
    parser.add_argument("tracking_numbers", nargs="*")
    parser.add_argument("--file", "-f")
    parser.add_argument("--output", "-o")
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
        parser.error("No tracking numbers provided.")

    # 去重
    seen = set()
    numbers = [n for n in numbers if n not in seen and not seen.add(n)]

    print(f"Tracking {len(numbers)} number(s) via USPS (Pure Selenium)...")
    results = track_bulk(numbers)

    print(chr(10) + "=" * 70)
    print("TRACKING RESULTS")
    print("=" * 70)
    for r in results:
        print(format_result(r))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"Results saved to {args.output}")

    script_elapsed = time.time() - script_start  # 计算总耗时
    print(f"\n[Done] Total execution time: {script_elapsed:.2f} seconds.")


if __name__ == "__main__":
    main()