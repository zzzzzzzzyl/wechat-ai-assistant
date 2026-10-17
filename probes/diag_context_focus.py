# -*- coding: utf-8 -*-
"""只读诊断：一次生成时，「当前屏（回复目标）」和「历史」里到底是什么。

回答用户反馈的那两种手感：
  · 「明明有上下文，模型只盯着最后一两条无关紧要的回复」
  · 「容易被噪声带偏」

只打印统计（条数 / 类型 / 发送者 / 长度），**不打印消息正文**。

    python probes\diag_context_focus.py
"""
import json
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wechat_assistant import window          # noqa: E402  （只依赖标准库）

SCR = os.path.join(ROOT, "state", "screens.db")
MAX_MESSAGES = 20        # runtime.max_messages
HISTORY_CANDIDATES = 40  # reply.history_messages


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)


def kind_of(it):
    return it.get("kind") or "text"


def show_block(block):
    n = len(block)
    kinds = {}
    who = {}
    q = 0
    for it in block:
        kinds[kind_of(it)] = kinds.get(kind_of(it), 0) + 1
        w = window.who_of(it)
        who[w] = who.get(w, 0) + 1
        if w == "?":
            q += 1
    last = block[-1] if block else None
    last_who = window.who_of(last) if last else "-"
    last_self = last.get("is_self") if last else None
    tail_len = sum(len((it.get("text") or "")) for it in block[-2:])
    return {
        "n": n, "kinds": kinds, "who": who, "q": q,
        "last_who": last_who, "last_self": last_self,
        "tail_len": tail_len,
        "last_is_noise": last_who in ("?", "群友") or last_self is True,
    }


def main():
    if not os.path.exists(SCR):
        print("找不到 state/screens.db")
        return 1
    conn = ro(SCR)
    sessions = [r[0] for r in conn.execute(
        "SELECT session_key, COUNT(*) FROM screens GROUP BY session_key "
        "ORDER BY COUNT(*) DESC").fetchall()]
    print("screens.db 会话数: %d\n" % len(sessions))

    for key in sessions:
        frames = []
        for (raw,) in conn.execute(
                "SELECT items_json FROM screens WHERE session_key=? ORDER BY id",
                (key,)).fetchall()[:80]:
            try:
                frames.append(json.loads(raw or "[]"))
            except Exception:
                frames.append([])
        if not frames:
            continue
        # 最新一屏 = 当前屏（screens 按 id 升序，最后一个最新）
        latest = frames[-1]
        block = [it for it in latest if (it.get("text") or "").strip()
                 and not window.is_time_item(it)]
        if not block:
            print("%-22s 当前屏没有可读消息" % key[:22])
            continue
        b = show_block(block)

        # 缝合历史（与 run_watch 同一条路径）
        items, gap = window.stitch_frames(
            list(reversed(frames)), max_items=HISTORY_CANDIDATES,
            cut_at_date=True)
        hist = window.to_prompt_dicts(items)
        # 摘掉回复目标：从 block 第一条往前砍（与 run_watch 相同的做法）
        first = (block[0].get("text") or "").strip()
        hit = False
        for i in range(len(hist) - 1, -1, -1):
            if hist[i]["text"] == first:
                hist = hist[:i]
                hit = True
                break
        hist = hist[-MAX_MESSAGES:]

        overlap = len(set(h["text"] for h in hist) &
                      set((it.get("text") or "").strip() for it in block))
        hq = sum(1 for h in hist if h["who"] == "?")
        hgroup = sum(1 for h in hist if h["who"] == "群友")
        hchars = sum(len(h["text"]) for h in hist)
        bchars = sum(len((it.get("text") or "")) for it in block)

        print("%-22s 屏%d 当前屏 %d 条/%d 字 历史 %d 条/%d 字 %s"
              % (key[:22], len(frames), b["n"], bchars, len(hist), hchars,
                 "gap" if gap else ""))
        print("   当前屏: 类型=%s 发送者=%s 最后一条=%-4s 后两条共%d字%s"
              % (b["kinds"], b["who"], b["last_who"], b["tail_len"],
                 "  ⚠️最后一条是「我」/噪声" if b["last_is_noise"] else ""))
        print("   历史:   摘目标%s 重叠%d条 未判定%d 群友%d"
              % ("命中" if hit else "未命中(重复!)", overlap, hq, hgroup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
