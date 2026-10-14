# -*- coding: utf-8 -*-
"""验证「用前后语句内容比对来定位插入点」这个思路能不能真跑通。

思路（用户提出）：
    屏幕上一屏消息 → 和库里该会话的历史**做序列对齐** → 找出重合的连续段作为锚点
    → 屏幕上有、库里没有的，就是缺失的历史 → 按锚点位置**插进去**，而不是一律接到库尾。

本脚本用**真实数据**验证可行性：
    左 = 当前屏幕上真实可见的消息文本（UIA 读的，按竖直顺序）
    右 = history.db 里该会话的真实历史（按 id 顺序）

只看对齐结果，不写任何东西。纯只读。
"""

import ctypes
import difflib
import os
import sqlite3
import sys

sys.path.insert(0, ".")

from wechat_assistant.config import load_config          # noqa: E402
from wechat_assistant.reader import WeChatReader         # noqa: E402
from wechat_assistant.uia_worker import UiaWorker        # noqa: E402

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "..", "state", "history.db")


def read_screen(cfg):
    worker = UiaWorker(20.0)
    reader = WeChatReader(cfg, worker)
    win = reader.find_window()
    if not win:
        worker.close()
        return None, None, None
    snap = reader.read_chat(win["hwnd"])
    worker.close()
    if snap is None:
        return None, None, None
    msgs = sorted([m for m in snap.messages if (m.text or "").strip()],
                  key=lambda m: m.rect[1] if m.rect else 0)
    return snap.title, [m.text.strip() for m in msgs], snap


def read_db(session_key):
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    rows = con.execute(
        "SELECT id, content FROM messages WHERE session_key = ? ORDER BY id",
        (session_key,)).fetchall()
    con.close()
    return rows


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    cfg = load_config()
    title, screen, snap = read_screen(cfg)
    if not screen:
        print("读不到当前会话的消息")
        return 1

    print("=" * 78)
    print("序列对齐验证：屏幕 vs 历史库")
    print("会话: %r   屏幕上可见 %d 条   容器 rect=%s"
          % (title, len(screen), snap.container_rect))
    print("=" * 78)

    db = read_db(title)
    if not db:
        print("库里没有这个会话（%r）的记录" % title)
        return 0
    db_ids = [r[0] for r in db]
    db_texts = [r[1] for r in db]
    print("库里该会话 %d 条（id %s ~ %s）" % (len(db), db_ids[0], db_ids[-1]))
    print()
    print("屏幕内容（按竖直顺序）:")
    for i, t in enumerate(screen):
        print("  S%-2d %r" % (i, t[:50]))
    print()

    # autojunk=False：聊天里「图片」「动画表情」这类重复内容极多，
    # 默认的 autojunk 启发式会把它们当垃圾丢掉，对齐结果不可用
    sm = difflib.SequenceMatcher(None, screen, db_texts, autojunk=False)
    ops = sm.get_opcodes()

    print("对齐结果（get_opcodes）:")
    print("  %-9s %-14s %-14s" % ("动作", "屏幕区间", "库区间"))
    for tag, i1, i2, j1, j2 in ops:
        mark = {"equal": "  重合", "insert": "  库里有、屏幕上没看到",
                "delete": "  屏幕上有、库里没有", "replace": "  对不上"}.get(tag, "")
        print("  %-9s S[%d:%d]%-6s D[%d:%d]%-6s%s"
              % (tag, i1, i2, "", j1, j2, "", mark))

    # ---- 关键判定：屏幕能不能在库里被唯一定位
    equal_blocks = [(i1, i2, j1, j2) for t, i1, i2, j1, j2 in ops if t == "equal"]
    print()
    print("=" * 78)
    print("能不能定位？")
    print("=" * 78)
    if not equal_blocks:
        print("  一个重合块都没有 → 无法定位（只能退回保守策略：不写）")
        return 0

    longest = max(equal_blocks, key=lambda b: b[1] - b[0])
    i1, i2, j1, j2 = longest
    print("  最长重合块：屏幕 S[%d:%d] ↔ 库 D[%d:%d]，共 %d 条"
          % (i1, i2, j1, j2, i2 - i1))
    print("  → 屏幕第 %d 条 == 库第 %d 条（id=%s）"
          % (i1, db_ids[j1], db_ids[j1]))
    print("  → 屏幕第 %d 条 == 库第 %d 条（id=%s）"
          % (i2 - 1, db_ids[j2 - 1], db_ids[j2 - 1]))
    print("  独立重合块数：%d（>1 说明内容重复、可能有歧义）" % len(equal_blocks))
    if len(equal_blocks) > 1:
        print("     注意：重复内容（「图片」「动画表情」）会产生多个候选锚点，")
        print("          需要用「最长的连续块 + 靠后优先」来消歧。")

    # ---- 屏幕上库里没有的（可插入的候选）
    missing = []
    for tag, a1, a2, b1, b2 in ops:
        if tag in ("delete", "replace"):
            missing.extend(range(a1, a2))
    if missing:
        print()
        print("  屏幕上有、库里没有的 %d 条（这批就是能补录进去的）:" % len(missing))
        for k, idx in enumerate(missing):
            print("    S%-2d %r" % (idx, screen[idx][:50]))
        print("  插入位置：按上面对齐到的锚点插，而不是一律接到库尾。")
    else:
        print()
        print("  屏幕上的消息库里全都有 → 本次无需写入（当前行为也是这样）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
