# -*- coding: utf-8 -*-
"""只读探测：聊天区里都有哪些控件类？时间分隔能不能读到？

为什么查这个
------------
用户问：「过了很久开新话题，历史对话会不会变成噪音？」

会的，而且当前实现有个明确的边界：`merge_with_store` 取的是**最近 20 条**，
是**按条数**切的，不是按话题切的。所以：

  · 新话题已经聊了 >= 20 条  -> 旧话题自然被挤出去，没问题
  · 新话题刚开始（3 条）      -> 剩下 17 条全是上一个话题的 -> 就是噪音

**天然的时间信号其实就在聊天里**：微信会插入时间分隔（`21:30` / `昨天` /
`9月20日`）。如果能读到它，就能在「隔了很久」的地方把历史截断。

但 `reader.py:316` 只收 `cls.endswith("ItemView")` 的元素，时间分隔即使存在
也会被跳过。本脚本把所有类名摊开，看它到底在不在 UIA 树里。

⚠️ 只读：不改窗口、不点击。
用法： python probes/probe_time_sep.py
"""

import ctypes
import re
import sys
from collections import Counter

sys.path.insert(0, ".")

from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.reader import WeChatReader  # noqa: E402
from wechat_assistant.uia_worker import UiaWorker  # noqa: E402

# 像时间的文本：21:30 / 昨天 / 9月20日 / 星期三 / 上午10:05
TIME_RE = re.compile(
    r"^(\d{1,2}:\d{2}|昨天|前天|星期[一二三四五六日天]|"
    r"\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月\d{1,2}日|"
    r"(上午|下午|晚上|凌晨)\s*\d{1,2}:\d{2})$")


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    cfg = load_config()
    worker = UiaWorker(20.0)
    reader = WeChatReader(cfg, worker)

    from comtypes.gen import UIAutomationClient as uia
    SCOPE_DESCENDANTS = 4

    win = reader.find_window()
    hwnd = win["hwnd"]
    id_msglist = cfg.get("wechat.node.message_list", "chat_message_list")

    def job(auto, uia_mod):
        el = auto.ControlFromHandle(hwnd).Element
        arr = el.FindAll(SCOPE_DESCENDANTS, uia_mod.CreateTrueCondition())
        rows = []
        for i in range(arr.Length):
            e = arr.GetElement(i)
            try:
                rows.append((
                    e.CurrentClassName or "",
                    e.CurrentAutomationId or "",
                    e.CurrentName or "",
                ))
            except Exception:
                continue
        return rows

    rows = worker.call(job)
    title = reader.read_chat_title(hwnd)      # 必须在 close 之前读
    worker.close()

    print("会话 %r，共 %d 个 UIA 元素\n" % (title, len(rows)))

    # ---------------------------------------------------------- 1. 类名分布
    print("=" * 76)
    print("1. 类名分布（只看聊天区相关的）")
    print("=" * 76)
    cnt = Counter(c for c, a, n in rows if c)
    for cls, n in cnt.most_common(20):
        mark = ""
        if cls.endswith("ItemView"):
            mark = "   <- 会被 read_chat 收走"
        print("  %-44s x%-4d%s" % (cls[:42], n, mark))

    # ---------------------------------------------------------- 2. 时间样文本
    print("\n" + "=" * 76)
    print("2. 有没有「长得像时间」的元素？")
    print("=" * 76)
    hits = [(c, a, n) for c, a, n in rows if n and TIME_RE.match(n.strip())]
    if hits:
        print("  找到 %d 个:" % len(hits))
        for c, a, n in hits[:20]:
            print("    cls=%-34s aid=%-26s name=%r" % (c[:32], a[:24], n))
        print("\n  => 时间分隔**在 UIA 树里**，可以用来判断「隔了很久」")
    else:
        print("  没找到形如 21:30 / 昨天 / 9月20日 的元素。")
        print("  => 时间分隔不在 UIA 树里（可能和正文画在同一张 canvas 上），")
        print("     想拿到只能走 OCR / PrintWindow 截图那条路。")

    # ---------------------------------------------------------- 3. 消息列表子元素
    print("\n" + "=" * 76)
    print("3. 消息列表容器下的元素（看时间分隔挂在哪里）")
    print("=" * 76)
    inside = [(c, a, n) for c, a, n in rows if a.startswith(id_msglist)]
    print("  automationId 以 %r 开头的元素共 %d 个" % (id_msglist, len(inside)))
    cc = Counter(c for c, a, n in inside)
    for cls, n in cc.most_common(12):
        print("    %-44s x%d" % (cls[:42], n))
    # 打印几个不是 ItemView 的
    others = [(c, a, n) for c, a, n in inside if not c.endswith("ItemView")]
    if others:
        print("\n  其中**不是** ItemView 的（这些就是被 read_chat 跳过的）：")
        for c, a, n in others[:20]:
            print("    cls=%-34s aid=%-22s name=%r" % (c[:32], a[:20], n[:30]))
    else:
        print("\n  消息列表下**只有** ItemView，没有别的元素。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
