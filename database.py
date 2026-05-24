"""
database.py — SQLite database layer for user accounts, sessions, points, and parcels.
"""
import os
import json
import sqlite3
import secrets
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from werkzeug.security import generate_password_hash, check_password_hash

DB_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DB_DIR, "usps.db")

_local = threading.local()

RECHARGE_PLANS = [
    {"id": 1, "amount_yuan": 10, "points": 100, "label": "10元 = 100点"},
    {"id": 2, "amount_yuan": 50, "points": 550, "label": "50元 = 550点"},
    {"id": 3, "amount_yuan": 100, "points": 1200, "label": "100元 = 1200点"},
]

REGISTER_GIFT_POINTS = 100
QUERY_COST_PER_NUMBER = 1
SESSION_EXPIRE_DAYS = 30


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        os.makedirs(DB_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def init_db():
    conn = _get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        role TEXT NOT NULL DEFAULT 'user',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token TEXT UNIQUE NOT NULL,
        device_info TEXT DEFAULT '',
        ip_address TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        last_active_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS point_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        amount INTEGER NOT NULL,
        balance_after INTEGER NOT NULL,
        tracking_number TEXT,
        description TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS user_queried_numbers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        tracking_number TEXT NOT NULL,
        first_queried_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id),
        UNIQUE(user_id, tracking_number)
    );

    CREATE TABLE IF NOT EXISTS recharge_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        order_no TEXT UNIQUE NOT NULL,
        amount_yuan REAL NOT NULL,
        points INTEGER NOT NULL,
        payment_method TEXT NOT NULL DEFAULT 'wechat',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        paid_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS parcels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        tracking_number TEXT NOT NULL,
        carrier TEXT DEFAULT '美国邮政',
        carrier_country TEXT DEFAULT '美国',
        destination_country TEXT DEFAULT '美国',
        main_status TEXT DEFAULT '查询不到',
        sub_status TEXT DEFAULT '查询不到',
        remark TEXT DEFAULT '',
        tags TEXT DEFAULT '[]',
        shipped_at TEXT,
        added_at TEXT NOT NULL,
        last_fetched_at TEXT,
        last_success_at TEXT,
        delivered_at TEXT,
        aging TEXT DEFAULT '{}',
        events TEXT DEFAULT '[]',
        latest_event TEXT,
        online_event TEXT,
        delivery_event TEXT,
        raw TEXT DEFAULT '{}',
        FOREIGN KEY (user_id) REFERENCES users(id),
        UNIQUE(user_id, tracking_number)
    );

    CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_parcels_user ON parcels(user_id);
    CREATE INDEX IF NOT EXISTS idx_parcels_user_tn ON parcels(user_id, tracking_number);
    CREATE INDEX IF NOT EXISTS idx_queried_user_tn ON user_queried_numbers(user_id, tracking_number);
    CREATE INDEX IF NOT EXISTS idx_point_tx_user ON point_transactions(user_id);
    CREATE INDEX IF NOT EXISTS idx_recharge_user ON recharge_orders(user_id);
    CREATE INDEX IF NOT EXISTS idx_recharge_order_no ON recharge_orders(order_no);
    """)
    conn.commit()

    try:
        conn.execute("ALTER TABLE parcels ADD COLUMN last_success_at TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    _ensure_admin()


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _ensure_admin():
    conn = _get_conn()
    row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if not row:
        now = _now_iso()
        pw = generate_password_hash("admin123")
        conn.execute(
            "INSERT INTO users (username, password_hash, points, role, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            ("admin", pw, 9999, "admin", now, now),
        )
        conn.commit()


# ─── User CRUD ───

def create_user(username: str, password: str) -> Optional[dict]:
    conn = _get_conn()
    now = _now_iso()
    pw_hash = generate_password_hash(password)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, points, role, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (username, pw_hash, REGISTER_GIFT_POINTS, "user", now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return None
    user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    _record_transaction(user["id"], "register_gift", REGISTER_GIFT_POINTS, REGISTER_GIFT_POINTS, description="注册赠送")
    return dict(user)


def verify_user(username: str, password: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return dict(row)


def get_user_by_id(user_id: int) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def change_password(user_id: int, old_password: str, new_password: str) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], old_password):
        return False
    now = _now_iso()
    conn.execute(
        "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
        (generate_password_hash(new_password), now, user_id),
    )
    conn.commit()
    return True


def admin_reset_password(user_id: int, new_password: str) -> bool:
    conn = _get_conn()
    now = _now_iso()
    conn.execute(
        "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
        (generate_password_hash(new_password), now, user_id),
    )
    conn.commit()
    return True


# ─── Session CRUD ───

def create_session(user_id: int, device_info: str = "", ip_address: str = "") -> str:
    conn = _get_conn()
    token = secrets.token_hex(32)
    now = _now_iso()
    expires = (datetime.utcnow() + timedelta(days=SESSION_EXPIRE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT INTO sessions (user_id, token, device_info, ip_address, created_at, last_active_at, expires_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, token, device_info, ip_address, now, now, expires),
    )
    conn.commit()
    return token


def validate_session(token: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT s.*, u.username, u.points, u.role FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token=?",
        (token,),
    ).fetchone()
    if not row:
        return None
    now = _now_iso()
    if row["expires_at"] < now:
        conn.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
        conn.commit()
        return None
    conn.execute("UPDATE sessions SET last_active_at=? WHERE id=?", (now, row["id"]))
    conn.commit()
    return dict(row)


def delete_session(token: str):
    conn = _get_conn()
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()


def delete_user_sessions(user_id: int, except_token: str = ""):
    conn = _get_conn()
    if except_token:
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token!=?", (user_id, except_token))
    else:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()


def get_user_sessions(user_id: int) -> List[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, device_info, ip_address, created_at, last_active_at FROM sessions WHERE user_id=? ORDER BY last_active_at DESC",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def revoke_session(user_id: int, session_id: int) -> bool:
    conn = _get_conn()
    r = conn.execute("DELETE FROM sessions WHERE id=? AND user_id=?", (session_id, user_id))
    conn.commit()
    return r.rowcount > 0


# ─── Points ───

def _record_transaction(user_id: int, tx_type: str, amount: int, balance_after: int,
                        tracking_number: str = None, description: str = ""):
    conn = _get_conn()
    now = _now_iso()
    conn.execute(
        "INSERT INTO point_transactions (user_id, type, amount, balance_after, tracking_number, description, created_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, tx_type, amount, balance_after, tracking_number, description, now),
    )
    conn.commit()


def get_user_points(user_id: int) -> int:
    conn = _get_conn()
    row = conn.execute("SELECT points FROM users WHERE id=?", (user_id,)).fetchone()
    return row["points"] if row else 0


def get_queried_numbers(user_id: int, tracking_numbers: List[str]) -> set:
    if not tracking_numbers:
        return set()
    conn = _get_conn()
    placeholders = ",".join("?" * len(tracking_numbers))
    rows = conn.execute(
        f"SELECT tracking_number FROM user_queried_numbers WHERE user_id=? AND tracking_number IN ({placeholders})",
        [user_id] + tracking_numbers,
    ).fetchall()
    return {r["tracking_number"] for r in rows}


def estimate_cost(user_id: int, tracking_numbers: List[str]) -> dict:
    already = get_queried_numbers(user_id, tracking_numbers)
    new_numbers = [tn for tn in tracking_numbers if tn not in already]
    cost = len(new_numbers) * QUERY_COST_PER_NUMBER
    balance = get_user_points(user_id)
    return {
        "total": len(tracking_numbers),
        "already_queried": len(already),
        "new_count": len(new_numbers),
        "cost": cost,
        "balance": balance,
        "sufficient": balance >= cost,
    }


def deduct_points(user_id: int, tracking_numbers: List[str]) -> dict:
    """Deduct points for new tracking numbers. Returns {ok, deducted, new_numbers, error}."""
    conn = _get_conn()
    already = get_queried_numbers(user_id, tracking_numbers)
    new_numbers = [tn for tn in tracking_numbers if tn not in already]

    if not new_numbers:
        return {"ok": True, "deducted": 0, "new_numbers": [], "balance": get_user_points(user_id)}

    cost = len(new_numbers) * QUERY_COST_PER_NUMBER
    row = conn.execute("SELECT points FROM users WHERE id=?", (user_id,)).fetchone()
    balance = row["points"]

    if balance < cost:
        return {"ok": False, "error": "insufficient_points", "balance": balance, "cost": cost}

    new_balance = balance - cost
    now = _now_iso()
    conn.execute("UPDATE users SET points=?, updated_at=? WHERE id=?", (new_balance, now, user_id))

    for tn in new_numbers:
        try:
            conn.execute(
                "INSERT INTO user_queried_numbers (user_id, tracking_number, first_queried_at) VALUES (?,?,?)",
                (user_id, tn, now),
            )
        except sqlite3.IntegrityError:
            pass
        _record_transaction(user_id, "query", -QUERY_COST_PER_NUMBER, new_balance, tracking_number=tn, description=f"查询单号 {tn}")

    conn.commit()
    return {"ok": True, "deducted": cost, "new_numbers": new_numbers, "balance": new_balance}


def admin_adjust_points(user_id: int, amount: int, reason: str) -> dict:
    conn = _get_conn()
    now = _now_iso()
    row = conn.execute("SELECT points FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "user_not_found"}
    new_balance = row["points"] + amount
    if new_balance < 0:
        return {"ok": False, "error": "balance_would_be_negative"}
    conn.execute("UPDATE users SET points=?, updated_at=? WHERE id=?", (new_balance, now, user_id))
    _record_transaction(user_id, "admin_adjust", amount, new_balance, description=reason)
    conn.commit()
    return {"ok": True, "balance": new_balance}


def get_transactions(user_id: int, page: int = 1, page_size: int = 20) -> dict:
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM point_transactions WHERE user_id=?", (user_id,)).fetchone()["c"]
    offset = (page - 1) * page_size
    rows = conn.execute(
        "SELECT * FROM point_transactions WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (user_id, page_size, offset),
    ).fetchall()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "data": [dict(r) for r in rows],
    }


# ─── Recharge orders ───

def create_recharge_order(user_id: int, plan_id: int, payment_method: str) -> Optional[dict]:
    plan = next((p for p in RECHARGE_PLANS if p["id"] == plan_id), None)
    if not plan:
        return None
    conn = _get_conn()
    now = _now_iso()
    order_no = f"R{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{secrets.token_hex(4).upper()}"
    conn.execute(
        "INSERT INTO recharge_orders (user_id, order_no, amount_yuan, points, payment_method, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, order_no, plan["amount_yuan"], plan["points"], payment_method, "pending", now),
    )
    conn.commit()
    return {
        "order_no": order_no,
        "amount_yuan": plan["amount_yuan"],
        "points": plan["points"],
        "payment_method": payment_method,
        "status": "pending",
    }


def get_recharge_order(order_no: str, user_id: int = None) -> Optional[dict]:
    conn = _get_conn()
    if user_id:
        row = conn.execute("SELECT * FROM recharge_orders WHERE order_no=? AND user_id=?", (order_no, user_id)).fetchone()
    else:
        row = conn.execute("SELECT * FROM recharge_orders WHERE order_no=?", (order_no,)).fetchone()
    return dict(row) if row else None


def confirm_recharge(order_no: str) -> dict:
    conn = _get_conn()
    order = conn.execute("SELECT * FROM recharge_orders WHERE order_no=?", (order_no,)).fetchone()
    if not order:
        return {"ok": False, "error": "order_not_found"}
    if order["status"] != "pending":
        return {"ok": False, "error": "order_not_pending"}
    now = _now_iso()
    conn.execute("UPDATE recharge_orders SET status='paid', paid_at=? WHERE order_no=?", (now, order_no))
    points = order["points"]
    user_id = order["user_id"]
    row = conn.execute("SELECT points FROM users WHERE id=?", (user_id,)).fetchone()
    new_balance = row["points"] + points
    conn.execute("UPDATE users SET points=?, updated_at=? WHERE id=?", (new_balance, now, user_id))
    _record_transaction(user_id, "recharge", points, new_balance, description=f"充值 {order['amount_yuan']}元 获得 {points}点")
    conn.commit()
    return {"ok": True, "balance": new_balance}


# ─── Admin queries ───

def admin_list_users(page: int = 1, page_size: int = 20, search: str = "") -> dict:
    conn = _get_conn()
    where = ""
    params = []
    if search:
        where = "WHERE username LIKE ?"
        params.append(f"%{search}%")
    total = conn.execute(f"SELECT COUNT(*) c FROM users {where}", params).fetchone()["c"]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT id, username, points, role, created_at, updated_at FROM users {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    users = []
    for r in rows:
        u = dict(r)
        sess = conn.execute("SELECT COUNT(*) c FROM sessions WHERE user_id=?", (u["id"],)).fetchone()
        u["active_sessions"] = sess["c"]
        last = conn.execute("SELECT last_active_at FROM sessions WHERE user_id=? ORDER BY last_active_at DESC LIMIT 1", (u["id"],)).fetchone()
        u["last_active"] = last["last_active_at"] if last else None
        users.append(u)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "data": users,
    }


def admin_get_user_detail(user_id: int) -> Optional[dict]:
    user = get_user_by_id(user_id)
    if not user:
        return None
    conn = _get_conn()
    del user["password_hash"]
    user["active_sessions"] = conn.execute("SELECT COUNT(*) c FROM sessions WHERE user_id=?", (user_id,)).fetchone()["c"]
    user["total_queries"] = conn.execute("SELECT COUNT(*) c FROM user_queried_numbers WHERE user_id=?", (user_id,)).fetchone()["c"]
    user["total_recharges"] = conn.execute("SELECT COUNT(*) c FROM recharge_orders WHERE user_id=? AND status='paid'", (user_id,)).fetchone()["c"]
    return user


def admin_list_recharge_orders(page: int = 1, page_size: int = 20, status: str = "", user_id: int = None) -> dict:
    conn = _get_conn()
    where_parts = []
    params = []
    if status:
        where_parts.append("r.status=?")
        params.append(status)
    if user_id:
        where_parts.append("r.user_id=?")
        params.append(user_id)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    total = conn.execute(f"SELECT COUNT(*) c FROM recharge_orders r {where}", params).fetchone()["c"]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT r.*, u.username FROM recharge_orders r JOIN users u ON r.user_id=u.id {where} ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "data": [dict(r) for r in rows],
    }


def admin_list_transactions(page: int = 1, page_size: int = 20, user_id: int = None) -> dict:
    conn = _get_conn()
    where = ""
    params = []
    if user_id:
        where = "WHERE t.user_id=?"
        params.append(user_id)
    total = conn.execute(f"SELECT COUNT(*) c FROM point_transactions t {where}", params).fetchone()["c"]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT t.*, u.username FROM point_transactions t JOIN users u ON t.user_id=u.id {where} ORDER BY t.created_at DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "data": [dict(r) for r in rows],
    }


def admin_stats() -> dict:
    conn = _get_conn()
    total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    total_points_in_circulation = conn.execute("SELECT COALESCE(SUM(points),0) s FROM users").fetchone()["s"]
    total_recharges = conn.execute("SELECT COUNT(*) c FROM recharge_orders WHERE status='paid'").fetchone()["c"]
    total_recharge_yuan = conn.execute("SELECT COALESCE(SUM(amount_yuan),0) s FROM recharge_orders WHERE status='paid'").fetchone()["s"]
    total_queries = conn.execute("SELECT COUNT(*) c FROM user_queried_numbers").fetchone()["c"]
    today = datetime.utcnow().strftime("%Y-%m-%dT00:00:00")
    active_today = conn.execute("SELECT COUNT(DISTINCT user_id) c FROM sessions WHERE last_active_at>=?", (today,)).fetchone()["c"]
    return {
        "total_users": total_users,
        "active_today": active_today,
        "total_points_in_circulation": total_points_in_circulation,
        "total_recharges": total_recharges,
        "total_recharge_yuan": total_recharge_yuan,
        "total_queries": total_queries,
    }


# ─── Parcels (SQLite-backed) ───

def _parcel_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for field in ("tags", "aging", "events", "raw"):
        if field in d and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                d[field] = [] if field in ("tags", "events") else {}
    for field in ("id", "user_id"):
        d.pop(field, None)
    return d


def _parcel_dict_to_row(p: dict) -> dict:
    d = dict(p)
    for field in ("tags", "aging", "events", "raw"):
        if field in d and not isinstance(d[field], str):
            d[field] = json.dumps(d[field], ensure_ascii=False)
    return d


def get_all_parcels(user_id: int) -> List[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM parcels WHERE user_id=?", (user_id,)).fetchall()
    return [_parcel_row_to_dict(r) for r in rows]


def get_parcels_by_tns(user_id: int, tns: List[str]) -> List[dict]:
    if not tns:
        return []
    conn = _get_conn()
    placeholders = ",".join("?" * len(tns))
    rows = conn.execute(
        f"SELECT * FROM parcels WHERE user_id=? AND tracking_number IN ({placeholders})",
        [user_id] + tns,
    ).fetchall()
    return [_parcel_row_to_dict(r) for r in rows]


def merge_tracking_numbers(user_id: int, nums: List[str]) -> int:
    conn = _get_conn()
    now = _now_iso()
    added = 0
    for tn in nums:
        tn = tn.strip()
        if not tn:
            continue
        try:
            conn.execute(
                """INSERT INTO parcels (user_id, tracking_number, carrier, carrier_country, destination_country,
                   main_status, sub_status, remark, tags, added_at, aging, events, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, tn, "美国邮政", "美国", "美国", "查询不到", "查询不到", "", "[]", now, "{}", "[]", "{}"),
            )
            added += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return added


def clear_and_import(user_id: int, nums: List[str]) -> int:
    conn = _get_conn()
    conn.execute("DELETE FROM parcels WHERE user_id=?", (user_id,))
    now = _now_iso()
    seen = set()
    count = 0
    for tn in nums:
        tn = tn.strip()
        if tn and tn not in seen:
            seen.add(tn)
            conn.execute(
                """INSERT INTO parcels (user_id, tracking_number, carrier, carrier_country, destination_country,
                   main_status, sub_status, remark, tags, added_at, aging, events, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, tn, "美国邮政", "美国", "美国", "查询不到", "查询不到", "", "[]", now, "{}", "[]", "{}"),
            )
            count += 1
    conn.commit()
    return count


def update_parcel(user_id: int, tracking_number: str, updates: dict):
    conn = _get_conn()
    row_updates = _parcel_dict_to_row(updates)
    set_parts = []
    values = []
    for k, v in row_updates.items():
        if k in ("id", "user_id", "tracking_number"):
            continue
        set_parts.append(f"{k}=?")
        values.append(v)
    if not set_parts:
        return
    values.extend([user_id, tracking_number])
    conn.execute(f"UPDATE parcels SET {','.join(set_parts)} WHERE user_id=? AND tracking_number=?", values)
    conn.commit()


def upsert_parcel(user_id: int, parcel: dict):
    conn = _get_conn()
    tn = parcel["tracking_number"]
    row = conn.execute("SELECT id FROM parcels WHERE user_id=? AND tracking_number=?", (user_id, tn)).fetchone()
    d = _parcel_dict_to_row(parcel)
    if row:
        set_parts = []
        values = []
        for k, v in d.items():
            if k in ("tracking_number",):
                continue
            set_parts.append(f"{k}=?")
            values.append(v)
        values.extend([user_id, tn])
        conn.execute(f"UPDATE parcels SET {','.join(set_parts)} WHERE user_id=? AND tracking_number=?", values)
    else:
        now = d.get("added_at") or _now_iso()
        conn.execute(
            """INSERT INTO parcels (user_id, tracking_number, carrier, carrier_country, destination_country,
               main_status, sub_status, remark, tags, shipped_at, added_at, last_fetched_at, last_success_at,
               delivered_at, aging, events, latest_event, online_event, delivery_event, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, tn, d.get("carrier", "美国邮政"), d.get("carrier_country", "美国"),
             d.get("destination_country", "美国"), d.get("main_status", "查询不到"), d.get("sub_status", "查询不到"),
             d.get("remark", ""), d.get("tags", "[]"), d.get("shipped_at"), now,
             d.get("last_fetched_at"), d.get("last_success_at"), d.get("delivered_at"), d.get("aging", "{}"),
             d.get("events", "[]"), d.get("latest_event"), d.get("online_event"),
             d.get("delivery_event"), d.get("raw", "{}")),
        )
    conn.commit()


def delete_parcels(user_id: int, tracking_numbers: List[str]) -> int:
    if not tracking_numbers:
        return 0
    conn = _get_conn()
    placeholders = ",".join("?" * len(tracking_numbers))
    r = conn.execute(
        f"DELETE FROM parcels WHERE user_id=? AND tracking_number IN ({placeholders})",
        [user_id] + tracking_numbers,
    )
    conn.commit()
    return r.rowcount


def update_remark(user_id: int, tracking_number: str, remark: str):
    conn = _get_conn()
    conn.execute("UPDATE parcels SET remark=? WHERE user_id=? AND tracking_number=?", (remark, user_id, tracking_number))
    conn.commit()


def stats_by_status(user_id: int) -> dict:
    conn = _get_conn()
    from parcel_store import MAIN_STATUSES
    counts = {s: 0 for s in MAIN_STATUSES if s != "全部"}
    rows = conn.execute(
        "SELECT main_status, COUNT(*) c FROM parcels WHERE user_id=? GROUP BY main_status",
        (user_id,),
    ).fetchall()
    total = 0
    for r in rows:
        ms = r["main_status"]
        cnt = r["c"]
        if ms in counts:
            counts[ms] = cnt
        total += cnt
    counts["全部"] = total
    return counts


def query_parcels(user_id: int, filters: dict = None, page: int = 1, page_size: int = 20,
                  sort_field: str = "added_at", sort_order: str = "desc") -> dict:
    conn = _get_conn()
    filters = filters or {}
    where_parts = ["user_id=?"]
    params = [user_id]

    ms = filters.get("main_status")
    if ms and "全部" not in ms:
        placeholders = ",".join("?" * len(ms))
        where_parts.append(f"main_status IN ({placeholders})")
        params.extend(ms)

    ss = filters.get("sub_status")
    if ss and "全部" not in ss:
        placeholders = ",".join("?" * len(ss))
        where_parts.append(f"sub_status IN ({placeholders})")
        params.extend(ss)

    dest = filters.get("destination")
    if dest and "全部" not in dest:
        clean = [d.split("-", 1)[-1] if "-" in d else d for d in dest]
        placeholders = ",".join("?" * len(clean))
        where_parts.append(f"destination_country IN ({placeholders})")
        params.extend(clean)

    for fk in ("shipped_at", "delivered_at"):
        f_from = filters.get(f"{fk}_from")
        f_to = filters.get(f"{fk}_to")
        if f_from:
            where_parts.append(f"{fk} >= ?")
            params.append(f_from)
        if f_to:
            end = f_to + "T23:59:59" if len(f_to) == 10 else f_to
            where_parts.append(f"{fk} <= ?")
            params.append(end)

    tns = filters.get("tracking_numbers")
    if tns:
        placeholders = ",".join("?" * len(tns))
        where_parts.append(f"tracking_number IN ({placeholders})")
        params.extend(tns)

    remark_type = filters.get("remark_type")
    remark_kw = filters.get("remark", "")
    if remark_type == "无备注":
        where_parts.append("(remark IS NULL OR remark='')")
    elif remark_kw:
        where_parts.append("remark LIKE ?")
        params.append(f"%{remark_kw}%")

    where = " AND ".join(where_parts)

    allowed_sort = {"added_at", "shipped_at", "delivered_at", "last_fetched_at", "tracking_number", "main_status"}
    if sort_field not in allowed_sort:
        sort_field = "added_at"
    order = "DESC" if sort_order == "desc" else "ASC"

    total = conn.execute(f"SELECT COUNT(*) c FROM parcels WHERE {where}", params).fetchone()["c"]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT * FROM parcels WHERE {where} ORDER BY {sort_field} {order} LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "data": [_parcel_row_to_dict(r) for r in rows],
    }


# ─── Migration ───

def migrate_json_parcels(json_path: str, user_id: int):
    if not os.path.exists(json_path):
        return 0
    with open(json_path, "r", encoding="utf-8") as f:
        parcels = json.load(f)
    count = 0
    for p in parcels:
        p_copy = dict(p)
        upsert_parcel(user_id, p_copy)
        count += 1
    return count
