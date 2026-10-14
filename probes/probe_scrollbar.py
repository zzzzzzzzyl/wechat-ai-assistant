# -*- coding: utf-8 -*-
"""只读探测：能不能用**滚动条**判断「是否在底部」，替代基于位置的 gap 判据。

为什么要换判据
--------------
现在的 `ChatSnapshot.at_bottom()` 用的是：
    gap = container_bottom - 最后一条可见消息的 bottom
    在底部 -> gap ≈ 0；往上滚 -> gap < 0

但有个漏洞（用户指出）：列表是**虚拟化**的，最后一条滚出渲染区后会从 UIA
树里消失，「最后一条」就换成了上一条。设往上滚了 S、那一条高度是 h：

    滚出前：gap = -S
    滚出后：gap = h - S

**当 S 正好等于若干条消息高度时，gap 会变成 0 -> 被误判成「在底部」。**
单聊里消息高度都是 84，滚轮步进又常是它的倍数，很容易撞上。

滚动条则没有这个问题：`value == maximum` 就是到底了，与消息高度无关。
本脚本查：
    1. 消息列表附近有没有滚动条元素
    2. 它支不支持 RangeValuePattern（能不能读到 value / maximum）
    3. UIA 树里到底报告了多少条消息（换行看渲染区有多大）

⚠️ 只读：不改窗口、不滚动、不点击。
用法： python probes/probe_scrollbar.py
"""

import ctypes
import sys

sys.path.insert(0, ".")

from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.reader import WeChatReader  # noqa: E402
from wechat_assistant.uia_worker import UiaWorker  # noqa: E402


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    cfg = load_config()
    worker = UiaWorker(20.0)
    reader = WeChatReader(cfg, worker)
    win = reader.find_window()
    hwnd = win["hwnd"]
    id_msglist = cfg.get("wechat.node.message_list", "chat_message_list")

    import comtypes.client
    from comtypes.gen import UIAutomationClient as uia

    def job(auto, uia_mod):
        el = auto.ControlFromHandle(hwnd).Element
        arr = el.FindAll(4, uia_mod.CreateTrueCondition())   # Descendants
        rows = []
        for i in range(arr.Length):
            e = arr.GetElement(i)
            try:
                cls = e.CurrentClassName or ""
                aid = e.CurrentAutomationId or ""
                name = e.CurrentName or ""
                l, t, r, b = e.CurrentBoundingRectangle
            except Exception:
                continue
            # 支不支持 RangeValue（滚动条的 value/maximum 就在这个 pattern 里）
            try:
                rvp = e.GetCurrentPattern(uia.UIA_RangeValuePatternId)
                rng = rvp.QueryInterface(uia.IUIAutomationRangeValuePattern)
                val = rng.CurrentValue
                mx = rng.CurrentMaximum
                mn = rng.CurrentMinimum
                rngtxt = "value=%.1f min=%.1f max=%.1f" % (val, mn, mx)
            except Exception:
                rngtxt = ""
            rows.append((cls, aid, name, (l, t, r, b), rngtxt))
        return rows

    rows = worker.call(job)
    title = reader.read_chat_title(hwnd)
    snap = reader.read_chat(hwnd)
    worker.close()

    print("会话 %r" % title)
    print("容器 rect = %s" % (snap.container_rect,))
    print("可见消息 %d 条" % len(snap.messages))
    print("at_bottom() = %s   bottom_gap = %s" % (snap.at_bottom(), snap.bottom_gap()))
    print()
    if snap.messages:
        print("可见消息的 y 范围:")
        for i, m in enumerate(snap.messages):
            print("   %2d. y=%d..%d  (高 %d)  %r"
                  % (i, m.rect[1], m.rect[3], m.rect[3] - m.rect[1], (m.text or "")[:26]))
    print()

    # ---------------------------------------------------------- 1. 找滚动条
    print("=" * 74)
    print("1. 支持 RangeValue 的元素（滚动条候选）")
    print("=" * 74)
    rvs = [r for r in rows if r[4]]
    if rvs:
        for cls, aid, name, rect, rng in rvs:
            print("  cls=%-30s aid=%-22s name=%-14r %s"
                  % (cls[:28], aid[:20], name[:12], rng))
            print("      rect=%s" % (rect,))
    else:
        print("  ✗ 一个都没有 —— 滚动条没暴露 RangeValuePattern")

    # ---------------------------------------------------------- 2. 类名含 Scroll
    print("\n" + "=" * 74)
    print("2. 类名/ID 里带 Scroll 的元素")
    print("=" * 74)
    bars = [r for r in rows
            if "scroll" in r[0].lower() or "scroll" in r[1].lower()]
    if bars:
        for cls, aid, name, rect, rng in bars:
            print("  cls=%-30s aid=%-22s name=%r rect=%s %s"
                  % (cls[:28], aid[:20], name[:12], rect, rng))
    else:
        print("  ✗ 没有带 Scroll 字样的元素")

    # ---------------------------------------------------------- 3. 消息列表附近
    print("\n" + "=" * 74)
    print("3. aid 以 %r 开头的元素" % id_msglist)
    print("=" * 74)
    for cls, aid, name, rect, rng in rows:
        if aid.startswith(id_msglist):
            print("  cls=%-30s name=%-10r rect=%s %s"
                  % (cls[:28], name[:10], rect, rng))

    # ---------------------------------------------------------- 4. 结论
    print("\n" + "=" * 74)
    print("4. 结论")
    print("=" * 74)
    if rvs:
        print("  ✓ 有滚动条可读 -> 可以用 value==maximum 判断到底，")
        print("    这个判据与「消息高度」完全无关，能绕开用户指出的那个漏洞。")
    else:
        print("  ✗ 没有可读的滚动条 -> 得换别的判据，例如：")
        print("    a) 用「容器底部是否还有内容」而不是「最后一条的底部」；")
        print("    b) 结合库里的消息：可见的最后一条若已在库中且不是库尾，说明可能是翻历史；")
        print("    c) 记住上一次的可见消息集合，看有没有「上方插入」")

    return 0


if __name__ == "__main__":
    sys.exit(main())
