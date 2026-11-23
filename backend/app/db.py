from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from app.config import DB_PATH

CALL_EVENTS_DDL = '''
CREATE TABLE IF NOT EXISTS call_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  window_id INTEGER,
  action TEXT NOT NULL,
  from_status TEXT NOT NULL DEFAULT '',
  to_status TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(ticket_id) REFERENCES tickets(id),
  FOREIGN KEY(window_id) REFERENCES windows(id)
);
'''

SCHEMA = '''
CREATE TABLE IF NOT EXISTS business_kinds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL UNIQUE,
  seq INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  kind_ids TEXT NOT NULL,
  open INTEGER NOT NULL DEFAULT 1,
  current_ticket_id INTEGER
);
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  kind_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  window_id INTEGER,
  queue_pos INTEGER NOT NULL,
  taken_at TEXT NOT NULL,
  called_at TEXT,
  finished_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(kind_id) REFERENCES business_kinds(id),
  FOREIGN KEY(window_id) REFERENCES windows(id)
);
''' + CALL_EVENTS_DDL + '''
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tickets_day_status ON tickets(business_day, status);
CREATE INDEX IF NOT EXISTS idx_call_events_ticket ON call_events(ticket_id, id);
'''

# 一个窗口同一时刻至多一个「叫号中/办理中」号牌；双终态在库层面即不可能落盘。
ACTIVE_WINDOW_INDEX = '''
CREATE UNIQUE INDEX IF NOT EXISTS uq_tickets_active_window
ON tickets(window_id)
WHERE window_id IS NOT NULL AND status IN ('叫号中', '办理中');
'''


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 并发双点：后到的事务等前一个写完，而不是立刻 database is locked。
    conn.execute("PRAGMA busy_timeout=10000")
    conn.isolation_level = None  # 事务由 db() 显式管理
    return conn


@contextmanager
def db(write: bool = False):
    """一个请求 = 一个事务。

    write=True 用 BEGIN IMMEDIATE 先取写锁，让「读状态 → 判可做 → 改号牌 →
    改窗口当前号指针 → 写叫号事件」整段串行落账；并发的作废/重叫只有一个先后，
    不会交叉出「已作废又办理」或双终态。任一步抛错整笔 ROLLBACK，不留半截。
    读请求用 WAL 快照事务，令一次响应里的号牌、窗口指针、事件彼此同刷。
    """
    conn = connect()
    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        if conn.in_transaction:
            conn.execute("COMMIT")
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库升级：call_events 需可挂「无窗口」的作废事件，并补前态/后态两列。"""
    cols = {c["name"]: c for c in conn.execute("PRAGMA table_info(call_events)").fetchall()}
    if not cols:
        return
    needs_rebuild = bool(cols.get("window_id") and cols["window_id"]["notnull"]) or (
        "from_status" not in cols
    )
    if not needs_rebuild:
        return
    legacy_cols = [c["name"] for c in conn.execute("PRAGMA table_info(call_events)")]
    shared = [c for c in ("id", "ticket_id", "window_id", "action", "created_at", "note") if c in legacy_cols]
    # 旧数据按旧约束写入，重建期间不校验外键（外键 pragma 只能在事务外切换）。
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE call_events RENAME TO call_events_legacy")
        conn.execute(CALL_EVENTS_DDL)
        conn.execute(
            f"""INSERT INTO call_events({", ".join(shared)})
                SELECT {", ".join(shared)} FROM call_events_legacy"""
        )
        conn.execute("DROP TABLE call_events_legacy")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    conn.execute("PRAGMA foreign_keys=ON")


def _repair_double_active(conn: sqlite3.Connection) -> None:
    """旧库可能已存在「同一窗口两张叫号中/办理中」的分裂结局。

    终态只能有一个：保留窗口指针所指（否则取最近叫号）的一张继续办理，
    其余转为过号并在事件里留痕，使其仍可从回收页重叫或作废，不凭空消失。
    """
    groups = conn.execute(
        """SELECT window_id FROM tickets
           WHERE window_id IS NOT NULL AND status IN ('叫号中','办理中')
           GROUP BY window_id HAVING COUNT(*) > 1"""
    ).fetchall()
    for g in groups:
        wid = g["window_id"]
        rows = conn.execute(
            """SELECT * FROM tickets
               WHERE window_id=? AND status IN ('叫号中','办理中')
               ORDER BY called_at DESC, id DESC""",
            (wid,),
        ).fetchall()
        ptr = conn.execute(
            "SELECT current_ticket_id FROM windows WHERE id=?", (wid,)
        ).fetchone()
        ptr_id = ptr["current_ticket_id"] if ptr else None
        keeper = next((r for r in rows if r["id"] == ptr_id), rows[0])
        conn.execute(
            "UPDATE windows SET current_ticket_id=? WHERE id=?", (keeper["id"], wid)
        )
        for r in rows:
            if r["id"] == keeper["id"]:
                continue
            conn.execute(
                """UPDATE tickets SET status='过号', finished_at=NULL, note=?
                   WHERE id=?""",
                ("迁移修复：同窗双在办，保留一张继续办理", r["id"]),
            )
            conn.execute(
                """INSERT INTO call_events(ticket_id, window_id, action, from_status, to_status, created_at, note)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    r["id"],
                    wid,
                    "过号",
                    r["status"],
                    "过号",
                    datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "迁移修复：同窗双在办",
                ),
            )


def _repair_stale_pointer(conn: sqlite3.Connection) -> None:
    """窗口指针若指向已过号/已作废/已办结的号牌，属历史残留：清掉，免其虚占窗。"""
    rows = conn.execute(
        """SELECT w.id AS wid, t.id AS tid, t.status
           FROM windows w JOIN tickets t ON t.id = w.current_ticket_id
           WHERE t.status NOT IN ('叫号中','办理中')"""
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=?", (r["wid"],)
        )


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.executescript(SCHEMA)  # 重建迁移中随旧表丢弃的索引
        conn.execute("BEGIN IMMEDIATE")
        try:
            _repair_double_active(conn)
            _repair_stale_pointer(conn)
            conn.execute(ACTIVE_WINDOW_INDEX)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
