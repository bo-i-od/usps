import io
import os
import sys
import json
import socket
import subprocess
import threading
import time
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, send_file, Response, g

import database as db
import parcel_store
from usps_direct_tracker import (
    get_webdriver, get_webdriver_bit, track_batch,
    BATCH_SIZE, MAX_RETRY, BATCH_TIMEOUT,
    BROWSER_MODE_LOCAL, BROWSER_MODE_BIT,
    cleanup_and_close_bit,
)

BATCH_INTERVAL = 5

app = Flask(__name__)

_track_lock = threading.Lock()
_track_states = {}


def _get_track_state(user_id):
    if user_id not in _track_states:
        _track_states[user_id] = {"running": False, "total": 0, "done": 0, "ok": 0, "errors": 0, "failed_numbers": [], "cancel_requested": False}
    return _track_states[user_id]


# ─── Auth middleware ───

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "未登录", "code": "unauthorized"}), 401
        token = auth[7:]
        session = db.validate_session(token)
        if not session:
            return jsonify({"error": "登录已过期", "code": "session_expired"}), 401
        g.current_user = {
            "id": session["user_id"],
            "username": session["username"],
            "points": session["points"],
            "role": session["role"],
        }
        g.token = token
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if g.current_user["role"] != "admin":
            return jsonify({"error": "需要管理员权限"}), 403
        return f(*args, **kwargs)
    return decorated


# ─── Background tracking ───

def _bg_track_incremental(user_id, numbers, browser_mode=BROWSER_MODE_LOCAL, bit_browser_id=None):
    state = _get_track_state(user_id)
    total = len(numbers)
    with _track_lock:
        state.update(running=True, total=total, done=0, ok=0, errors=0, failed_numbers=[], cancel_requested=False)

    batches = [numbers[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    print(f"  [bg] User {user_id}: Total: {total}, {len(batches)} batch(es), mode={browser_mode}")

    try:
        if browser_mode == BROWSER_MODE_BIT:
            if not bit_browser_id:
                raise ValueError("bit_browser_id is required for bit mode")
            driver = get_webdriver_bit(bit_browser_id)
        else:
            driver = get_webdriver()
    except Exception as e:
        print(f"  [bg] Failed to start browser: {e}")
        with _track_lock:
            state.update(running=False, done=total, errors=total, failed_numbers=list(numbers))
        return

    try:
        retry_queue = []
        done = 0
        ok_count = 0
        err_count = 0

        for bi, batch in enumerate(batches):
            with _track_lock:
                if state["cancel_requested"]:
                    remaining_numbers = []
                    for b in batches[bi:]:
                        remaining_numbers.extend(b)
                    remaining_numbers.extend(retry_queue)
                    fail_results = [{"tracking_number": tn, "error": "Cancelled by user", "data": None} for tn in remaining_numbers]
                    if fail_results:
                        parcel_store.apply_track_results_for_user(user_id, fail_results)
                    state["failed_numbers"] = list(remaining_numbers)
                    state.update(done=total, errors=err_count + len(remaining_numbers))
                    break

            print(f"  [bg] Batch {bi + 1}/{len(batches)} ({len(batch)} numbers)")
            batch_results = track_batch(driver, batch)

            batch_ok = []
            for r in batch_results:
                if r.get("error"):
                    retry_queue.append(r["tracking_number"])
                else:
                    batch_ok.append(r)

            if batch_ok:
                parcel_store.apply_track_results_for_user(user_id, batch_ok)

            done += len(batch)
            ok_count += len(batch_ok)
            err_count += len(batch) - len(batch_ok)
            with _track_lock:
                state.update(done=done, ok=ok_count, errors=err_count)

            if bi < len(batches) - 1:
                time.sleep(BATCH_INTERVAL)

        for attempt in range(1, MAX_RETRY + 1):
            if not retry_queue:
                break
            with _track_lock:
                if state["cancel_requested"]:
                    fail_results = [{"tracking_number": tn, "error": "Cancelled by user", "data": None} for tn in retry_queue]
                    if fail_results:
                        parcel_store.apply_track_results_for_user(user_id, fail_results)
                    state["failed_numbers"] = list(retry_queue)
                    state.update(done=total, errors=total - ok_count)
                    retry_queue = []
                    break
            retry_timeout = int(BATCH_TIMEOUT * (1 + 0.5 * attempt))
            print(f"  [bg] Retry {attempt}/{MAX_RETRY} — {len(retry_queue)} numbers, timeout {retry_timeout}s")
            retry_batches = [retry_queue[i:i + BATCH_SIZE] for i in range(0, len(retry_queue), BATCH_SIZE)]
            next_retry = []
            for bi, batch in enumerate(retry_batches):
                batch_results = track_batch(driver, batch, timeout=retry_timeout)
                batch_ok = []
                for r in batch_results:
                    if r.get("error"):
                        next_retry.append(r["tracking_number"])
                    else:
                        batch_ok.append(r)
                if batch_ok:
                    parcel_store.apply_track_results_for_user(user_id, batch_ok)
                ok_count += len(batch_ok)
                err_count = err_count - len(batch_ok)
                with _track_lock:
                    state.update(ok=ok_count, errors=total - ok_count)
                if bi < len(retry_batches) - 1:
                    time.sleep(BATCH_INTERVAL)
            retry_queue = next_retry

        if not state["cancel_requested"]:
            if retry_queue:
                print(f"  [bg] {len(retry_queue)} still failed")
                fail_results = [{"tracking_number": tn, "error": "Failed after retries", "data": None} for tn in retry_queue]
                parcel_store.apply_track_results_for_user(user_id, fail_results)

            with _track_lock:
                state["failed_numbers"] = list(retry_queue)

    except Exception as e:
        print(f"  [bg] error: {e}")
    finally:
        try:
            if browser_mode == BROWSER_MODE_BIT:
                cleanup_and_close_bit(driver, bit_browser_id)
            else:
                driver.quit()
        except Exception:
            pass
        with _track_lock:
            state["running"] = False


# ─── Pages ───

@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/")
def index():
    return send_file(
        os.path.join(os.path.dirname(__file__), "tracker-ui.html"),
        mimetype="text/html; charset=utf-8",
    )


@app.route("/admin")
def admin_page():
    return send_file(
        os.path.join(os.path.dirname(__file__), "admin-ui.html"),
        mimetype="text/html; charset=utf-8",
    )


# ─── Auth API ───

@app.route("/api/auth/register", methods=["POST"])
def api_register():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    if not username or len(username) < 2:
        return jsonify({"error": "用户名至少2个字符"}), 400
    if not password or len(password) < 6:
        return jsonify({"error": "密码至少6个字符"}), 400
    user = db.create_user(username, password)
    if not user:
        return jsonify({"error": "用户名已存在"}), 409
    device_info = request.headers.get("User-Agent", "")[:200]
    ip = request.remote_addr or ""
    token = db.create_session(user["id"], device_info, ip)
    return jsonify({
        "token": token,
        "user": {"id": user["id"], "username": user["username"], "points": user["points"], "role": user["role"]},
    })


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    user = db.verify_user(username, password)
    if not user:
        return jsonify({"error": "用户名或密码错误"}), 401
    device_info = request.headers.get("User-Agent", "")[:200]
    ip = request.remote_addr or ""
    token = db.create_session(user["id"], device_info, ip)
    return jsonify({
        "token": token,
        "user": {"id": user["id"], "username": user["username"], "points": user["points"], "role": user["role"]},
    })


@app.route("/api/auth/logout", methods=["POST"])
@login_required
def api_logout():
    db.delete_session(g.token)
    return jsonify({"ok": True})


@app.route("/api/auth/me")
@login_required
def api_me():
    user = db.get_user_by_id(g.current_user["id"])
    return jsonify({
        "id": user["id"], "username": user["username"],
        "points": user["points"], "role": user["role"],
    })


@app.route("/api/auth/change_password", methods=["POST"])
@login_required
def api_change_password():
    data = request.get_json(force=True)
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "")
    if not new_pw or len(new_pw) < 6:
        return jsonify({"error": "新密码至少6个字符"}), 400
    ok = db.change_password(g.current_user["id"], old_pw, new_pw)
    if not ok:
        return jsonify({"error": "原密码错误"}), 400
    db.delete_user_sessions(g.current_user["id"], except_token=g.token)
    return jsonify({"ok": True})


@app.route("/api/auth/sessions")
@login_required
def api_sessions():
    sessions = db.get_user_sessions(g.current_user["id"])
    return jsonify({"sessions": sessions})


@app.route("/api/auth/sessions/revoke", methods=["POST"])
@login_required
def api_revoke_session():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    ok = db.revoke_session(g.current_user["id"], session_id)
    return jsonify({"ok": ok})


# ─── Points API ───

@app.route("/api/points/balance")
@login_required
def api_points_balance():
    balance = db.get_user_points(g.current_user["id"])
    return jsonify({"balance": balance})


@app.route("/api/points/transactions")
@login_required
def api_points_transactions():
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)
    result = db.get_transactions(g.current_user["id"], page, page_size)
    return jsonify(result)


@app.route("/api/points/estimate", methods=["POST"])
@login_required
def api_points_estimate():
    data = request.get_json(force=True)
    nums = data.get("tracking_numbers", [])
    nums = list(dict.fromkeys(n.strip() for n in nums if n.strip()))
    result = db.estimate_cost(g.current_user["id"], nums)
    return jsonify(result)


# ─── Recharge API ───

@app.route("/api/recharge/plans")
@login_required
def api_recharge_plans():
    return jsonify({"plans": db.RECHARGE_PLANS})


@app.route("/api/recharge/create", methods=["POST"])
@login_required
def api_recharge_create():
    data = request.get_json(force=True)
    plan_id = data.get("plan_id")
    payment_method = data.get("payment_method", "wechat")
    order = db.create_recharge_order(g.current_user["id"], plan_id, payment_method)
    if not order:
        return jsonify({"error": "无效的充值套餐"}), 400
    return jsonify(order)


@app.route("/api/recharge/status/<order_no>")
@login_required
def api_recharge_status(order_no):
    order = db.get_recharge_order(order_no, g.current_user["id"])
    if not order:
        return jsonify({"error": "订单不存在"}), 404
    return jsonify({"order_no": order["order_no"], "status": order["status"]})


@app.route("/api/recharge/callback", methods=["POST"])
def api_recharge_callback():
    data = request.get_json(force=True)
    order_no = data.get("order_no", "")
    result = db.confirm_recharge(order_no)
    if not result["ok"]:
        return jsonify(result), 400
    return jsonify(result)


# ─── Parcels API (auth-protected, user-scoped) ───

@app.route("/api/parcels")
@login_required
def api_parcels():
    user_id = g.current_user["id"]
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

    result = db.query_parcels(user_id, filters, page, page_size, sort_field, sort_order)
    return jsonify(result)


@app.route("/api/parcels/stats")
@login_required
def api_parcels_stats():
    return jsonify(db.stats_by_status(g.current_user["id"]))


@app.route("/api/parcels/import_merge", methods=["POST"])
@login_required
def api_parcels_import_merge():
    user_id = g.current_user["id"]
    data = request.get_json(force=True)
    nums = data.get("tracking_numbers", [])
    nums = [n.strip() for n in nums if n.strip()]
    if not nums:
        return jsonify({"added": 0})

    deduct = db.deduct_points(user_id, nums)
    if not deduct["ok"]:
        return jsonify({"error": "点数不足", "balance": deduct["balance"], "cost": deduct["cost"]}), 402

    added = db.merge_tracking_numbers(user_id, nums)
    return jsonify({"added": added, "deducted": deduct["deducted"], "balance": deduct["balance"]})


@app.route("/api/parcels/import", methods=["POST"])
@login_required
def api_parcels_import():
    user_id = g.current_user["id"]
    data = request.get_json(force=True)
    nums = data.get("tracking_numbers", [])
    if not nums:
        return jsonify({"error": "No tracking numbers provided"}), 400
    nums = [n.strip() for n in nums if n.strip()]

    deduct = db.deduct_points(user_id, nums)
    if not deduct["ok"]:
        return jsonify({"error": "点数不足", "balance": deduct["balance"], "cost": deduct["cost"]}), 402

    added = db.clear_and_import(user_id, nums)

    bmode = data.get("browser_mode", BROWSER_MODE_LOCAL)
    bit_id = data.get("bit_browser_id") or os.environ.get("BIT_BROWSER_ID")
    t = threading.Thread(target=_bg_track_incremental, args=(user_id, nums, bmode, bit_id), daemon=True)
    t.start()

    return jsonify({"added": added, "total_submitted": len(nums), "tracking_started": True,
                     "deducted": deduct["deducted"], "balance": deduct["balance"]})


@app.route("/api/parcels/track_status")
@login_required
def api_track_status():
    state = _get_track_state(g.current_user["id"])
    with _track_lock:
        return jsonify(dict(state))


@app.route("/api/parcels/track_cancel", methods=["POST"])
@login_required
def api_track_cancel():
    user_id = g.current_user["id"]
    state = _get_track_state(user_id)
    with _track_lock:
        if not state["running"]:
            return jsonify({"ok": False, "error": "No tracking in progress"}), 400
        state["cancel_requested"] = True
    return jsonify({"ok": True})


@app.route("/api/parcels/refresh", methods=["POST"])
@login_required
def api_parcels_refresh():
    user_id = g.current_user["id"]
    state = _get_track_state(user_id)
    with _track_lock:
        if state["running"] and not state["cancel_requested"]:
            return jsonify({"error": "Tracking already in progress"}), 409

    for _ in range(50):
        with _track_lock:
            if not state["running"]:
                break
        time.sleep(0.1)
    else:
        with _track_lock:
            if state["running"]:
                return jsonify({"error": "Previous tracking still stopping"}), 409

    data = request.get_json(force=True)
    nums = data.get("tracking_numbers", [])
    if data.get("all"):
        all_p = db.get_all_parcels(user_id)
        nums = [p["tracking_number"] for p in all_p]
    if not nums:
        return jsonify({"error": "No tracking numbers to refresh"}), 400

    deduct = db.deduct_points(user_id, nums)
    if not deduct["ok"]:
        return jsonify({"error": "点数不足", "balance": deduct["balance"], "cost": deduct["cost"]}), 402

    bmode = data.get("browser_mode", BROWSER_MODE_LOCAL)
    bit_id = data.get("bit_browser_id") or os.environ.get("BIT_BROWSER_ID")
    t = threading.Thread(target=_bg_track_incremental, args=(user_id, nums, bmode, bit_id), daemon=True)
    t.start()
    return jsonify({"tracking_started": True, "total": len(nums),
                     "deducted": deduct["deducted"], "balance": deduct["balance"]})


@app.route("/api/parcels/delete", methods=["POST"])
@login_required
def api_parcels_delete():
    data = request.get_json(force=True)
    nums = data.get("tracking_numbers", [])
    deleted = db.delete_parcels(g.current_user["id"], nums)
    return jsonify({"deleted": deleted})


@app.route("/api/parcels/remark", methods=["POST"])
@login_required
def api_parcels_remark():
    data = request.get_json(force=True)
    tn = data.get("tracking_number", "")
    remark = data.get("remark", "")
    db.update_remark(g.current_user["id"], tn, remark)
    return jsonify({"ok": True})


@app.route("/api/parcels/export", methods=["POST"])
@login_required
def api_parcels_export():
    try:
        import openpyxl
    except ImportError:
        return jsonify({"error": "openpyxl not installed"}), 500

    user_id = g.current_user["id"]
    data = request.get_json(force=True)
    tns = data.get("tracking_numbers", [])
    columns = data.get("columns", [])

    if tns:
        parcels = db.get_parcels_by_tns(user_id, tns)
    else:
        parcels = db.get_all_parcels(user_id)

    if not columns:
        columns = [
            "tracking_number", "carrier", "main_status",
            "destination_country", "shipped_at",
            "last_fetched_at", "delivered_at", "latest_event", "remark",
        ]

    col_labels = {
        "tracking_number": "物流单号", "carrier": "物流商", "main_status": "状态",
        "destination_country": "目的国", "shipped_at": "发货时间",
        "last_fetched_at": "查询时间", "delivered_at": "签收时间",
        "latest_event": "末条轨迹事件", "track_events": "轨迹",
        "transit_days": "运输(天)", "delivered_days": "签收(天)", "remark": "备注",
    }

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "包裹导出"
    ws.append([col_labels.get(c, c) for c in columns])

    for p in parcels:
        row = []
        for c in columns:
            if c == "transit_days":
                v = (p.get("aging") or {}).get("transit")
                row.append(v if v is not None else "-")
            elif c == "delivered_days":
                v = (p.get("aging") or {}).get("delivered")
                row.append(v if v is not None else "-")
            elif c == "track_events":
                parts = []
                if p.get("online_event"):
                    parts.append(p["online_event"])
                if p.get("delivery_event"):
                    parts.append(p["delivery_event"])
                row.append(" | ".join(parts) if parts else "")
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


# ─── Admin API ───

@app.route("/api/admin/stats")
@admin_required
def api_admin_stats():
    return jsonify(db.admin_stats())


@app.route("/api/admin/users")
@admin_required
def api_admin_users():
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)
    search = request.args.get("search", "")
    return jsonify(db.admin_list_users(page, page_size, search))


@app.route("/api/admin/users/<int:uid>")
@admin_required
def api_admin_user_detail(uid):
    user = db.admin_get_user_detail(uid)
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    return jsonify(user)


@app.route("/api/admin/users/<int:uid>/adjust_points", methods=["POST"])
@admin_required
def api_admin_adjust_points(uid):
    data = request.get_json(force=True)
    amount = data.get("amount", 0)
    reason = data.get("reason", "管理员调整")
    result = db.admin_adjust_points(uid, amount, reason)
    if not result["ok"]:
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/admin/users/<int:uid>/reset_password", methods=["POST"])
@admin_required
def api_admin_reset_password(uid):
    data = request.get_json(force=True)
    new_pw = data.get("new_password", "")
    if not new_pw or len(new_pw) < 6:
        return jsonify({"error": "密码至少6个字符"}), 400
    db.admin_reset_password(uid, new_pw)
    db.delete_user_sessions(uid)
    return jsonify({"ok": True})


@app.route("/api/admin/recharge_orders")
@admin_required
def api_admin_recharge_orders():
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)
    status = request.args.get("status", "")
    user_id = request.args.get("user_id", None, type=int)
    return jsonify(db.admin_list_recharge_orders(page, page_size, status, user_id))


@app.route("/api/admin/transactions")
@admin_required
def api_admin_transactions():
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)
    user_id = request.args.get("user_id", None, type=int)
    return jsonify(db.admin_list_transactions(page, page_size, user_id))


@app.route("/api/admin/recharge_orders/<order_no>/confirm", methods=["POST"])
@admin_required
def api_admin_confirm_recharge(order_no):
    result = db.confirm_recharge(order_no)
    if not result["ok"]:
        return jsonify(result), 400
    return jsonify(result)


# ─── Init ───

with app.app_context():
    db.init_db()
    json_path = os.path.join(os.path.dirname(__file__), "data", "parcels.json")
    if os.path.exists(json_path):
        admin = db.get_user_by_id(1)
        if admin:
            count = db.migrate_json_parcels(json_path, admin["id"])
            if count > 0:
                os.rename(json_path, json_path + ".migrated")
                print(f"Migrated {count} parcels from JSON to SQLite (assigned to admin)")


def _stop_other_app_instances():
    """Terminate other python processes running this app.py (Windows)."""
    if os.name != "nt":
        return
    app_py = os.path.abspath(__file__).lower()
    my_pid = os.getpid()
    try:
        out = subprocess.check_output(
            [
                "wmic",
                "process",
                "where",
                "CommandLine like '%app.py%'",
                "get",
                "ProcessId,CommandLine",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    for line in out.splitlines():
        if not line.strip().isdigit():
            continue
        pid = int(line.strip())
        if pid == my_pid:
            continue
        # wmic returns ProcessId and CommandLine on separate lines; verify path.
        try:
            cmd = subprocess.check_output(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        if app_py not in cmd.lower():
            continue
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    time.sleep(0.5)


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


if __name__ == "__main__":
    # Port 5000 is often left in a zombie LISTEN state on Windows after crashes.
    # Default to 5050; override with env PORT=5000 if you have cleaned the port.
    port = int(os.environ.get("PORT", "5050"))
    _stop_other_app_instances()
    if not _port_available(port):
        print(f"ERROR: port {port} is already in use.")
        print("  Close other app.py windows, run start.bat, or set PORT=5051")
        sys.exit(1)

    print(f"Open in browser: http://127.0.0.1:{port}/")
    print(f"Health check:    http://127.0.0.1:{port}/health")
    app.run(
        host="0.0.0.0",
        port=port,
        debug=True,
        use_reloader=False,
        threaded=True,
    )
