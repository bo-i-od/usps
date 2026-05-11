"""Track718 Direct API Bulk Tracker - No browser dependency!

Uses the reverse-engineered Track718 API sign computation to make
direct HTTP requests, eliminating the need for Playwright/browser.

Sign algorithm:
  1. AES-256-ECB encrypt JSON.stringify(data) with key h(c)
  2. Base64 encode the ciphertext
  3. MD5 hash the Base64 string

Where c = "482737d7ad3f29a2b43e73345830385a" (default key for <=10 tracks)
And for >10 tracks, key = h(longKey + "Track7182024xx")
And longKey = formatDate().slice(0,10) + "longKey"
"""

import argparse
import base64
import hashlib
import json
import os
import random
import time
from typing import List

import httpx

BASE_URL = "https://apigetway.track718.net"
EMAIL = "wujianyong0001@163.com"
PASSWORD = "wzx998998"
BATCH_SIZE = 10
SIGN_KEY = "482737d7ad3f29a2b43e73345830385a"


def _h_func(s: str) -> bytes:
    """Equivalent to JS: function h(e) { return CryptoJS.enc.Utf8.parse(e.padEnd(16, 'ABDEFMN')) }"""
    s = str(s)
    if len(s) >= 16:
        return s[:16].encode("utf-8")
    fill = "ABDEFMN"
    needed = 16 - len(s)
    padding = "".join(fill[i % len(fill)] for i in range(needed))
    return (s + padding).encode("utf-8")


def _track_sign(data_str: str, long_key: str = "") -> str:
    """Compute the track718-api-sign header value.

    Algorithm: AES-256-ECB(JSON_data, key) -> Base64 -> MD5
    """
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    if long_key:
        key_str = long_key + "Track7182024xx"
    else:
        key_str = SIGN_KEY

    key = key_str.encode("utf-8")  # 32 bytes for AES-256

    cipher = AES.new(key, AES.MODE_ECB)
    plaintext = pad(data_str.encode("utf-8"), AES.block_size)
    ciphertext = cipher.encrypt(plaintext)
    ciphertext_b64 = base64.b64encode(ciphertext).decode("utf-8")
    return hashlib.md5(ciphertext_b64.encode("utf-8")).hexdigest()


def md5_password(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def login(email: str = EMAIL, password: str = PASSWORD) -> str:
    pwd_hash = md5_password(password)
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "x-requested-with": "XMLHttpRequest",
        "referer": "https://www.track718.us/",
    }
    body = {"email": email, "password": pwd_hash, "from": "", "accessToken": ""}

    with httpx.Client(timeout=15) as client:
        resp = client.post(f"{BASE_URL}/user/login", json=body, headers=headers)
        data = resp.json()

    if data.get("code") != 200:
        raise RuntimeError(f"Login failed: {data}")

    token = data["token"]
    print(f"Login OK. Token: {token[:40]}...")
    return token


def _build_uuid(token: str) -> str:
    rand = f"0.{random.random() * 10000000000000000:.17f}"
    ts = int(time.time() * 1000)
    return f"{rand}-{ts}{chr(8212)}{chr(8212)}{token}"


def _build_headers(token: str, sign: str) -> dict:
    return {
        "Authorization": token,
        "track718-api-appcode": "",
        "track718-api-pagekey": "",
        "track718-api-sign": sign,
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "x-requested-with": "XMLHttpRequest",
        "referer": "https://www.track718.us/",
        "vsid": "",
    }


def _get_long_key() -> str:
    """longKey = formatDate().slice(0,10) + 'longKey'"""
    date_str = time.strftime("%Y-%m-%d")
    return date_str + "longKey"


def query_tracking(
    tracking_numbers: List[str],
    token: str,
    batch_size: int = BATCH_SIZE,
) -> List[dict]:
    results = []
    total = len(tracking_numbers)
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    long_key = _get_long_key()

    batches = [tracking_numbers[i:i + batch_size] for i in range(0, total, batch_size)]

    with httpx.Client(timeout=30) as client:
        for bi, batch in enumerate(batches):
            tracks = [{"track": tn, "key": "production.shippingapis.com"} for tn in batch]
            nums_str = ",".join(batch)

            body = {
                "tracks": tracks,
                "uuid": _build_uuid(token),
                "noCache": False,
                "referrer": f"https://www.track718.us/zh-CN/detail?nums={nums_str}",
                "isChoose": False,
                "webDateTime": now_str,
            }

            # Compute sign
            body_str = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            sign = _track_sign(body_str, long_key if len(batch) > 10 else "")

            headers = _build_headers(token, sign)

            nums_display = ", ".join(batch[:3])
            suffix = f" +{len(batch)-3} more" if len(batch) > 3 else ""
            print(f"  [{bi+1}/{len(batches)}] {nums_display}{suffix}...", end=" ", flush=True)

            try:
                resp = client.post(
                    f"{BASE_URL}/track/real_query_multi",
                    json=body,
                    headers=headers,
                    timeout=30,
                )
                data = resp.json()

                status_code = data.get("status", {}).get("code", -1)
                if status_code == 1:
                    # Parameter format error - try without long key
                    sign2 = _track_sign(body_str, "")
                    headers2 = _build_headers(token, sign2)
                    resp = client.post(
                        f"{BASE_URL}/track/real_query_multi",
                        json=body,
                        headers=headers2,
                        timeout=30,
                    )
                    data = resp.json()
                    status_code = data.get("status", {}).get("code", -1)

                if status_code == 401:
                    print("TOKEN EXPIRED")
                    for tn in batch:
                        results.append({"tracking_number": tn, "error": "Token expired", "data": None})
                    continue

                resp_items = data.get("data", [])
                if not isinstance(resp_items, list):
                    resp_items = [resp_items] if resp_items else []

                resp_map = {}
                for item in resp_items:
                    tn = item.get("track", "")
                    if tn:
                        resp_map[tn] = item

                ok = 0
                for tn in batch:
                    item = resp_map.get(tn)
                    if item:
                        results.append(_parse_item(tn, item))
                        ok += 1
                    else:
                        results.append({"tracking_number": tn, "error": "No data in response", "data": None})

                print(f"OK ({ok}/{len(batch)})")

            except Exception as e:
                print(f"ERROR: {str(e)[:50]}")
                for tn in batch:
                    results.append({"tracking_number": tn, "error": str(e), "data": None})

            if bi < len(batches) - 1:
                time.sleep(0.3)

    return results


def _parse_item(tracking_number: str, item: dict) -> dict:
    result_code = item.get("result", -1)
    sub_result = item.get("subResult", -1)
    latest = item.get("latest", {}) or {}
    expect = item.get("expect", {}) or {}

    from_events = item.get("from", []) or []
    to_events = item.get("to", []) or []

    events = []
    for ev in from_events:
        events.append({
            "date": ev.get("ondate", "") or ev.get("resultDate", ""),
            "time": "",
            "location": ev.get("address", ""),
            "description": ev.get("status", ""),
        })
    for ev in to_events:
        events.append({
            "date": ev.get("ondate", "") or ev.get("resultDate", ""),
            "time": "",
            "location": ev.get("address", ""),
            "description": ev.get("status", ""),
        })

    status_map = {
        0: "Not Found", 10: "Label Created", 8: "In Transit",
        31: "Out for Delivery", 40: "Delivered",
        3: "Exception", 4: "Expired",
    }
    status = status_map.get(result_code, f"Status {result_code}")
    if result_code == 0 and sub_result == 0:
        status = "Not Found / No Data"

    return {
        "tracking_number": tracking_number,
        "error": None,
        "data": {
            "status": status,
            "result_code": result_code,
            "sub_result": sub_result,
            "latest_description": latest.get("status", ""),
            "latest_date": latest.get("ondate", "") or latest.get("resultDate", ""),
            "latest_location": latest.get("address", ""),
            "estimated_delivery": item.get("estimatedDelivery", "") or expect.get("date", ""),
            "from_code": item.get("fromCode", ""),
            "to_code": item.get("toCode", ""),
            "events": events,
        },
    }


def format_result(r: dict) -> str:
    tn = r["tracking_number"]
    if r.get("error"):
        return f"  {tn}: ERROR - {r['error']}"
    d = r.get("data") or {}
    lines = [f"  {tn}: {d.get('status', 'Unknown')}"]
    if d.get("latest_description"):
        loc = f" | {d['latest_location']}" if d.get("latest_location") else ""
        date = f"({d['latest_date']}) " if d.get("latest_date") else ""
        lines.append(f"    Latest: {date}{d['latest_description']}{loc}")
    if d.get("estimated_delivery"):
        lines.append(f"    Est. Delivery: {d['estimated_delivery']}")
    events = d.get("events") or []
    if events:
        lines.append(f"    Events ({len(events)}):")
        for ev in events[:10]:
            dt = ev.get("date", "")
            desc = ev.get("description", "")
            loc = ev.get("location", "")
            loc_str = f" | {loc}" if loc else ""
            lines.append(f"      {dt}{loc_str}: {desc}")
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
    parser = argparse.ArgumentParser(description="Track718 Direct API Bulk Tracker")
    parser.add_argument("tracking_numbers", nargs="*")
    parser.add_argument("--file", "-f")
    parser.add_argument("--output", "-o")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--token", help="Use existing token instead of logging in")
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

    seen = set()
    unique = [n for n in numbers if n not in seen and not seen.add(n)]
    numbers = unique

    token = args.token
    if not token:
        token = login()

    print(f"Querying {len(numbers)} tracking number(s) via track718 direct API (batch {args.batch_size})...")
    results = query_tracking(numbers, token, batch_size=args.batch_size)

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
