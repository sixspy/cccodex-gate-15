import os
from datetime import date, datetime

from flask import Flask, jsonify, request

from auth import authenticate, issue_token, read_user
from db import (
    LandingError,
    STRATEGIES,
    connect,
    init_db,
    landing_overview,
    lock_setting,
    orchestrate_landing,
    rebuild_day,
)

app = Flask(__name__)
app.json.ensure_ascii = False


def _stamp(value):
    if value is None:
        return None
    return value.isoformat(sep=" ", timespec="minutes")


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _overview_response(conn):
    data = landing_overview(conn)
    return jsonify(
        {
            "settings": [
                {**_jsonable(row), "strategy_name": STRATEGIES[row["strategy"]]}
                for row in data["settings"]
            ],
            "batches": [
                {
                    **_jsonable(row),
                    "landed_strategy_name": STRATEGIES[row["landed_strategy"]]
                    if row["landed_strategy"]
                    else None,
                }
                for row in data["batches"]
            ],
            "records": [
                {**_jsonable(row), "strategy_name": STRATEGIES[row["strategy"]]}
                for row in data["records"]
            ],
            "dead_letters": [_jsonable(row) for row in data["dead_letters"]],
            "failures": [_jsonable(row) for row in data["failures"]],
            "totals": data["totals"],
            "strategies": [
                {"code": code, "name": name} for code, name in STRATEGIES.items()
            ],
        }
    )


def _user():
    return read_user(request.headers.get("Authorization"))


def _require_keeper():
    user = _user()
    if not user:
        return None, (jsonify({"detail": "未登录"}), 401)
    if user["role"] != "keeper":
        return None, (jsonify({"detail": "只有仓管能收束落批办法、编排失败批次"}), 403)
    return user, None


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return jsonify({"status": "ok", "service": "gate-event-lake"})


@app.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    user = authenticate(str(body.get("username", "")).strip(), str(body.get("password", "")))
    if not user:
        return jsonify({"detail": "用户名或密码错误"}), 401
    return jsonify(
        {
            "access_token": issue_token(user),
            "username": user["username"],
            "role": user["role"],
            "title": user["title"],
        }
    )


@app.get("/api/dates")
def list_dates():
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT d.biz_date,
                   (SELECT COUNT(*) FROM ods_scan o WHERE o.biz_date = d.biz_date) AS ods_count,
                   (SELECT COUNT(*) FROM dwd_move m WHERE m.biz_date = d.biz_date) AS dwd_count,
                   EXISTS(SELECT 1 FROM build_runs r WHERE r.biz_date = d.biz_date) AS built,
                   (SELECT COUNT(*) FROM scan_batch sb
                     WHERE sb.biz_date = d.biz_date
                       AND sb.invalid_count > 0 AND sb.status = 'PENDING') AS pending_batches
            FROM (SELECT DISTINCT biz_date FROM ods_scan) d
            ORDER BY d.biz_date
            """
        ).fetchall()
    for row in rows:
        row["biz_date"] = row["biz_date"].isoformat()
    return jsonify(rows)


@app.get("/api/dates/<biz_date>")
def date_detail(biz_date):
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        ods = conn.execute(
            """
            SELECT id, terminal_code, batch_no, raw_payload
            FROM ods_scan WHERE biz_date = %s ORDER BY id
            """,
            (biz_date,),
        ).fetchall()
        moves = conn.execute(
            """
            SELECT source_id, terminal_code, container_no, event_kind, event_at
            FROM dwd_move WHERE biz_date = %s ORDER BY event_at, id
            """,
            (biz_date,),
        ).fetchall()
        run = conn.execute(
            """
            SELECT id, accepted_count, rejected_count, built_by, built_at
            FROM build_runs WHERE biz_date = %s ORDER BY id DESC LIMIT 1
            """,
            (biz_date,),
        ).fetchone()
        rejects = []
        if run:
            rejects = conn.execute(
                """
                SELECT source_id, reason FROM build_rejects
                WHERE run_id = %s ORDER BY source_id
                """,
                (run["id"],),
            ).fetchall()
    if not ods:
        return jsonify({"detail": "这一天没有贴源扫描"}), 404
    for move in moves:
        move["event_at"] = _stamp(move["event_at"])
    if run:
        run["built_at"] = _stamp(run["built_at"])
    return jsonify({"biz_date": biz_date, "ods": ods, "moves": moves, "run": run, "rejects": rejects})


@app.post("/api/dates/<biz_date>/build")
def build_date(biz_date):
    user = _user()
    if not user:
        return jsonify({"detail": "未登录"}), 401
    if user["role"] != "writer":
        return jsonify({"detail": "核对员不能构建明细层"}), 403
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ods_scan WHERE biz_date = %s LIMIT 1",
            (biz_date,),
        ).fetchone()
        if not exists:
            return jsonify({"detail": "这一天没有贴源扫描"}), 404
        rebuild_day(conn, biz_date, user["username"])
        conn.commit()
    return date_detail(biz_date)


@app.get("/api/landing")
def landing_get():
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        # 同轮核齐：落批设置、落批列表、失败详情、死信队列取同一个事务快照
        conn.isolation_level = "REPEATABLE READ"
        return _overview_response(conn)


@app.post("/api/landing/settings")
def landing_lock():
    user, error = _require_keeper()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    biz_date = str(body.get("biz_date", "")).strip()
    strategy = str(body.get("strategy", "")).strip()
    if not biz_date:
        return jsonify({"detail": "要选一个业务日才能收束落批办法"}), 400
    with connect() as conn:
        try:
            lock_setting(conn, biz_date, strategy, user["username"])
            conn.commit()
        except LandingError as exc:
            conn.rollback()
            return jsonify({"detail": exc.detail}), exc.status
        return _overview_response(conn)


@app.post("/api/landing/orchestrate")
def landing_orchestrate():
    user, error = _require_keeper()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    batch_id = body.get("batch_id")
    strategy = str(body.get("strategy", "")).strip()
    if not isinstance(batch_id, int):
        return jsonify({"detail": "要指定一个失败批次"}), 400
    with connect() as conn:
        try:
            orchestrate_landing(conn, batch_id, strategy, user["username"])
            conn.commit()
        except LandingError as exc:
            # 偏办法、争用的审计留痕与业务拒绝在同一事务：留痕要保住，批次数据不动
            conn.commit()
            return jsonify({"detail": exc.detail}), exc.status
        return _overview_response(conn)


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
