"""USPS Direct Tracker - Uses Selenium to bypass Akamai, then reuses cookies for HTTP requests.

Strategy (based on usps-cli approach):
1. Use Selenium with a real browser to navigate to tools.usps.com and get past Akamai
2. Extract cookies from the browser session
3. Reuse those cookies for direct HTTP requests to fetch tracking data

This avoids the need for any third-party API or USPS API credentials.
"""

import argparse
import json
import os
import re
import time
from typing import List, Optional

import httpx
from selectolax.lexbor import LexborHTMLParser


COOKIES_FILE = os.path.join(os.path.dirname(__file__), "usps_cookies.json")
TRACKING_URL = "https://tools.usps.com/go/TrackConfirmAction?qtc_tLabels1={tracking}"


def _generate_cookies_via_selenium(tracking_number: str = "9400111899223197969528") -> dict:
    """Use Selenium to get past Akamai and extract cookies."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        raise ImportError("selenium is required. Install with: pip install selenium")

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Try to find ChromeDriver
    try:
        driver = webdriver.Chrome(options=options)
    except Exception:
        try:
            from selenium.webdriver.firefox.options import Options as FirefoxOptions
            from selenium.webdriver.firefox.service import Service as FirefoxService
            firefox_opts = FirefoxOptions()
            firefox_opts.add_argument("--headless")
            driver = webdriver.Firefox(options=firefox_opts)
        except Exception as e:
            raise RuntimeError(
                "Could not launch any browser. Install Chrome or Firefox and their WebDriver. "
                f"Error: {e}"
            )

    # Anti-detection
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )

    url = TRACKING_URL.format(tracking=tracking_number)
    print(f"  Loading {url} via browser to bypass Akamai...")
    driver.get(url)

    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CLASS_NAME, "tracking-number"))
        )
    except Exception:
        # Check if page loaded at all
        page_source = driver.page_source
        if "Akamai" in page_source or len(page_source) < 500:
            print("  Warning: Akamai challenge may not have been bypassed")
        else:
            print("  Page loaded (no tracking-number class found, but content present)")

    # Wait a bit more for JS to settle
    time.sleep(3)

    cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
    page_source = driver.page_source
    driver.quit()

    # Save cookies
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2)
    print(f"  Saved {len(cookies)} cookies to {COOKIES_FILE}")

    return cookies, page_source


def _load_cookies() -> Optional[dict]:
    """Load previously saved cookies."""
    if os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        print(f"  Loaded {len(cookies)} cookies from {COOKIES_FILE}")
        return cookies
    return None


def _parse_tracking_html(html: str, tracking_number: str) -> dict:
    """Parse tracking information from USPS HTML page."""
    tree = LexborHTMLParser(html)

    # Check for valid tracking content
    status_selectors = [
        ".preshipment-status", ".shipping-partner-status",
        ".delivery-attempt-status", ".addressee-unknown-status",
        ".current-step", ".tb-step",
    ]

    has_tracking = any(tree.css_matches(sel) for sel in status_selectors)

    if not has_tracking:
        # Check for error messages
        error_el = tree.css_first(".red-banner > .banner-header")
        if error_el:
            return {
                "tracking_number": tracking_number,
                "error": None,
                "data": {
                    "status": error_el.text(strip=True),
                    "events": [],
                },
            }
        return {
            "tracking_number": tracking_number,
            "error": "No tracking data found in HTML",
            "data": None,
        }

    # Extract status
    status_el = tree.css_first(".banner-content")
    status = status_el.text(strip=True) if status_el else "Unknown"

    # Extract steps/events
    events = []
    for step in tree.css(".tb-step:not(.toggle-history-container)"):
        date_el = step.css_first(".tb-date")
        location_el = step.css_first(".tb-location")
        detail_el = step.css_first(".tb-status-detail")

        date_str = ""
        if date_el:
            lines = [l.strip() for l in date_el.text().splitlines() if l.strip()]
            date_str = " ".join(lines[:2])

        location = location_el.text(strip=True) if location_el else ""
        detail = detail_el.text(strip=True) if detail_el else ""

        events.append({
            "date": date_str,
            "location": location,
            "description": detail,
        })

    # Estimated delivery
    est_delivery = ""
    date_el = tree.css_first(".date")
    month_year_el = tree.css_first(".month_year")
    if date_el and month_year_el:
        try:
            day = date_el.text().zfill(2)
            month_year_text = month_year_el.text().splitlines()[0].strip()
            month_year = month_year_text.split(" ")
            if len(month_year) >= 2:
                est_delivery = f"{month_year[0]} {day}, {month_year[1]}"
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


def track_single(tracking_number: str, cookies: dict, client: httpx.Client) -> dict:
    """Track a single package using saved cookies."""
    url = TRACKING_URL.format(tracking=tracking_number)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://tools.usps.com/",
    }

    try:
        resp = client.get(url, cookies=cookies, headers=headers, timeout=15, follow_redirects=True)
        html = resp.text

        if "originalHeaders" in html or len(html) < 500:
            return {
                "tracking_number": tracking_number,
                "error": "Akamai blocked (cookies expired or invalid)",
                "data": None,
            }

        return _parse_tracking_html(html, tracking_number)

    except Exception as e:
        return {
            "tracking_number": tracking_number,
            "error": str(e),
            "data": None,
        }


def track_bulk(
    tracking_numbers: List[str],
    cookies: Optional[dict] = None,
    regenerate_on_fail: bool = True,
) -> List[dict]:
    """Track multiple packages."""
    if cookies is None:
        cookies = _load_cookies()

    results = []
    need_regenerate = False

    with httpx.Client(timeout=15) as client:
        for i, tn in enumerate(tracking_numbers):
            if cookies is None:
                need_regenerate = True
                break

            print(f"  [{i+1}/{len(tracking_numbers)}] {tn}...", end=" ", flush=True)
            result = track_single(tn, cookies, client)
            results.append(result)

            if result.get("error") and "Akamai" in result.get("error", ""):
                print("BLOCKED")
                need_regenerate = True
                break
            elif result.get("error"):
                print(f"ERROR: {result['error'][:40]}")
            else:
                status = result.get("data", {}).get("status", "Unknown")
                events_count = len(result.get("data", {}).get("events", []))
                print(f"{status} ({events_count} events)")

            # Rate limiting
            if i < len(tracking_numbers) - 1:
                time.sleep(0.5)

    if need_regenerate and regenerate_on_fail:
        print("  Cookies expired or missing. Regenerating via browser...")
        first_tn = tracking_numbers[0] if tracking_numbers else "9400111899223197969528"
        new_cookies, first_html = _generate_cookies_via_selenium(first_tn)

        # Parse the first result from the browser HTML
        first_result = _parse_tracking_html(first_html, first_tn)
        results = [first_result]

        # Continue with the rest
        with httpx.Client(timeout=15) as client:
            for i, tn in enumerate(tracking_numbers[1:], 1):
                print(f"  [{i+1}/{len(tracking_numbers)}] {tn}...", end=" ", flush=True)
                result = track_single(tn, new_cookies, client)
                results.append(result)

                if result.get("error") and "Akamai" in result.get("error", ""):
                    print("BLOCKED - giving up")
                    for remaining_tn in tracking_numbers[i+1:]:
                        results.append({
                            "tracking_number": remaining_tn,
                            "error": "Akamai blocked",
                            "data": None,
                        })
                    break
                elif result.get("error"):
                    print(f"ERROR: {result['error'][:40]}")
                else:
                    status = result.get("data", {}).get("status", "Unknown")
                    events_count = len(result.get("data", {}).get("events", []))
                    print(f"{status} ({events_count} events)")

                if i < len(tracking_numbers) - 1:
                    time.sleep(0.5)

    return results


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
            lines.append(f"      ... +{len(events)-10} more events")
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
    parser = argparse.ArgumentParser(description="USPS Direct Tracker (Selenium + HTTP)")
    parser.add_argument("tracking_numbers", nargs="*")
    parser.add_argument("--file", "-f")
    parser.add_argument("--output", "-o")
    parser.add_argument("--refresh-cookies", action="store_true", help="Force regenerate cookies via browser")
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

    # Deduplicate
    seen = set()
    numbers = [n for n in numbers if n not in seen and not seen.add(n)]

    cookies = None
    if not args.refresh_cookies:
        cookies = _load_cookies()

    print(f"Tracking {len(numbers)} number(s) via USPS direct (Selenium + HTTP)...")
    results = track_bulk(numbers, cookies=cookies, regenerate_on_fail=True)

    print(chr(10) + "=" * 70)
    print("TRACKING RESULTS")
    print("=" * 70)
    for r in results:
        print(format_result(r))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"Results saved to {args.output}")


if __name__ == "__main__":
    main()
