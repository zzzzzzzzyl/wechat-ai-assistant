# -*- coding: utf-8 -*-
"""看库里到底存了什么（**只读**，程序开着也能跑）。

两个库：
  state/history.db   消息表（messages）—— 口吻样本、AI 标记还靠它
  state/screens.db   屏快照（screens）—— 上下文的顺序由它缝合出来

常用：
    python probes\\show_db.py                      # 总览：有哪些会话、各多少条
    python probes\\show_db.py -s 张三             # 看这个会话的消息（旧表，最近 40 条）
    python probes\\show_db.py -s 张三 --tail 100
    python probes\\show_db.py --screens             # 看屏快照（每屏一次读取）
    python probes\\show_db.py --screens -s 张三 --tail 3
    python probes\\show_db.py --stitch -s 张三    # **缝合结果 = 真正要喂给 AI 的上下文**
    python probes\\show_db.py --db 某个备份.db      # 看备份/别的库

会话名懒得打：先跑总览，再用 `--pick 3` 按序号选。

⚠️ 一律以 `mode=ro` 打开，不会写坏数据。
"""

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from wechat_assistant.window import stitch_frames, who_of      # noqa: E402


def ro_connect(path):
    """只读打开。文件不存在返回 None。"""
    if not os.path.exists(path):
        return None
    try:
        return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    except Exception as e:
        print("  打不开 %s: %s" % (path, e))
        return None


def fmt_time(s):
    return (s or "")[5:16]          # '2026-09-25 16:21:05' -> '09-25 16:21'


def who_of_row(r):
    if r["is_self"] == 1:
        return "我"
    if r["is_self"] == 0:
        return r["sender_name"] or "对方"
    return "?"


# ------------------------------------------------------------------ 总览

def show_overview(hist, hist_path, scr_path):
    print("=" * 78)
    print("库总览")
    print("=" * 78)

    print("历史库 %s" % hist_path)
    if hist is None:
        print("  （不存在）")
    else:
        rows = hist.execute(
            "SELECT session_key, COUNT(*) n,"
            " SUM(CASE WHEN is_self=1 THEN 1 ELSE 0 END) mine,"
            " SUM(CASE WHEN is_self=0 THEN 1 ELSE 0 END) other,"
            " SUM(CASE WHEN is_self IS NULL THEN 1 ELSE 0 END) unknown,"
            " SUM(CASE WHEN from_ai=1 THEN 1 ELSE 0 END) from_ai,"
            " MIN(created_at) a, MAX(created_at) b"
            " FROM messages GROUP BY session_key ORDER BY MAX(id) DESC").fetchall()
        if not rows:
            print("  （空的）")
        else:
            print("  %-3s %-24s %5s %5s %5s %5s %5s  %s"
                  % ("#", "会话", "条数", "我", "对方", "未判", "AI", "时间范围"))
            for i, r in enumerate(rows, 1):
                print("  %-3d %-24s %5d %5d %5d %5d %5d  %s ~ %s"
                      % (i, r[0][:24], r[1], r[2] or 0, r[3] or 0, r[4] or 0,
                         r[5] or 0, fmt_time(r[6]), fmt_time(r[7])))
            print("  合计 %d 条 / %d 个会话"
                  % (sum(r[1] for r in rows), len(rows)))

    print()
    scr = ro_connect(scr_path)
    print("屏快照库 %s" % scr_path)
    if scr is None:
        print("  （还没生成 —— 新代码跑过一次之后才会有）")
        return
    rows = scr.execute(
        "SELECT session_key, COUNT(*) n, MAX(seen_at) b FROM screens "
        "GROUP BY session_key ORDER BY MAX(id) DESC").fetchall()
    if not rows:
        print("  （空的）")
        return
    print("  %-3s %-24s %5s  %s" % ("#", "会话", "屏数", "最后记录于"))
    for i, r in enumerate(rows, 1):
        print("  %-3d %-24s %5d  %s" % (i, r[0][:24], r[1], fmt_time(r[2])))
    print("  合计 %d 屏 / %d 个会话（每屏约 5~8 条消息）"
          % (sum(r[1] for r in rows), len(rows)))


# ------------------------------------------------------------------ 消息表

def dump_messages(hist, session, tail):
    print("=" * 78)
    print("历史库 · 会话「%s」· 最近 %d 条" % (session, tail))
    print("=" * 78)
    hist.row_factory = sqlite3.Row
    rows = hist.execute(
        "SELECT * FROM messages WHERE session_key=? ORDER BY id DESC LIMIT ?",
        (session, tail)).fetchall()[::-1]
    if not rows:
        print("  （没有记录）")
        return
    total = hist.execute("SELECT COUNT(*) FROM messages WHERE session_key=?",
                         (session,)).fetchone()[0]
    print("  （该会话共 %d 条，下面是最新的 %d 条，按时间正序）\n" % (total, len(rows)))
    print("  %-6s %-12s %-10s %-4s %s" % ("id", "记录时间", "谁", "AI", "内容"))
    for r in rows:
        print("  %-6d %-12s %-10s %-4s %s"
              % (r["id"], fmt_time(r["created_at"]), who_of_row(r)[:10],
                 "是" if r["from_ai"] else "", (r["content"] or "")[:44]))


# ------------------------------------------------------------------ 屏快照

def _read_screens(scr, session, limit):
    scr.row_factory = sqlite3.Row
    rows = scr.execute(
        "SELECT * FROM screens WHERE session_key=? ORDER BY id DESC LIMIT ?",
        (session, limit)).fetchall()
    out = []
    for r in rows:
        try:
            items = json.loads(r["items_json"])
        except Exception:
            items = []
        out.append({"id": r["id"], "seen_at": r["seen_at"],
                    "at_bottom": bool(r["at_bottom"]), "items": items})
    return out


def dump_screens(scr, session, tail):
    print("=" * 78)
    print("屏快照 · 会话「%s」· 最近 %d 屏" % (session, tail))
    print("=" * 78)
    frames = _read_screens(scr, session, tail)
    if not frames:
        print("  （没有记录）")
        return
    total = scr.execute("SELECT COUNT(*) FROM screens WHERE session_key=?",
                        (session,)).fetchone()[0]
    print("  （该会话共 %d 屏，下面是最近的 %d 屏，**最新在前**）\n" % (total, len(frames)))
    for f in frames:
        print("  [屏 #%d] %s  在底部=%s  %d 条"
              % (f["id"], f["seen_at"][11:], "是" if f["at_bottom"] else "否",
                 len(f["items"])))
        for j, it in enumerate(f["items"], 1):
            print("      %d. %-8s %s" % (j, who_of(it)[:8],
                                         (it.get("text") or "")[:46]))
        print()


def dump_stitch(scr, session, max_items, max_screens):
    print("=" * 78)
    print("缝合结果 · 会话「%s」" % session)
    print("=" * 78)
    print("  **这就是实际上要喂给 AI 的历史**（从最近 20 屏缝出来的）")
    print()
    frames = _read_screens(scr, session, max_screens)
    if not frames:
        print("  （没有记录）")
        return
    merged, gap = stitch_frames([f["items"] for f in frames], max_items)
    print("  用 %d 屏缝出 %d 条，gap=%s%s"
          % (len(frames), len(merged), gap,
             "  ← 有屏接不上，中间可能有没看到的内容" if gap else ""))
    print()
    print("  旧 ─────────────────────────────────────────────► 新")
    for it in merged:
        print("    %-10s %s" % (who_of(it)[:10], (it.get("text") or "")[:50]))


# ------------------------------------------------------------------ 选会话

def sessions_of(con, table):
    """会话列表。⚠️ 排序必须和总览里**完全一致**，否则 `--pick N` 会选到别的会话
    （第一版这里用的是不带 ORDER BY 的 DISTINCT，序号跟总览对不上）。"""
    return [r[0] for r in con.execute(
        "SELECT session_key FROM %s GROUP BY session_key ORDER BY MAX(id) DESC"
        % table)]


def pick(con, table, name, index):
    names = sessions_of(con, table)
    if index:
        if 1 <= index <= len(names):
            return names[index - 1]
        print("序号 %d 超范围（共 %d 个会话）" % (index, len(names)))
        return None
    if name in names:
        return name
    hit = [n for n in names if name in n]
    if len(hit) == 1:
        return hit[0]
    if len(hit) > 1:
        print("「%s」匹配到多个会话：%s" % (name, hit))
        return None
    print("没找到会话「%s」。现有：%s" % (name, names))
    return None


HIST = os.path.join(BASE, "state", "history.db")
SCREENS = os.path.join(BASE, "state", "screens.db")


def main():
    ap = argparse.ArgumentParser(description="看库里存了什么（只读）")
    ap.add_argument("--db", help="历史库路径（默认 state/history.db）")
    ap.add_argument("--screens-db", help="屏快照库路径（默认 state/screens.db）")
    ap.add_argument("-s", "--session", help="会话名（支持子串匹配）")
    ap.add_argument("--pick", type=int, help="按总览里的序号选会话")
    ap.add_argument("--tail", type=int, default=40, help="看最近多少条/屏（默认 40）")
    ap.add_argument("--screens", action="store_true", help="看屏快照而不是消息表")
    ap.add_argument("--stitch", action="store_true",
                    help="看缝合结果（真正喂给 AI 的上下文）")
    ap.add_argument("--max-items", type=int, default=60, help="缝合最多多少条")
    ap.add_argument("--max-screens", type=int, default=20, help="缝合最多用多少屏")
    args = ap.parse_args()

    hist_path = args.db or HIST
    scr_path = args.screens_db or SCREENS
    hist = ro_connect(hist_path)
    scr = ro_connect(scr_path)

    if hist is None and scr is None:
        print("两个库都不存在：\n  %s\n  %s" % (hist_path, scr_path))
        return 1

    # 没指定会话 -> 总览
    if not args.session and not args.pick:
        show_overview(hist, hist_path, scr_path)
        return 0

    # 选会话：--stitch / --screens 走屏快照库，否则走消息表
    if args.stitch:
        if scr is None:
            print("屏快照库还不存在：%s\n（新代码跑过一次之后才会有）" % scr_path)
            return 1
        s = pick(scr, "screens", args.session, args.pick)
        if s:
            dump_stitch(scr, s, args.max_items, args.max_screens)
        return 0

    if args.screens:
        if scr is None:
            print("屏快照库还不存在：%s" % scr_path)
            return 1
        s = pick(scr, "screens", args.session, args.pick)
        if s:
            dump_screens(scr, s, args.tail)
        return 0

    if hist is None:
        print("历史库不存在：%s" % hist_path)
        return 1
    s = pick(hist, "messages", args.session, args.pick)
    if s:
        dump_messages(hist, s, args.tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
