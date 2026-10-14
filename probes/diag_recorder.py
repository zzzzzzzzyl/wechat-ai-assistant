# -*- coding: utf-8 -*-
"""只读排查：记录器的「内容没变就跳过」为什么没生效。

`Recorder._has_new()` 会拿屏幕上的消息文本和库里最近的 N 条比对，
相同就跳过发送者判定（省 ~45ms）。但实测日志里
    [记录] 已轮 30 次 | 新增 4 条 | 无变化 0
「无变化」是 0 —— 说明它每次都觉得有变化，于是每次都白做一遍判定。

本脚本把两边的实际内容打出来对照，看差在哪。

⚠️ 只读：不写库、不改微信。
用法： python probes/diag_recorder.py
"""

import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.reader import WeChatReader  # noqa: E402
from wechat_assistant.store import MessageStore  # noqa: E402
from wechat_assistant.uia_worker import UiaWorker  # noqa: E402


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    cfg = load_config()
    db = cfg.get("history.path", "state/history.db")
    if not os.path.isabs(db):
        db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), db)
    store = MessageStore(db, logger=lambda m: None)

    worker = UiaWorker(20.0)
    reader = WeChatReader(cfg, worker)
    win = reader.find_window()
    snap = reader.read_chat(win["hwnd"])
    worker.close()

    key = snap.title
    texts = [(m.text or "").strip() for m in snap.messages]
    texts = [t for t in texts if t]

    recent = store.get_recent(key, len(texts))
    old = [(r.get("content") or "").strip() for r in recent]

    print("会话: %r" % key)
    print("列表是否在底部: %s   bottom_gap = %.1f px"
          % (snap.at_bottom(), snap.bottom_gap()))
    if not snap.at_bottom():
        print("  ⚠️ 现在正翻着历史！recorder 若此时记录，就会把**旧消息接到库尾**，")
        print("     于是 get_recent(20) 取到的「最近 20 条」其实是旧消息 —— AI 会拿它当当前上下文。")
    print("屏幕可见 %d 条，库里取最近 %d 条\n" % (len(texts), len(old)))

    print("%-4s %-38s | %-38s %s" % ("#", "屏幕上的", "库里的", "一致?"))
    print("-" * 100)
    n = max(len(texts), len(old))
    same = 0
    for i in range(n):
        a = texts[i] if i < len(texts) else "<无>"
        b = old[i] if i < len(old) else "<无>"
        ok = "✓" if a == b else "✗"
        if a == b:
            same += 1
        print("%-4d %-38r | %-38r %s" % (i, a[:36], b[:36], ok))

    print()
    print("逐行相同的条数: %d / %d" % (same, n))
    print()
    # Recorder._has_new 的实际判断
    verdict = (old != texts[-len(old):]) if old else True
    print("Recorder._has_new 会说: %s" % ("「有新内容」（要判定+存库）" if verdict else "「没变化」（跳过）"))
    print()
    if verdict:
        tail = texts[-len(old):] if old else texts
        diff = [(i, a, b) for i, (a, b) in enumerate(zip(old, tail)) if a != b]
        print("差异明细（库里 vs 屏幕尾部对齐后）:")
        for i, a, b in diff[:10]:
            print("   [%d] 库=%-34r 屏=%r" % (i, a[:32], b[:32]))
        if not diff:
            print("   （竟然完全一致 —— 说明差异出在长度/对齐，不是内容）")
            print("   屏幕 %d 条 vs 库里 %d 条" % (len(texts), len(old)))

    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
