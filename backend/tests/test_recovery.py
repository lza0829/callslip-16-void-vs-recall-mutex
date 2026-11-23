"""回收页「作废 vs 重叫」双动作联动回归。

口径（全局唯一）：作废为准。
- 已作废的号牌重叫整笔拒绝：不写重叫事件、不占窗、不动指针。
- 重叫命中即落办理中占窗；此后作废按状态规则拒绝。
- 每次动作在一个 BEGIN IMMEDIATE 事务里同刷号牌、窗口指针、事件；
  失败整笔回滚；并发双点只有一个赢家，绝不出现双终态。

运行：python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="callslip-test-")
os.environ["CALLSLIP_DB"] = str(Path(_TMP) / "test.db")

from app.db import connect, db, init_db  # noqa: E402
from app.services import tickets as tic  # noqa: E402


def _seed() -> None:
    init_db()
    conn = connect()
    today = date.today().isoformat()
    conn.execute("BEGIN IMMEDIATE")
    k = conn.execute(
        "INSERT INTO business_kinds(name, prefix, seq, active) VALUES ('个人储蓄','储',0,1)"
    ).lastrowid
    w = conn.execute(
        "INSERT INTO windows(name, kind_ids, open) VALUES ('一号窗', ?, 1)", (str(k),)
    ).lastrowid
    conn.execute("INSERT INTO settings(key, value) VALUES ('营业日', ?)", (today,))
    conn.execute(
        "INSERT INTO day_seals(business_day, sealed, note) VALUES (?,0,'测试')", (today,)
    )
    conn.execute("COMMIT")
    conn.close()


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _seed()

    def setUp(self) -> None:
        conn = connect()
        day = conn.execute("SELECT value FROM settings WHERE key='营业日'").fetchone()[0]
        kind = conn.execute("SELECT id FROM business_kinds LIMIT 1").fetchone()[0]
        win = conn.execute("SELECT id FROM windows LIMIT 1").fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE windows SET current_ticket_id=NULL")
        conn.execute(
            "UPDATE tickets SET status='已办结' WHERE status IN ('过号','办理中','叫号中','已作废')"
        )
        cur = conn.execute(
            """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id,
                                   queue_pos, taken_at, note)
               VALUES (?,?,?,'过号',NULL,?,?,'test')""",
            (f"T{self._testMethodName[:8]}-{os.urandom(3).hex()}", kind, day, 1, day + "T09:00:00"),
        )
        self.tid = cur.lastrowid
        self.wid = win
        self.day = day
        conn.execute("COMMIT")
        conn.close()

    # -- helpers ---------------------------------------------------------
    def _status(self) -> str:
        c = connect()
        s = c.execute("SELECT status FROM tickets WHERE id=?", (self.tid,)).fetchone()[0]
        c.close()
        return s

    def _pointer(self):
        c = connect()
        p = c.execute(
            "SELECT current_ticket_id FROM windows WHERE id=?", (self.wid,)
        ).fetchone()[0]
        c.close()
        return p

    def _actions(self) -> list[str]:
        c = connect()
        rs = c.execute(
            "SELECT action FROM call_events WHERE ticket_id=? ORDER BY id", (self.tid,)
        ).fetchall()
        c.close()
        return [r[0] for r in rs]

    # -- 顺序不变量 ------------------------------------------------------
    def test_void_then_recall_rejected(self) -> None:
        with db(write=True) as c:
            tic.void_ticket(c, self.tid)
        self.assertEqual(self._status(), "已作废")
        self.assertIsNone(self._pointer())
        with self.assertRaises(Exception) as ctx:
            with db(write=True) as c:
                tic.recall(c, self.tid, self.wid)
        self.assertIn("已作废", str(ctx.exception))
        self.assertEqual(self._status(), "已作废")
        self.assertIsNone(self._pointer(), "已作废号不得占窗")
        self.assertEqual(self._actions(), ["作废"], "拒因不得写重叫事件")

    def test_recall_then_void_rejected(self) -> None:
        with db(write=True) as c:
            out = tic.recall(c, self.tid, self.wid)
        self.assertEqual(out["状态"], "办理中")
        self.assertTrue(out["占窗"])
        self.assertEqual(self._pointer(), self.tid)
        with self.assertRaises(Exception) as ctx:
            with db(write=True) as c:
                tic.void_ticket(c, self.tid)
        self.assertIn("不可作废", str(ctx.exception))
        self.assertEqual(self._status(), "办理中")
        self.assertEqual(self._pointer(), self.tid)
        self.assertEqual(self._actions(), ["重叫"])

    def test_double_void_second_rejected(self) -> None:
        with db(write=True) as c:
            tic.void_ticket(c, self.tid)
        with self.assertRaises(Exception):
            with db(write=True) as c:
                tic.void_ticket(c, self.tid)
        self.assertEqual(self._actions(), ["作废"])

    def test_rollback_leaves_no_half_state(self) -> None:
        original = tic.log_event

        def boom(*a, **kw):
            raise RuntimeError("event write failed")

        tic.log_event = boom
        try:
            with self.assertRaises(RuntimeError):
                with db(write=True) as c:
                    tic.void_ticket(c, self.tid)
        finally:
            tic.log_event = original
        self.assertEqual(self._status(), "过号", "半截须回滚")
        self.assertEqual(self._actions(), [])
        # 回滚后再次作废应干净成功
        with db(write=True) as c:
            tic.void_ticket(c, self.tid)
        self.assertEqual(self._status(), "已作废")

    def test_replay_closure(self) -> None:
        with db(write=True) as c:
            tic.recall(c, self.tid, self.wid)
        c = connect()
        rep = tic.replay_ticket(c, self.tid)
        c.close()
        self.assertTrue(rep["闭合"])
        self.assertEqual([e["动作"] for e in rep["事件"]], ["重叫"])
        self.assertEqual(rep["事件"][0]["前态"], "过号")
        self.assertEqual(rep["事件"][0]["后态"], "办理中")

    # -- 并发双点 --------------------------------------------------------
    def _reset_ticket(self, tid: int) -> None:
        c = connect()
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE windows SET current_ticket_id=NULL WHERE current_ticket_id=?", (tid,))
        c.execute("UPDATE tickets SET status='过号', window_id=NULL WHERE id=?", (tid,))
        c.execute("DELETE FROM call_events WHERE ticket_id=?", (tid,))
        c.execute("COMMIT")
        c.close()

    def _make_skipped(self) -> int:
        c = connect()
        kind = c.execute("SELECT id FROM business_kinds LIMIT 1").fetchone()[0]
        cur = c.execute(
            """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id,
                                   queue_pos, taken_at, note)
               VALUES (?, ?, ?, '过号', NULL, 1, ?, 'test')""",
            (f"X{os.urandom(4).hex()}", kind, self.day, self.day + "T09:00:00"),
        )
        c.commit()
        tid = cur.lastrowid
        c.close()
        return tid

    def test_concurrent_void_recall_single_winner(self) -> None:
        for round_no in range(10):
            self._reset_ticket(self.tid)

            results: list[tuple[str, str]] = []
            barrier = threading.Barrier(4)

            def worker(kind: str) -> None:
                barrier.wait()
                try:
                    with db(write=True) as cc:
                        if kind == "void":
                            tic.void_ticket(cc, self.tid)
                        else:
                            tic.recall(cc, self.tid, self.wid)
                    results.append((kind, "ok"))
                except Exception as e:  # noqa: BLE001
                    results.append((kind, str(e)))

            threads = [
                threading.Thread(target=worker, args=("void" if i % 2 == 0 else "recall",))
                for i in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            wins = [r for r in results if r[1] == "ok"]
            self.assertEqual(len(wins), 1, f"第{round_no}轮应恰有一个赢家：{results}")
            status = self._status()
            acts = self._actions()
            if wins[0][0] == "void":
                self.assertEqual(status, "已作废")
                self.assertIsNone(self._pointer())
                self.assertNotIn("重叫", acts)
            else:
                self.assertEqual(status, "办理中")
                self.assertEqual(self._pointer(), self.tid)
                self.assertNotIn("作废", acts)

    def test_same_ticket_recalled_to_two_windows(self) -> None:
        """同一号牌被两个窗口同时重叫：只落一个窗，另一窗不得被虚占。"""
        c = connect()
        c.execute("BEGIN IMMEDIATE")
        kind = c.execute("SELECT id FROM business_kinds LIMIT 1").fetchone()[0]
        w2 = c.execute(
            "INSERT INTO windows(name, kind_ids, open) VALUES (?, ?, 1)",
            (f"二窗{os.urandom(2).hex()}", str(kind)),
        ).lastrowid
        c.execute("COMMIT")
        c.close()

        for round_no in range(6):
            self._reset_ticket(self.tid)
            results: list[tuple[int, str]] = []
            barrier = threading.Barrier(2)

            def worker(wid: int) -> None:
                barrier.wait()
                try:
                    with db(write=True) as cc:
                        tic.recall(cc, self.tid, wid)
                    results.append((wid, "ok"))
                except Exception as e:  # noqa: BLE001
                    results.append((wid, str(e)))

            threads = [threading.Thread(target=worker, args=(w,)) for w in (self.wid, w2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            wins = [r for r in results if r[1] == "ok"]
            self.assertEqual(len(wins), 1, f"第{round_no}轮：{results}")
            winner = wins[0][0]
            c = connect()
            ptrs = {
                w: c.execute("SELECT current_ticket_id FROM windows WHERE id=?", (w,)).fetchone()[0]
                for w in (self.wid, w2)
            }
            c.close()
            self.assertEqual(ptrs[winner], self.tid)
            self.assertIsNone(ptrs[w2 if winner == self.wid else self.wid], "落败窗口不得虚占")
            self.assertEqual(self._status(), "办理中")
            self._reset_ticket(self.tid)

    def test_two_tickets_recalled_to_one_window(self) -> None:
        """两个过号同时重叫进同一窗口：只能进一个，另一笔硬拒。"""
        for round_no in range(4):
            a, b = self._make_skipped(), self._make_skipped()
            results: list[tuple[int, str]] = []
            barrier = threading.Barrier(2)

            def worker(tid: int) -> None:
                barrier.wait()
                try:
                    with db(write=True) as cc:
                        tic.recall(cc, tid, self.wid)
                    results.append((tid, "ok"))
                except Exception as e:  # noqa: BLE001
                    results.append((tid, str(e)))

            threads = [threading.Thread(target=worker, args=(x,)) for x in (a, b)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            wins = [r for r in results if r[1] == "ok"]
            self.assertEqual(len(wins), 1, f"第{round_no}轮：{results}")
            winner = wins[0][0]
            c = connect()
            ptr = c.execute("SELECT current_ticket_id FROM windows WHERE id=?", (self.wid,)).fetchone()[0]
            loser_status = c.execute(
                "SELECT status FROM tickets WHERE id=?", (b if winner == a else a,)
            ).fetchone()[0]
            c.close()
            self.assertEqual(ptr, winner)
            self.assertEqual(loser_status, "过号", "落败号牌应仍可重试")
            self._reset_ticket(winner)


if __name__ == "__main__":
    unittest.main()
