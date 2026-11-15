import json
import os
import time

import psycopg
from psycopg.rows import dict_row

from build import clean_scan

ROLLBACK = "ROLLBACK"
PARTIAL = "PARTIAL"
STRATEGIES = {
    ROLLBACK: "整批回滚",
    PARTIAL: "成功先落库、失败进死信",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ods_scan (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    terminal_code TEXT NOT NULL,
    batch_no TEXT,
    raw_payload JSONB NOT NULL,
    landed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE ods_scan ADD COLUMN IF NOT EXISTS batch_no TEXT;

CREATE TABLE IF NOT EXISTS scan_batch (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    batch_no TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,
    valid_count INTEGER NOT NULL DEFAULT 0,
    invalid_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING',
    landed_strategy TEXT,
    landed_by TEXT,
    landed_at TIMESTAMPTZ,
    UNIQUE (biz_date, batch_no)
);
CREATE INDEX IF NOT EXISTS idx_ods_batch ON ods_scan(biz_date, batch_no);

CREATE TABLE IF NOT EXISTS dwd_move (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    terminal_code TEXT NOT NULL,
    container_no TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    event_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS landing_setting (
    biz_date DATE PRIMARY KEY,
    strategy TEXT NOT NULL,
    locked_by TEXT NOT NULL,
    locked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS landing_record (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES scan_batch(id),
    biz_date DATE NOT NULL,
    batch_no TEXT NOT NULL,
    strategy TEXT NOT NULL,
    keeper TEXT NOT NULL,
    outcome TEXT NOT NULL,
    rollback_count INTEGER NOT NULL DEFAULT 0,
    landed_count INTEGER NOT NULL DEFAULT 0,
    dead_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dead_letter (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES scan_batch(id),
    source_id BIGINT NOT NULL UNIQUE REFERENCES ods_scan(id),
    reason TEXT NOT NULL,
    landed_by TEXT NOT NULL,
    landed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS build_runs (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    built_by TEXT NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS build_rejects (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES build_runs(id),
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    reason TEXT NOT NULL
);
"""


class LandingError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                reconcile_batches(conn)
                count = conn.execute("SELECT COUNT(*) AS n FROM ods_scan").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _payload_dict(raw):
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _ensure_batch(conn, biz_date: str, batch_no: str):
    return conn.execute(
        """
        INSERT INTO scan_batch (biz_date, batch_no)
        VALUES (%s, %s)
        ON CONFLICT (biz_date, batch_no) DO NOTHING
        """,
        (biz_date, batch_no),
    )


def reconcile_batches(conn):
    rows = conn.execute(
        "SELECT DISTINCT biz_date, batch_no FROM ods_scan WHERE batch_no IS NOT NULL"
    ).fetchall()
    for row in rows:
        _ensure_batch(conn, row["biz_date"], row["batch_no"])


def _land(conn, biz_date, terminal, payload, batch_no=None):
    batch_no = (batch_no or "").strip() or None
    conn.execute(
        """
        INSERT INTO ods_scan (biz_date, terminal_code, batch_no, raw_payload)
        VALUES (%s, %s, %s, %s::jsonb)
        """,
        (biz_date, terminal, batch_no, json.dumps(payload, ensure_ascii=False)),
    )
    if batch_no:
        _, reason = clean_scan(payload)
        valid = 0 if reason else 1
        invalid = 1 if reason else 0
        conn.execute(
            """
            INSERT INTO scan_batch (biz_date, batch_no, total_count, valid_count, invalid_count)
            VALUES (%s, %s, 1, %s, %s)
            ON CONFLICT (biz_date, batch_no) DO UPDATE SET
                total_count = scan_batch.total_count + 1,
                valid_count = scan_batch.valid_count + %s,
                invalid_count = scan_batch.invalid_count + %s
            """,
            (biz_date, batch_no, valid, invalid, valid, invalid),
        )



def _insert_move(conn, biz_date, row, cleaned):
    conn.execute(
        """
        INSERT INTO dwd_move (biz_date, source_id, terminal_code, container_no, event_kind, event_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            biz_date,
            row["id"],
            row["terminal_code"],
            cleaned["container_no"],
            cleaned["event_kind"],
            cleaned["event_at"],
        ),
    )


def _ensure_dead_letter(conn, batch_id, source_id, reason, keeper):
    conn.execute(
        """
        INSERT INTO dead_letter (batch_id, source_id, reason, landed_by)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (source_id) DO UPDATE SET reason = EXCLUDED.reason
        """,
        (batch_id, source_id, reason, keeper),
    )


def _upsert_batch_counts(conn, biz_date, batch_no, total, valid, invalid):
    return conn.execute(
        """
        INSERT INTO scan_batch (biz_date, batch_no, total_count, valid_count, invalid_count)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (biz_date, batch_no) DO UPDATE SET
            total_count = EXCLUDED.total_count,
            valid_count = EXCLUDED.valid_count,
            invalid_count = EXCLUDED.invalid_count
        RETURNING id
        """,
        (biz_date, batch_no, total, valid, invalid),
    ).fetchone()


def _seed(conn):
    # 2026-11-11：整批合法，已经收成明细
    built = [
        {"scan": "IN", "box": "CSNU1029384", "at": "2026-11-11T07:40:00", "gate": "T1"},
        {"scan": "OUT", "box": "CSNU1029384", "at": "2026-11-11T16:05:00", "gate": "T1"},
    ]
    for payload in built:
        _land(conn, "2026-11-11", payload["gate"], payload, batch_no="B1101")

    # 2026-11-12：还没构建。B1201 有两条坏扫描，是失败批次；另有一条空批次号散扫
    waiting_batch = [
        {"scan": "IN", "box": "TCLU5566778", "at": "2026-11-12T09:15:00", "gate": "T2"},
        {"scan": "XX", "box": "TCLU5566778", "at": "2026-11-12T11:00:00", "gate": "T2"},
        {"scan": "RS", "box": "BAD", "at": "2026-11-12T12:00:00", "gate": "T2"},
    ]
    for payload in waiting_batch:
        _land(conn, "2026-11-12", payload["gate"], payload, batch_no="B1201")
    _land(
        conn,
        "2026-11-12",
        "T2",
        {"scan": "IN", "box": "BAD", "at": "2026-11-12T13:00:00", "gate": "T2"},
    )

    # 2026-11-13：还没构建，落批办法也没收束
    b1301 = [
        {"scan": "IN", "box": "MSKU4100233", "at": "2026-11-13T08:05:00", "gate": "T1"},
        {"scan": "OUT", "box": "MSKU4100233", "at": "2026-11-13T18:20:00", "gate": "T1"},
        {"scan": "RS", "box": "MSKU4100233", "at": "not-a-time", "gate": "T1"},
    ]
    b1302 = [
        {"scan": "IN", "box": "GESU5522110", "at": "2026-11-13T09:00:00", "gate": "T3"},
        {"scan": "OUT", "box": "GESU5522110", "at": "2026-11-13T17:10:00", "gate": "T3"},
    ]
    for payload in b1301:
        _land(conn, "2026-11-13", payload["gate"], payload, batch_no="B1301")
    for payload in b1302:
        _land(conn, "2026-11-13", payload["gate"], payload, batch_no="B1302")
    _land(
        conn,
        "2026-11-13",
        "T3",
        {"scan": "ZZ", "box": "GESU5522110", "at": "2026-11-13T10:00:00", "gate": "T3"},
    )

    # 2026-11-14：办法收束为整批回滚，B1401 已按回滚落批
    b1401 = [
        {"scan": "IN", "box": "CMAU2200145", "at": "2026-11-14T07:50:00", "gate": "T2"},
        {"scan": "XX", "box": "CMAU2200145", "at": "2026-11-14T09:30:00", "gate": "T2"},
    ]
    b1402 = [
        {"scan": "IN", "box": "OOLU6611802", "at": "2026-11-14T10:10:00", "gate": "T1"},
        {"scan": "RS", "box": "OOLU6611802", "at": "2026-11-14T14:40:00", "gate": "T1"},
    ]
    for payload in b1401:
        _land(conn, "2026-11-14", payload["gate"], payload, batch_no="B1401")
    for payload in b1402:
        _land(conn, "2026-11-14", payload["gate"], payload, batch_no="B1402")

    # 2026-11-15：办法收束为部分落库，B1501 留过偏办法与争用痕迹后按部分落库
    b1501 = [
        {"scan": "IN", "box": "TRLU8833021", "at": "2026-11-15T08:15:00", "gate": "T3"},
        {"scan": "OUT", "box": "TRLU8833021", "at": "2026-11-15T16:45:00", "gate": "T3"},
        {"scan": "RS", "box": "BADBOX01", "at": "2026-11-15T12:05:00", "gate": "T3"},
    ]
    b1502 = [
        {"scan": "IN", "box": "HJLU1900477", "at": "2026-11-15T09:25:00", "gate": "T2"},
        {"scan": "OUT", "box": "HJLU1900477", "at": "2026-11-15T15:35:00", "gate": "T2"},
    ]
    for payload in b1501:
        _land(conn, "2026-11-15", payload["gate"], payload, batch_no="B1501")
    for payload in b1502:
        _land(conn, "2026-11-15", payload["gate"], payload, batch_no="B1502")

    rebuild_day(conn, "2026-11-11", "admin")
    rebuild_day(conn, "2026-11-14", "admin")
    rebuild_day(conn, "2026-11-15", "admin")

    lock_setting(conn, "2026-11-14", ROLLBACK, "keeper1")
    b1401_row = conn.execute(
        "SELECT id FROM scan_batch WHERE biz_date='2026-11-14' AND batch_no='B1401'"
    ).fetchone()
    _orchestrate(conn, b1401_row["id"], ROLLBACK, "keeper1", raise_expected=False)

    lock_setting(conn, "2026-11-15", PARTIAL, "keeper1")
    b1501_row = conn.execute(
        "SELECT id FROM scan_batch WHERE biz_date='2026-11-15' AND batch_no='B1501'"
    ).fetchone()
    # 先拿整批回滚来编排：办法偏了，留痕但批次仍待落批
    _orchestrate(conn, b1501_row["id"], ROLLBACK, "keeper2", raise_expected=False)
    _orchestrate(conn, b1501_row["id"], PARTIAL, "keeper1", raise_expected=False)
    # 后到的仓管再来：记落批争用，结果不被改写
    _orchestrate(conn, b1501_row["id"], PARTIAL, "keeper2", raise_expected=False)


def rebuild_day(conn, biz_date: str, username: str):
    conn.execute("DELETE FROM dwd_move WHERE biz_date = %s", (biz_date,))
    rows = conn.execute(
        "SELECT id, terminal_code, batch_no, raw_payload FROM ods_scan WHERE biz_date = %s ORDER BY id",
        (biz_date,),
    ).fetchall()
    run = conn.execute(
        """
        INSERT INTO build_runs (biz_date, accepted_count, rejected_count, built_by)
        VALUES (%s, 0, 0, %s)
        RETURNING id
        """,
        (biz_date, username),
    ).fetchone()
    accepted = 0
    rejected = 0
    loose = []
    batches = {}
    for row in rows:
        cleaned, reason = clean_scan(_payload_dict(row["raw_payload"]))
        if row["batch_no"]:
            batches.setdefault(row["batch_no"], []).append((row, cleaned, reason))
        else:
            loose.append((row, cleaned, reason))

    # 空批次号散扫：沿用老规矩，坏的进剔除清单，永远不进死信队列
    for row, cleaned, reason in loose:
        if reason:
            conn.execute(
                "INSERT INTO build_rejects (run_id, source_id, reason) VALUES (%s, %s, %s)",
                (run["id"], row["id"], reason),
            )
            rejected += 1
        else:
            _insert_move(conn, biz_date, row, cleaned)
            accepted += 1

    for batch_no, items in batches.items():
        valid_items = [item for item in items if not item[2]]
        invalid_items = [item for item in items if item[2]]
        batch = _upsert_batch_counts(
            conn, biz_date, batch_no, len(items), len(valid_items), len(invalid_items)
        )
        if not invalid_items:
            for row, cleaned, _ in items:
                _insert_move(conn, biz_date, row, cleaned)
                accepted += 1
            conn.execute(
                """
                UPDATE scan_batch
                SET status = 'LANDED', landed_strategy = NULL, landed_by = %s, landed_at = NOW()
                WHERE id = %s
                """,
                (username, batch["id"]),
            )
            continue
        prior = conn.execute(
            "SELECT status, landed_strategy, landed_by FROM scan_batch WHERE id = %s",
            (batch["id"],),
        ).fetchone()
        if prior["status"] == "LANDED" and prior["landed_strategy"] is None:
            # 原本整批合法自动收成，后来混进坏扫描：打回待落批，让仓管重新收口，
            # 否则它既不进明细又无法再编排，成了死批次。
            # 待落批失败批次一笔明细都不留，显式清掉，不依赖函数开头的整日删除
            conn.execute(
                """
                DELETE FROM dwd_move
                WHERE source_id IN (
                    SELECT id FROM ods_scan WHERE biz_date = %s AND batch_no = %s
                )
                """,
                (biz_date, batch_no),
            )
            conn.execute(
                """
                UPDATE scan_batch
                SET status = 'PENDING', landed_strategy = NULL, landed_by = NULL, landed_at = NULL
                WHERE id = %s
                """,
                (batch["id"],),
            )
        elif prior["status"] == "LANDED" and prior["landed_strategy"] == PARTIAL:
            # 部分落库的批次重建：合法行回明细、坏行回死信，落信人沿用原仓管
            keeper = prior["landed_by"] or username
            for row, cleaned, reason in items:
                if reason:
                    _ensure_dead_letter(conn, batch["id"], row["id"], reason, keeper)
                else:
                    _insert_move(conn, biz_date, row, cleaned)
                    accepted += 1
        # 待落批或整批回滚的失败批次：一笔明细都不进

    conn.execute(
        "UPDATE build_runs SET accepted_count = %s, rejected_count = %s WHERE id = %s",
        (accepted, rejected, run["id"]),
    )
    return run["id"]


def lock_setting(conn, biz_date: str, strategy: str, username: str):
    if strategy not in STRATEGIES:
        raise LandingError(400, "落批办法只能选整批回滚或成功先落库、失败进死信")
    # 单语句收束：并发下由主键约束兜底，撞键即视为已收束，避免裸 500
    inserted = conn.execute(
        """
        INSERT INTO landing_setting (biz_date, strategy, locked_by)
        VALUES (%s, %s, %s)
        ON CONFLICT (biz_date) DO NOTHING
        RETURNING biz_date
        """,
        (biz_date, strategy, username),
    ).fetchone()
    if not inserted:
        raise LandingError(409, "落批办法已经收束，当日禁止改口")


def orchestrate_landing(conn, batch_id: int, strategy: str, keeper: str):
    return _orchestrate(conn, batch_id, strategy, keeper, raise_expected=True)


def _orchestrate(conn, batch_id, strategy, keeper, raise_expected):
    """落批核心。raise_expected=True 时把偏办法/争用等业务冲突抛给调用方，
    False 时只写审计留痕并返回结果字典，供初始化种子使用。"""
    def conflict(detail):
        if raise_expected:
            raise LandingError(409, detail)
        return {"error": detail}

    if strategy not in STRATEGIES:
        raise LandingError(400, "落批办法只能选整批回滚或成功先落库、失败进死信")
    batch = conn.execute(
        "SELECT * FROM scan_batch WHERE id = %s FOR UPDATE", (batch_id,)
    ).fetchone()
    if not batch:
        raise LandingError(404, "没有这个批次")
    if batch["invalid_count"] == 0:
        raise LandingError(400, "这个批次没有失败扫描，不是失败批次")

    if batch["status"] == "LANDED":
        conn.execute(
            """
            INSERT INTO landing_record
                (batch_id, biz_date, batch_no, strategy, keeper, outcome,
                 rollback_count, landed_count, dead_count)
            VALUES (%s, %s, %s, %s, %s, 'CONTENTION', 0, 0, 0)
            """,
            (batch_id, batch["biz_date"], batch["batch_no"], strategy, keeper),
        )
        return conflict("落批争用：这个失败批次已经落批，后到的请求只记争用，不留第二份落批结果")

    setting = conn.execute(
        "SELECT * FROM landing_setting WHERE biz_date = %s", (batch["biz_date"],)
    ).fetchone()
    if not setting:
        raise LandingError(409, "落批办法尚未收束，当日不准编排失败批次")

    if strategy != setting["strategy"]:
        conn.execute(
            """
            INSERT INTO landing_record
                (batch_id, biz_date, batch_no, strategy, keeper, outcome,
                 rollback_count, landed_count, dead_count)
            VALUES (%s, %s, %s, %s, %s, 'DRIFT', 0, 0, 0)
            """,
            (batch_id, batch["biz_date"], batch["batch_no"], strategy, keeper),
        )
        return conflict("落批办法偏了：编排带上的办法跟落批设置不合，失败批次一笔不入库")

    scans = conn.execute(
        """
        SELECT id, terminal_code, raw_payload
        FROM ods_scan WHERE biz_date = %s AND batch_no = %s ORDER BY id
        """,
        (batch["biz_date"], batch["batch_no"]),
    ).fetchall()

    if strategy == ROLLBACK:
        conn.execute(
            """
            DELETE FROM dwd_move
            WHERE biz_date = %s
              AND source_id IN (
                SELECT id FROM ods_scan WHERE biz_date = %s AND batch_no = %s
              )
            """,
            (batch["biz_date"], batch["biz_date"], batch["batch_no"]),
        )
        conn.execute("DELETE FROM dead_letter WHERE batch_id = %s", (batch_id,))
        outcome, rollback_count, landed_count, dead_count = "LANDED_ROLLBACK", 1, 0, 0
    else:
        landed_count = 0
        dead_count = 0
        for row in scans:
            cleaned, reason = clean_scan(_payload_dict(row["raw_payload"]))
            if reason:
                _ensure_dead_letter(conn, batch_id, row["id"], reason, keeper)
                dead_count += 1
            else:
                conn.execute("DELETE FROM dwd_move WHERE source_id = %s", (row["id"],))
                _insert_move(conn, batch["biz_date"], row, cleaned)
                landed_count += 1
        outcome, rollback_count = "LANDED_PARTIAL", 0

    conn.execute(
        """
        INSERT INTO landing_record
            (batch_id, biz_date, batch_no, strategy, keeper, outcome,
             rollback_count, landed_count, dead_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            batch_id,
            batch["biz_date"],
            batch["batch_no"],
            strategy,
            keeper,
            outcome,
            rollback_count,
            landed_count,
            dead_count,
        ),
    )
    conn.execute(
        """
        UPDATE scan_batch
        SET status = 'LANDED', landed_strategy = %s, landed_by = %s, landed_at = NOW()
        WHERE id = %s
        """,
        (strategy, keeper, batch_id),
    )
    return {
        "outcome": outcome,
        "rollback_count": rollback_count,
        "landed_count": landed_count,
        "dead_count": dead_count,
    }


def landing_overview(conn):
    settings = conn.execute(
        "SELECT biz_date, strategy, locked_by, locked_at FROM landing_setting ORDER BY biz_date"
    ).fetchall()
    batches = conn.execute(
        "SELECT * FROM scan_batch ORDER BY biz_date, batch_no"
    ).fetchall()
    records = conn.execute(
        "SELECT * FROM landing_record ORDER BY biz_date, id"
    ).fetchall()
    dead_rows = conn.execute(
        """
        SELECT dl.id, dl.source_id, dl.reason, dl.landed_by, dl.landed_at,
               sb.biz_date, sb.batch_no
        FROM dead_letter dl
        JOIN scan_batch sb ON sb.id = dl.batch_id
        ORDER BY dl.id
        """
    ).fetchall()
    fail_rows = conn.execute(
        """
        SELECT o.id AS source_id, o.biz_date, o.batch_no, o.terminal_code, o.raw_payload,
               sb.id AS batch_id, sb.status, sb.landed_strategy,
               EXISTS(SELECT 1 FROM dead_letter dl WHERE dl.source_id = o.id) AS in_dlq
        FROM ods_scan o
        LEFT JOIN scan_batch sb
          ON sb.biz_date = o.biz_date AND sb.batch_no = o.batch_no
        ORDER BY o.biz_date, o.id
        """
    ).fetchall()
    setting_map = {row["biz_date"]: row for row in settings}
    failures = []
    for row in fail_rows:
        _, reason = clean_scan(_payload_dict(row["raw_payload"]))
        if not reason:
            continue
        if not row["batch_no"]:
            disposition = "散扫剔除"
        elif row["in_dlq"]:
            disposition = "死信队列"
        elif row["status"] == "LANDED" and row["landed_strategy"] == ROLLBACK:
            disposition = "整批回滚"
        else:
            disposition = "待落批"
        setting = setting_map.get(row["biz_date"])
        failures.append(
            {
                "biz_date": row["biz_date"],
                "batch_no": row["batch_no"],
                "batch_id": row["batch_id"],
                "source_id": row["source_id"],
                "terminal_code": row["terminal_code"],
                "raw_payload": _payload_dict(row["raw_payload"]),
                "reason": reason,
                "disposition": disposition,
                "setting_strategy": setting["strategy"] if setting else None,
                "setting_strategy_name": STRATEGIES[setting["strategy"]] if setting else None,
            }
        )
    return {
        "settings": settings,
        "batches": batches,
        "records": records,
        "dead_letters": dead_rows,
        "failures": failures,
        "totals": {
            "rollback_count": sum(row["rollback_count"] for row in records),
            "landed_count": sum(row["landed_count"] for row in records),
            "dead_count": len(dead_rows),
        },
    }
