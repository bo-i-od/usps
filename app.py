import io
import os
import threading
import time
from datetime import datetime

from flask import Flask, request, jsonify, send_file, Response

import parcel_store
from usps_direct_tracker import get_webdriver, track_batch, BATCH_SIZE, MAX_RETRY

app = Flask(__name__)

_track_lock = threading.Lock()
_track_state = {"running": False, "total": 0, "done": 0, "ok": 0, "errors": 0}


def _bg_track_incremental(numbers):
    """Batch-by-batch tracking with incremental saves after each batch."""
    total = len(numbers)
    with _track_lock:
        _track_state.update(running=True, total=total, done=0, ok=0, errors=0)

    batches = [numbers[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    print(f"  [bg] Total: {total}, {len(batches)} batch(es)")

    try:
        driver = get_webdriver()
    except Exception as e:
        print(f"  [bg] Failed to start browser: {e}")
        with _track_lock:
            _track_state.update(running=False, done=total, errors=total)
        return

    try:
        retry_queue = []
        done = 0
        ok_count = 0
        err_count = 0

        for bi, batch in enumerate(batches):
            print(f"  [bg] Batch {bi + 1}/{len(batches)} ({len(batch)} numbers)")
            batch_results = track_batch(driver, batch)

            batch_ok = []
            for r in batch_results:
                if r.get("error"):
                    retry_queue.append(r["tracking_number"])
                else:
                    batch_ok.append(r)

            if batch_ok:
                parcel_store.apply_track_results(batch_ok)

            done += len(batch)
            ok_count += len(batch_ok)
            err_count += len(batch) - len(batch_ok)
            with _track_lock:
                _track_state.update(done=done, ok=ok_count, errors=err_count)

            if bi < len(batches) - 1:
                time.sleep(1)

        for attempt in range(1, MAX_RETRY + 1):
            if not retry_queue:
                break
            print(f"  [bg] Retry {attempt}/{MAX_RETRY} — {len(retry_queue)} numbers")
            retry_batches = [retry_queue[i:i + BATCH_SIZE] for i in range(0, len(retry_queue), BATCH_SIZE)]
            next_retry = []
            for bi, batch in enumerate(retry_batches):
                batch_results = track_batch(driver, batch)
                batch_ok = []
                for r in batch_results:
                    if r.get("error"):
                        next_retry.append(r["tracking_number"])
                    else:
                        batch_ok.append(r)
                if batch_ok:
                    parcel_store.apply_track_results(batch_ok)
                ok_count += len(batch_ok)
                err_count = err_count - len(batch_ok)
                with _track_lock:
                    _track_state.update(ok=ok_count, errors=total - ok_count)
                if bi < len(retry_batches) - 1:
                    time.sleep(1)
            retry_queue = next_retry

        if retry_queue:
            print(f"  [bg] {len(retry_queue)} still failed")
            fail_results = [{"tracking_number": tn, "error": "Failed after retries", "data": None} for tn in retry_queue]
            parcel_store.apply_track_results(fail_results)

    except Exception as e:
        print(f"  [bg] error: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        with _track_lock:
            _track_state["running"] = False


@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(__file__), "tracker-ui.html"))


# ─── legacy API (keep backward compat) ───
@app.route("/api/track", methods=["POST"])
def api_track():
    data = request.get_json(force=True)
    numbers = data.get("tracking_numbers", [])
    if not numbers:
        return jsonify({"error": "No tracking numbers provided"}), 400
    seen = set()
    unique = [n for n in numbers if n not in seen and not seen.add(n)]
    results = track_bulk(unique)
    return jsonify(results)


# ─── parcels list (with filtering / sorting / pagination) ───
@app.route("/api/parcels")
def api_parcels():
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)
    sort_field = request.args.get("sort_field", "added_at")
    sort_order = request.args.get("sort_order", "desc")

    filters = {}

    ms = request.args.getlist("main_status")
    if ms:
        filters["main_status"] = ms

    ss = request.args.getlist("sub_status")
    if ss:
        filters["sub_status"] = ss

    dest = request.args.getlist("destination")
    if dest:
        filters["destination"] = dest

    df = request.args.get("date_field")
    if df:
        filters["date_field"] = df
    d_from = request.args.get("date_from")
    if d_from:
        filters["date_from"] = d_from
    d_to = request.args.get("date_to")
    if d_to:
        filters["date_to"] = d_to

    for fk in ("shipped_at", "delivered_at"):
        v_from = request.args.get(f"{fk}_from")
        if v_from:
            filters[f"{fk}_from"] = v_from
        v_to = request.args.get(f"{fk}_to")
        if v_to:
            filters[f"{fk}_to"] = v_to

    tns_raw = request.args.get("tracking_numbers", "")
    if tns_raw:
        filters["tracking_numbers"] = [t.strip() for t in tns_raw.split("\n") if t.strip()]

    remark_type = request.args.get("remark_type")
    if remark_type:
        filters["remark_type"] = remark_type
    remark = request.args.get("remark")
    if remark:
        filters["remark"] = remark

    tags = request.args.getlist("tags")
    if tags:
        filters["tags"] = tags

    result = parcel_store.query(
        filters=filters, page=page, page_size=page_size,
        sort_field=sort_field, sort_order=sort_order,
    )
    return jsonify(result)


# ─── sidebar stats ───
@app.route("/api/parcels/stats")
def api_parcels_stats():
    return jsonify(parcel_store.stats_by_status())


# ─── merge import (add new without clearing) ───
@app.route("/api/parcels/import_merge", methods=["POST"])
def api_parcels_import_merge():
    data = request.get_json(force=True)
    nums = data.get("tracking_numbers", [])
    nums = [n.strip() for n in nums if n.strip()]
    if not nums:
        return jsonify({"added": 0})
    added = parcel_store.merge_tracking_numbers(nums)
    return jsonify({"added": added})


# ─── import tracking numbers ───
@app.route("/api/parcels/import", methods=["POST"])
def api_parcels_import():
    data = request.get_json(force=True)
    nums = data.get("tracking_numbers", [])
    if not nums:
        return jsonify({"error": "No tracking numbers provided"}), 400
    nums = [n.strip() for n in nums if n.strip()]

    added = parcel_store.clear_and_import(nums)

    t = threading.Thread(target=_bg_track_incremental, args=(nums,), daemon=True)
    t.start()

    return jsonify({"added": added, "total_submitted": len(nums), "tracking_started": True})


@app.route("/api/parcels/track_status")
def api_track_status():
    with _track_lock:
        return jsonify(dict(_track_state))


# ─── refresh (re-track) ───
@app.route("/api/parcels/refresh", methods=["POST"])
def api_parcels_refresh():
    with _track_lock:
        if _track_state["running"]:
            return jsonify({"error": "Tracking already in progress"}), 409

    data = request.get_json(force=True)
    nums = data.get("tracking_numbers", [])
    if data.get("all"):
        all_p = parcel_store.get_all_parcels()
        nums = [p["tracking_number"] for p in all_p]
    if not nums:
        return jsonify({"error": "No tracking numbers to refresh"}), 400

    t = threading.Thread(target=_bg_track_incremental, args=(nums,), daemon=True)
    t.start()
    return jsonify({"tracking_started": True, "total": len(nums)})


# ─── delete ───
@app.route("/api/parcels/delete", methods=["POST"])
def api_parcels_delete():
    data = request.get_json(force=True)
    nums = data.get("tracking_numbers", [])
    deleted = parcel_store.delete_parcels(nums)
    return jsonify({"deleted": deleted})


# ─── update remark ───
@app.route("/api/parcels/remark", methods=["POST"])
def api_parcels_remark():
    data = request.get_json(force=True)
    tn = data.get("tracking_number", "")
    remark = data.get("remark", "")
    parcel_store.update_remark(tn, remark)
    return jsonify({"ok": True})


# ─── export xlsx ───
@app.route("/api/parcels/export", methods=["POST"])
def api_parcels_export():
    try:
        import openpyxl
    except ImportError:
        return jsonify({"error": "openpyxl not installed. Run: pip install openpyxl"}), 500

    data = request.get_json(force=True)
    tns = data.get("tracking_numbers", [])
    columns = data.get("columns", [])

    if tns:
        parcels = parcel_store.get_parcels_by_tns(tns)
    else:
        parcels = parcel_store.get_all_parcels()

    if not columns:
        columns = [
            "tracking_number", "carrier", "main_status", "sub_status",
            "destination_country", "shipped_at", "added_at",
            "last_fetched_at", "delivered_at", "latest_event",
            "remark",
        ]

    col_labels = {
        "tracking_number": "物流单号",
        "carrier": "物流商",
        "main_status": "状态",
        "sub_status": "子状态",
        "destination_country": "目的国",
        "shipped_at": "发货时间",
        "added_at": "添加时间",
        "last_fetched_at": "最近获取时间",
        "delivered_at": "签收时间",
        "latest_event": "末条轨迹事件",
        "online_event": "上网轨迹",
        "delivery_event": "签收轨迹",
        "transit_days": "运输(天)",
        "delivered_days": "签收(天)",
        "remark": "备注",
        "tags": "标签",
    }

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "包裹导出"

    headers = [col_labels.get(c, c) for c in columns]
    ws.append(headers)

    for p in parcels:
        row = []
        for c in columns:
            if c == "transit_days":
                v = (p.get("aging") or {}).get("transit")
                row.append(v if v is not None else "-")
            elif c == "delivered_days":
                v = (p.get("aging") or {}).get("delivered")
                row.append(v if v is not None else "-")
            elif c == "tags":
                row.append(", ".join(p.get("tags") or []))
            else:
                row.append(p.get(c, ""))
        ws.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=parcels_export_{ts}.xlsx"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
