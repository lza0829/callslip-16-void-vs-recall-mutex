from __future__ import annotations
from fastapi import HTTPException
from app.services.core import (
    business_day,
    kind_row,
    log_event,
    now,
    parse_kind_ids,
    require_open_day,
    ticket_dict,
    ticket_row,
    window_row,
)

# 架构口径：作废为准（全局唯一）
# - 号牌一旦进入「已作废」即终态：重叫整笔硬拒，不写重叫事件、不占窗、不动指针。
# - 重叫命中后落「办理中」并占窗；此时作废按状态规则硬拒（已重叫不得再被作废）。
# - 作废与重叫都在同一个 BEGIN IMMEDIATE 事务里「读状态 → 判定 → 改号牌 →
#   改窗口指针 → 写事件」，并发双点只会有一个先后，不会出现已作废又办理。
# 作废与重叫只作用回收页（等候/过号）；办理中/叫号中/已办结不在作废射程内。
VOIDABLE = ("等候", "过号")
RECALLABLE = ("过号",)
ACTIVE = ("叫号中", "办理中")


def list_tickets(conn, status: str | None = None, day: str | None = None) -> list[dict]:
    d = day or business_day(conn)
    if status:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? AND status=? ORDER BY queue_pos, id",
            (d, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? ORDER BY id DESC",
            (d,),
        ).fetchall()
    return [ticket_dict(conn, r) for r in rows]


def waiting_wall(conn) -> dict:
    day = business_day(conn)
    kinds = conn.execute(
        "SELECT id, name, prefix FROM business_kinds WHERE active=1 ORDER BY id"
    ).fetchall()
    wall = []
    for k in kinds:
        rows = conn.execute(
            """SELECT * FROM tickets
               WHERE business_day=? AND kind_id=? AND status IN ('等候','过号')
               ORDER BY CASE status WHEN '等候' THEN 0 ELSE 1 END, queue_pos, id""",
            (day, k["id"]),
        ).fetchall()
        wall.append(
            {
                "业务种类": k["name"],
                "前缀": k["prefix"],
                "等候数": sum(1 for r in rows if r["status"] == "等候"),
                "过号数": sum(1 for r in rows if r["status"] == "过号"),
                "队列": [ticket_dict(conn, r) for r in rows],
            }
        )
    current = conn.execute(
        """SELECT t.* FROM tickets t
           JOIN windows w ON w.current_ticket_id = t.id
           WHERE t.business_day=?""",
        (day,),
    ).fetchall()
    return {
        "营业日": day,
        "墙面": wall,
        "在办": [ticket_dict(conn, r) for r in current],
    }


def _receipt(conn, action: str, ticket_id: int, event: dict | None) -> dict:
    """动作回执：号牌终态 + 窗口当前号指针 + 本次事件，均取自本事务内写入后的库。

    窗口指针取「该号牌所在窗口」的当前号；占窗 = 指针正指向本号牌。
    """
    d = ticket_dict(conn, ticket_row(conn, ticket_id))
    ptr = None
    if d["窗口编号"]:
        row = conn.execute(
            "SELECT current_ticket_id FROM windows WHERE id=?", (d["窗口编号"],)
        ).fetchone()
        ptr = row["current_ticket_id"] if row else None
    d["回执"] = action
    d["事件"] = event
    d["窗口指针"] = ptr
    d["占窗"] = ptr == ticket_id
    return d


def _clear_window_pointer(conn, ticket_id: int) -> None:
    conn.execute(
        "UPDATE windows SET current_ticket_id=NULL WHERE current_ticket_id=?",
        (ticket_id,),
    )


def _window_busy_ticket(conn, window_id: int) -> int | None:
    """窗口是否占窗：以号码在办状态为准，指针只作补充。

    指针指向已过号/已作废的号牌属历史残留，不算占窗；而指针若指向真正的
    叫号中/办理中号牌，或库里有该窗口的在办号牌，则一律算占窗。
    """
    active = conn.execute(
        """SELECT id FROM tickets
           WHERE window_id=? AND status IN ('叫号中','办理中')
           ORDER BY id DESC LIMIT 1""",
        (window_id,),
    ).fetchone()
    if active:
        return active["id"]
    row = conn.execute(
        "SELECT current_ticket_id FROM windows WHERE id=?", (window_id,)
    ).fetchone()
    if row and row["current_ticket_id"]:
        stale = conn.execute(
            "SELECT status FROM tickets WHERE id=?", (row["current_ticket_id"],)
        ).fetchone()
        if stale and stale["status"] in ACTIVE:
            return row["current_ticket_id"]
    return None


def take_ticket(conn, kind_id: int) -> dict:
    day = require_open_day(conn)
    kind = kind_row(conn, kind_id)
    if not kind["active"]:
        raise HTTPException(400, detail="该业务种类已停用")
    open_wins = conn.execute("SELECT COUNT(*) AS c FROM windows WHERE open=1").fetchone()["c"]
    if open_wins == 0:
        raise HTTPException(400, detail="无开放窗口，禁止取号")
    seq = kind["seq"] + 1
    slip = f"{kind['prefix']}{seq:03d}"
    pos_row = conn.execute(
        "SELECT COALESCE(MAX(queue_pos),0) AS m FROM tickets WHERE business_day=? AND kind_id=? AND status='等候'",
        (day, kind_id),
    ).fetchone()
    pos = pos_row["m"] + 1
    cur = conn.execute(
        """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, note)
           VALUES (?,?,?,'等候',NULL,?,?,?)""",
        (slip, kind_id, day, pos, now(), ""),
    )
    conn.execute("UPDATE business_kinds SET seq=? WHERE id=?", (seq, kind_id))
    return ticket_dict(conn, ticket_row(conn, cur.lastrowid))


def call_next(conn, window_id: int, kind_id: int | None = None) -> dict:
    day = require_open_day(conn)
    win = window_row(conn, window_id)
    if not win["open"]:
        raise HTTPException(400, detail="窗口已关闭")
    busy = _window_busy_ticket(conn, window_id)
    if busy:
        raise HTTPException(400, detail="窗口仍有在办号，请先办结或过号")
    allowed = parse_kind_ids(win["kind_ids"])
    if kind_id is not None:
        if kind_id not in allowed:
            raise HTTPException(400, detail="本窗口不可办该业务种类")
        allowed = [kind_id]
    # 优先等候，同种类按位次；跨种类按取号时刻
    placeholders = ",".join("?" * len(allowed)) if allowed else "NULL"
    params: list = [day, *allowed]
    row = conn.execute(
        f"""SELECT * FROM tickets
            WHERE business_day=? AND status='等候' AND kind_id IN ({placeholders})
            ORDER BY queue_pos, taken_at, id LIMIT 1""",
        params,
    ).fetchone()
    if not row:
        # 过号不自动叫：重叫走回收页的显式动作
        raise HTTPException(400, detail="暂无等候号可叫")
    tid = row["id"]
    ts = now()
    # 叫号即占窗办理：号牌、窗口指针、事件在同事务一次落定，不留「叫号中」半截。
    conn.execute(
        "UPDATE tickets SET status='办理中', window_id=?, called_at=? WHERE id=?",
        (window_id, ts, tid),
    )
    conn.execute(
        "UPDATE windows SET current_ticket_id=? WHERE id=?",
        (tid, window_id),
    )
    event = log_event(conn, tid, window_id, "叫号", ts, "等候", "办理中")
    return _receipt(conn, "已叫号", tid, event)


def mark_skip(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ACTIVE:
        raise HTTPException(409, detail=f"当前状态「{row['status']}」不可过号")
    wid = row["window_id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='过号', finished_at=NULL WHERE id=?",
        (ticket_id,),
    )
    if wid:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
            (wid, ticket_id),
        )
    event = log_event(conn, ticket_id, wid, "过号", ts, row["status"], "过号")
    return _receipt(conn, "已过号", ticket_id, event)


def recall(conn, ticket_id: int, window_id: int | None = None) -> dict:
    """回收页重叫。作废为准：已作废号牌整笔拒绝，不写事件、不占窗。"""
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] == "已作废":
        raise HTTPException(409, detail="号牌已作废，重叫整笔拒绝")
    if row["status"] == "已办结":
        raise HTTPException(409, detail="号牌已办结，不可重叫")
    if row["status"] in ACTIVE:
        raise HTTPException(409, detail="号牌已在窗口办理中，不可重复重叫")
    if row["status"] not in RECALLABLE:
        raise HTTPException(409, detail=f"当前状态「{row['status']}」不可重叫")
    wid = window_id or row["window_id"]
    if not wid:
        raise HTTPException(400, detail="须指定窗口重叫")
    win = window_row(conn, wid)
    if not win["open"]:
        raise HTTPException(400, detail="窗口已关闭")
    busy = _window_busy_ticket(conn, wid)
    if busy and busy != ticket_id:
        raise HTTPException(409, detail="窗口仍有在办号，不可重叫")
    allowed = parse_kind_ids(win["kind_ids"])
    if row["kind_id"] not in allowed:
        raise HTTPException(400, detail="本窗口不可办该业务种类")
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='办理中', window_id=?, called_at=?, finished_at=NULL WHERE id=?",
        (wid, ts, ticket_id),
    )
    conn.execute("UPDATE windows SET current_ticket_id=? WHERE id=?", (ticket_id, wid))
    event = log_event(conn, ticket_id, wid, "重叫", ts, "过号", "办理中")
    return _receipt(conn, "已重叫", ticket_id, event)


def finish(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ACTIVE:
        raise HTTPException(409, detail=f"当前状态「{row['status']}」不可办结")
    wid = row["window_id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='已办结', finished_at=? WHERE id=?",
        (ts, ticket_id),
    )
    if wid:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
            (wid, ticket_id),
        )
    event = log_event(conn, ticket_id, wid, "办结", ts, row["status"], "已办结")
    return _receipt(conn, "已办结", ticket_id, event)


def void_ticket(conn, ticket_id: int) -> dict:
    """回收页作废。号牌进入终态已作废：解占窗、写作废事件；此后再重叫必硬拒。"""
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] == "已作废":
        raise HTTPException(409, detail="号牌已作废，重复作废已拒绝")
    if row["status"] == "已办结":
        raise HTTPException(409, detail="号牌已办结，不可作废")
    if row["status"] in ACTIVE:
        raise HTTPException(409, detail="号牌已重叫并在窗口办理中，不可作废")
    if row["status"] not in VOIDABLE:
        raise HTTPException(409, detail=f"当前状态「{row['status']}」不可作废")
    wid = row["window_id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='已作废', finished_at=? WHERE id=?",
        (ts, ticket_id),
    )
    # 不得占窗：即便历史指针残留也一并清掉。
    _clear_window_pointer(conn, ticket_id)
    event = log_event(conn, ticket_id, wid, "作废", ts, row["status"], "已作废")
    return _receipt(conn, "已作废", ticket_id, event)


def list_events(conn, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        """SELECT e.*, t.slip_no, w.name AS window_name
           FROM call_events e
           JOIN tickets t ON t.id = e.ticket_id
           LEFT JOIN windows w ON w.id = e.window_id
           ORDER BY e.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "号牌编号": r["ticket_id"],
            "号牌": r["slip_no"],
            "窗口": r["window_name"] or "",
            "动作": r["action"],
            "前态": r["from_status"],
            "后态": r["to_status"],
            "时刻": r["created_at"],
            "备注": r["note"],
        }
        for r in rows
    ]


def replay_ticket(conn, ticket_id: int) -> dict:
    """号牌动作回放：按事件序重放前态→后态，校验与当前终态是否闭链。

    作废与重叫二选一的结局因此可复核：已作废的号牌事件链上没有重叫，
    办理中的号牌事件链上没有作废；任何分叉都会在这里显示断链。
    """
    row = ticket_row(conn, ticket_id)
    events = conn.execute(
        "SELECT * FROM call_events WHERE ticket_id=? ORDER BY id", (ticket_id,)
    ).fetchall()
    journal = []
    closed = True
    prev = None
    unverifiable = False
    for i, e in enumerate(events, start=1):
        journal.append(
            {
                "序": i,
                "动作": e["action"],
                "前态": e["from_status"],
                "后态": e["to_status"],
                "时刻": e["created_at"],
                "窗口编号": e["window_id"],
                "备注": e["note"],
            }
        )
        # 旧库迁移来的事件没有前态/后态，无法核链，跳过该段比较而不误报。
        if not e["from_status"] and not e["to_status"]:
            unverifiable = True
            continue
        if prev is not None and e["from_status"] and e["from_status"] != prev:
            closed = False
        if e["to_status"]:
            prev = e["to_status"]
    if events:
        if prev is not None:
            closed = closed and prev == row["status"]
            note = "事件链闭合" if closed else "事件链与号牌当前状态不闭合"
        else:
            note = f"事件链闭合（{len(events)} 条旧事件无前态/后态，仅核终态）" if closed else "事件链与号牌当前状态不闭合"
    else:
        closed = row["status"] == "等候"
        note = "取号后未发生动作" if closed else "无动作事件，号牌却非初始等候态"
    return {
        "id": row["id"],
        "号牌": row["slip_no"],
        "当前状态": row["status"],
        "事件": journal,
        "闭合": closed,
        "说明": note,
    }
