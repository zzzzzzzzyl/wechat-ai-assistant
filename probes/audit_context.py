# -*- coding: utf-8 -*-
"""只读统计：现在「进 prompt 的历史」到底有多大、被丢掉多少。

用途：判断「L2 摘要层」值不值得做 —— 先量，再决定。判据见
`上下文分层与管理调研.md` §3。

⚠️ 只读：sqlite 用 mode=ro 打开；**只打印数字，不打印任何消息正文**。
⚠️ 探针可以在程序开着时跑（只读连接，不抢写锁）。

    python probes\audit_context.py

改顶部的 `MAX_MESSAGES` / `MAX_CHARS` 就能回答"调大条数闸会多花多少字"。
"""
import json
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(ROOT, "state", "history.db")
SCR = os.path.join(ROOT, "state", "screens.db")

MAX_MSG_CHARS = 500      # runtime.max_msg_chars
MAX_MESSAGES = 20        # runtime.max_messages
MAX_CHARS = 4000         # runtime.max_chars
HISTORY_CANDIDATES = 40  # reply.history_messages


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)


def simulate(rows):
    """按 ai.render_history 的顺序模拟：先取最近 20 条 -> 单条截断 -> 总量从最旧丢。"""
    recent = rows[-MAX_MESSAGES:]
    cut_n = len(rows) - len(recent)          # 被「条数闸」截掉多少条
    cut_chars = sum(len(w) + len(t) + 2 for w, t in rows[:cut_n])
    kept, truncated = [], 0
    for who, text in recent:
        if len(text) > MAX_MSG_CHARS:
            text = text[:MAX_MSG_CHARS]
            truncated += 1
        kept.append((who, text))
    total = sum(len(w) + len(t) + 2 for w, t in kept)
    dropped_chars = 0
    dropped_n = 0
    while kept and total > MAX_CHARS:
        w, t = kept.pop(0)
        total -= len(w) + len(t) + 2
        dropped_chars += len(t)
        dropped_n += 1
    return kept, truncated, dropped_n, dropped_chars, total, cut_n, cut_chars


def main():
    if not (os.path.exists(HIST) and os.path.exists(SCR)):
        print("找不到 state/history.db 或 state/screens.db")
        return 1

    h = ro(HIST)
    sessions = [r[0] for r in h.execute(
        "SELECT session_key, COUNT(*) FROM messages GROUP BY session_key "
        "ORDER BY COUNT(*) DESC").fetchall()]
    print("history.db 会话数: %d" % len(sessions))
    print("%-24s %5s %11s %9s %9s %9s %9s" % (
        "会话", "条数", "跨度(天)", "候选字", "进prompt", "条数截断", "长度丢条"))
    tot_cand = tot_kept = tot_drop_c = tot_cut_c = 0
    cut_days = 0
    for key in sessions:
        rows = h.execute(
            "SELECT is_self, sender_name, content, created_at FROM messages "
            "WHERE session_key=? ORDER BY id DESC LIMIT ?",
            (key, HISTORY_CANDIDATES)).fetchall()
        rows.reverse()
        msgs = []
        for is_self, sender, content, _ in rows:
            who = sender or ("我" if is_self == 1 else "对方")
            msgs.append((who, content or ""))
        (kept, truncated, dropped_n, dropped_chars, kept_chars,
         cut_n, cut_chars) = simulate(msgs)
        cand_chars = sum(len(w) + len(t) + 2 for w, t in msgs)
        days = len(set(str(r[0])[:10] for r in h.execute(
            "SELECT created_at FROM messages WHERE session_key=?",
            (key,)).fetchall()))
        if days > 1:
            cut_days += 1
        tot_cand += cand_chars
        tot_kept += kept_chars
        tot_drop_c += dropped_chars
        tot_cut_c += cut_chars
        print("%-24s %5d %11d %9d %9d %9d %9d" % (
            key[:24], len(rows), days, cand_chars, kept_chars,
            cut_n, dropped_n))

    print("-" * 80)
    print("候选 %d 字 -> 进 prompt %d 字" % (tot_cand, tot_kept))
    print("  被条数闸(>%d 条)截掉: %d 字" % (MAX_MESSAGES, tot_cut_c))
    print("  被长度闸(>%d 字)丢掉: %d 字" % (MAX_CHARS, tot_drop_c))
    print("跨天（>1 个自然日）的会话: %d / %d" % (cut_days, len(sessions)))

    s = ro(SCR)
    print("\nscreens.db:")
    print("%-24s %5s %8s %10s %9s" % ("会话", "屏数", "条目", "日期分隔", "最早屏"))
    tot_items = tot_markers = 0
    for key, n, last in s.execute(
            "SELECT session_key, COUNT(*), MAX(seen_at) FROM screens "
            "GROUP BY session_key ORDER BY COUNT(*) DESC").fetchall():
        items = 0
        markers = 0
        first = None
        for (raw,) in s.execute(
                "SELECT items_json FROM screens WHERE session_key=? ORDER BY id",
                (key,)).fetchall():
            try:
                arr = json.loads(raw or "[]")
            except Exception:
                arr = []
            items += len(arr)
            for it in arr:
                if isinstance(it, dict) and it.get("date"):
                    markers += 1
            if first is None:
                first = str(arr[0].get("date") or "")[:10] if arr else None
        tot_items += items
        tot_markers += markers
        print("%-24s %5d %8d %10d %9s" % (key[:24], n, items, markers, first or "-"))
    print("-" * 80)
    print("合计屏条目 %d，其中跨天日期分隔 %d 个" % (tot_items, tot_markers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
