"""
parcel_store.py — JSON 持久化存储，USPS 结果合并，状态映射（含完整子状态），筛选与 stats。
"""
import json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PARCELS_FILE = os.path.join(DATA_DIR, "parcels.json")

MAIN_STATUSES = [
    "全部", "查询不到", "暂未上线", "运输途中", "已上网", "已交航",
    "到达目的国", "到达待取", "派送途中", "投递失败", "可能异常",
    "运输过久", "签收成功", "包裹退回",
]

SUB_STATUSES = [
    "全部", "查询不到", "无效的单号", "暂未上线", "运输途中", "已上网", "已交航",
    "到达目的国", "清关中", "清关完成", "航班起飞", "航班抵达", "到达待取", "派送途中",
    "签收成功", "投递失败", "找不到收件人", "地址有误", "拒收包裹", "安全原因",
    "可能异常", "包裹丢失", "包裹损坏", "物流单被取消", "运输延迟",
    "可能被拒收", "可能安全原因", "可能地址有误", "可能没人签收",
    "签收超时", "轨迹更新异常", "上网超时", "等待交税", "清关失败",
    "运输过久", "包裹退回", "退回已签收", "退回处理中",
]

# USPS status text → (main_status, sub_status)
_STATUS_RULES: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"delivered", re.I), "签收成功", "签收成功"),
    (re.compile(r"out for delivery", re.I), "派送途中", "派送途中"),
    (re.compile(r"available for pickup", re.I), "到达待取", "到达待取"),
    (re.compile(r"return(ed)? to sender", re.I), "包裹退回", "包裹退回"),
    (re.compile(r"delivery attempt|notice left|unable to deliver", re.I), "投递失败", "投递失败"),
    (re.compile(r"no such number|invalid", re.I), "查询不到", "无效的单号"),
    (re.compile(r"alert|exception|held|seized", re.I), "可能异常", "可能异常"),
    (re.compile(r"in customs|customs", re.I), "运输途中", "清关中"),
    (re.compile(r"arrived|arrival", re.I), "运输途中", "到达目的国"),
    (re.compile(r"departed|accepted|origin", re.I), "运输途中", "已上网"),
    (re.compile(r"in transit|transit", re.I), "运输途中", "运输途中"),
    (re.compile(r"label created|pre.?shipment|shipping label", re.I), "暂未上线", "暂未上线"),
    (re.compile(r"not found|no record|does not exist", re.I), "查询不到", "查询不到"),
]

STATUS_COLORS = {
    "签收成功": "#28a745",
    "运输途中": "#5c67f2",
    "已上网": "#5c67f2",
    "已交航": "#5c67f2",
    "到达目的国": "#5c67f2",
    "派送途中": "#17a2b8",
    "到达待取": "#17a2b8",
    "投递失败": "#dc3545",
    "可能异常": "#dc3545",
    "运输过久": "#dc3545",
    "包裹退回": "#dc3545",
    "暂未上线": "#7f8c8d",
    "查询不到": "#7f8c8d",
}


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _map_status(usps_status: str) -> Tuple[str, str]:
    if not usps_status:
        return "查询不到", "查询不到"
    for pattern, main, sub in _STATUS_RULES:
        if pattern.search(usps_status):
            return main, sub
    return "运输途中", "运输途中"


def _parse_event_time(date_str: str) -> Optional[str]:
    """Try to parse USPS date string into ISO format."""
    if not date_str:
        return None
    for fmt in ("%B %d, %Y, %I:%M %p", "%B %d, %Y %I:%M %p",
                "%B %d, %Y", "%b %d, %Y, %I:%M %p", "%b %d, %Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return None


def _calc_aging(parcel: dict) -> dict:
    """Calculate transit / dwell / delivered days."""
    shipped = parcel.get("shipped_at")
    delivered = parcel.get("delivered_at")
    last_fetched = parcel.get("last_fetched_at")
    events = parcel.get("events") or []

    transit = None
    dwell = None
    delivered_days = None

    if shipped:
        try:
            ship_dt = datetime.fromisoformat(shipped)
            end_dt = datetime.fromisoformat(delivered) if delivered else datetime.utcnow()
            transit = round((end_dt - ship_dt).total_seconds() / 86400, 1)
        except (ValueError, TypeError):
            pass

    if delivered and shipped:
        try:
            delivered_days = round(
                (datetime.fromisoformat(delivered) - datetime.fromisoformat(shipped)).total_seconds() / 86400, 1
            )
        except (ValueError, TypeError):
            pass

    if events and len(events) >= 2:
        max_gap = 0
        sorted_events = []
        for e in events:
            t = e.get("time")
            if t:
                try:
                    sorted_events.append(datetime.fromisoformat(t))
                except (ValueError, TypeError):
                    pass
        sorted_events.sort()
        for i in range(1, len(sorted_events)):
            gap = (sorted_events[i] - sorted_events[i - 1]).total_seconds() / 86400
            if gap > max_gap:
                max_gap = gap
        dwell = round(max_gap, 1) if max_gap > 0 else None

    return {"transit": transit, "dwell": dwell, "delivered": delivered_days}


def _make_default_parcel(tracking_number: str) -> dict:
    now = _now_iso()
    return {
        "tracking_number": tracking_number,
        "carrier": "美国邮政",
        "carrier_country": "美国",
        "destination_country": "美国",
        "main_status": "查询不到",
        "sub_status": "查询不到",
        "remark": "",
        "tags": [],
        "shipped_at": None,
        "added_at": now,
        "last_fetched_at": None,
        "delivered_at": None,
        "aging": {"transit": None, "dwell": None, "delivered": None},
        "events": [],
        "latest_event": None,
        "online_event": None,
        "delivery_event": None,
        "raw": {},
    }


# --------------- persistence ---------------

def _load() -> List[dict]:
    if not os.path.exists(PARCELS_FILE):
        return []
    with open(PARCELS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(parcels: List[dict]):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PARCELS_FILE, "w", encoding="utf-8") as f:
        json.dump(parcels, f, indent=2, ensure_ascii=False)


# --------------- public API ---------------

def merge_tracking_numbers(nums: List[str]) -> int:
    """Import tracking numbers, dedup against existing store. Returns count of newly added."""
    parcels = _load()
    existing = {p["tracking_number"] for p in parcels}
    added = 0
    for tn in nums:
        tn = tn.strip()
        if tn and tn not in existing:
            parcels.append(_make_default_parcel(tn))
            existing.add(tn)
            added += 1
    _save(parcels)
    return added


def clear_and_import(nums: List[str]) -> int:
    """Clear all existing parcels and import fresh list. Returns count added."""
    parcels = []
    seen = set()
    for tn in nums:
        tn = tn.strip()
        if tn and tn not in seen:
            parcels.append(_make_default_parcel(tn))
            seen.add(tn)
    _save(parcels)
    return len(parcels)


def apply_track_results(results: List[dict]):
    """Merge track_bulk() output into the store."""
    parcels = _load()
    by_tn = {p["tracking_number"]: p for p in parcels}

    for r in results:
        tn = r.get("tracking_number")
        if not tn:
            continue
        if tn not in by_tn:
            by_tn[tn] = _make_default_parcel(tn)
            parcels.append(by_tn[tn])

        p = by_tn[tn]
        p["last_fetched_at"] = _now_iso()
        p["raw"] = r

        if r.get("error"):
            if not p.get("events"):
                p["main_status"] = "查询不到"
                p["sub_status"] = "查询不到"
            continue

        data = r.get("data") or {}
        usps_status = data.get("status", "")
        main, sub = _map_status(usps_status)
        p["main_status"] = main
        p["sub_status"] = sub

        raw_events = data.get("events") or []
        parsed_events = []
        for ev in raw_events:
            t = _parse_event_time(ev.get("date", ""))
            parsed_events.append({
                "time": t,
                "description": ev.get("description", ""),
                "location": ev.get("location", ""),
            })

        if parsed_events:
            p["events"] = parsed_events
            p["latest_event"] = parsed_events[0].get("description")
            if parsed_events[0].get("time") and not p.get("shipped_at"):
                p["shipped_at"] = parsed_events[-1].get("time")

            for ev in parsed_events:
                desc = ev.get("description", "").lower()
                if "accepted" in desc or "departed" in desc or "label created" in desc:
                    p["online_event"] = ev.get("description")
                    break

            if main == "签收成功":
                p["delivered_at"] = parsed_events[0].get("time")
                p["delivery_event"] = parsed_events[0].get("description")

        # check if too long without updates
        if main == "运输途中" and p.get("shipped_at"):
            try:
                ship_dt = datetime.fromisoformat(p["shipped_at"])
                if (datetime.utcnow() - ship_dt).days > 30:
                    p["main_status"] = "运输过久"
                    p["sub_status"] = "运输过久"
            except (ValueError, TypeError):
                pass

        p["aging"] = _calc_aging(p)

    _save(parcels)


def delete_parcels(tracking_numbers: List[str]) -> int:
    parcels = _load()
    to_del = set(tracking_numbers)
    before = len(parcels)
    parcels = [p for p in parcels if p["tracking_number"] not in to_del]
    _save(parcels)
    return before - len(parcels)


def update_remark(tracking_number: str, remark: str):
    parcels = _load()
    for p in parcels:
        if p["tracking_number"] == tracking_number:
            p["remark"] = remark
            break
    _save(parcels)


def stats_by_status() -> Dict[str, int]:
    """Return count for each of the 14 main statuses + total."""
    parcels = _load()
    counts = {s: 0 for s in MAIN_STATUSES if s != "全部"}
    for p in parcels:
        ms = p.get("main_status", "查询不到")
        if ms in counts:
            counts[ms] += 1
    counts["全部"] = len(parcels)
    return counts


def query(
    filters: Optional[Dict[str, Any]] = None,
    page: int = 1,
    page_size: int = 20,
    sort_field: str = "added_at",
    sort_order: str = "desc",
) -> Dict[str, Any]:
    """Server-side filtering + sorting + pagination."""
    parcels = _load()
    filters = filters or {}

    # --- filter ---
    main_statuses = filters.get("main_status")
    if main_statuses and "全部" not in main_statuses:
        parcels = [p for p in parcels if p.get("main_status") in main_statuses]

    sub_statuses = filters.get("sub_status")
    if sub_statuses and "全部" not in sub_statuses:
        parcels = [p for p in parcels if p.get("sub_status") in sub_statuses]

    destinations = filters.get("destination")
    if destinations and "全部" not in destinations:
        clean = [d.split("-", 1)[-1] if "-" in d else d for d in destinations]
        parcels = [p for p in parcels if p.get("destination_country") in clean]

    date_field = filters.get("date_field", "added_at")
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    if date_from:
        parcels = [p for p in parcels if (p.get(date_field) or "") >= date_from]
    if date_to:
        end = date_to + "T23:59:59" if len(date_to) == 10 else date_to
        parcels = [p for p in parcels if (p.get(date_field) or "") <= end]

    for field_key in ("shipped_at", "delivered_at"):
        f_from = filters.get(f"{field_key}_from")
        f_to = filters.get(f"{field_key}_to")
        if f_from:
            parcels = [p for p in parcels if (p.get(field_key) or "") >= f_from]
        if f_to:
            f_end = f_to + "T23:59:59" if len(f_to) == 10 else f_to
            parcels = [p for p in parcels if (p.get(field_key) or "") <= f_end]

    tns = filters.get("tracking_numbers")
    if tns:
        tn_set = set(tns)
        parcels = [p for p in parcels if p.get("tracking_number") in tn_set]

    remark_type = filters.get("remark_type")
    remark_kw = filters.get("remark", "")
    if remark_type == "无备注":
        parcels = [p for p in parcels if not p.get("remark")]
    elif remark_kw:
        parcels = [p for p in parcels if remark_kw in (p.get("remark") or "")]

    tags = filters.get("tags")
    if tags:
        tags_set = set(tags)
        parcels = [p for p in parcels if tags_set & set(p.get("tags") or [])]

    total = len(parcels)

    # --- sort ---
    reverse = sort_order == "desc"
    parcels.sort(key=lambda p: p.get(sort_field) or "", reverse=reverse)

    # --- paginate ---
    start = (page - 1) * page_size
    end = start + page_size
    page_data = parcels[start:end]

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "data": page_data,
    }


def get_all_parcels() -> List[dict]:
    return _load()


def get_parcels_by_tns(tns: List[str]) -> List[dict]:
    parcels = _load()
    tn_set = set(tns)
    return [p for p in parcels if p["tracking_number"] in tn_set]
