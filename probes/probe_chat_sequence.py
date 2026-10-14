# -*- coding: utf-8 -*-
"""把当前聊天区里**按竖直顺序**排列的元素全列出来，看时间分隔符有多稀疏。

要回答的问题：一屏 N 条消息里，到底有几个时间分隔符？它们出现在哪些位置？

判据：元素矩形落在消息容器 rect 内、且类名以 ItemView 结尾
      （时间分隔符是 mmui::ChatItemView，它**没有 automationId**，
       所以不能像 reader.read_chat 那样靠 aid 前缀筛）

纯只读。
"""

import ctypes
import datetime as dt
import re
import sys

sys.path.insert(0, ".")

from wechat_assistant.config import load_config          # noqa: E402
from wechat_assistant.reader import WeChatReader         # noqa: E402
from wechat_assistant.uia_worker import UiaWorker        # noqa: E402

# 微信的时间分隔样式：16:19 / 昨天 23:28 / 9月20日 / 星期三 / 2026年9月1日
TIME_RE = re.compile(
    r"^(\d{1,2}:\d{2}|昨天( \d{1,2}:\d{2})?|前天( \d{1,2}:\d{2})?|"
    r"星期[一二三四五六日天]|\d{1,2}月\d{1,2}日|"
    r"\d{4}年\d{1,2}月\d{1,2}日|(上午|下午|晚上|凌晨)\s*\d{1,2}:\d{2})$")


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    cfg = load_config()
    worker = UiaWorker(20.0)
    reader = WeChatReader(cfg, worker)

    win = reader.find_window()
    if not win:
        print("没找到微信窗口")
        return 1
    hwnd = win["hwnd"]
    id_msglist = cfg.get("wechat.node.message_list", "chat_message_list")

    def job(auto, uia_mod):
        el = auto.ControlFromHandle(hwnd).Element
        arr = el.FindAll(4, uia_mod.CreateTrueCondition())
        out = []
        for i in range(arr.Length):
            e = arr.GetElement(i)
            try:
                cls = e.CurrentClassName or ""
                aid = e.CurrentAutomationId or ""
                nm = e.CurrentName or ""
                r = e.CurrentBoundingRectangle
                rect = (r.left, r.top, r.right, r.bottom)
            except Exception:
                continue
            out.append((cls, aid, nm, rect))
        return out

    rows = worker.call(job)
    title = reader.read_chat_title(hwnd)
    snap = reader.read_chat(hwnd)
    worker.close()

    if snap is None or not snap.container_rect:
        print("读不到消息容器")
        return 1
    L, T, R, B = snap.container_rect

    print("=" * 78)
    print("聊天区元素竖直顺序  |  会话 %r" % title)
    print("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("容器 rect = %s" % (snap.container_rect,))
    print("=" * 78)

    # 落在容器内、且在竖直方向上与容器有交集的元素
    inside = []
    for cls, aid, nm, (l, t, r, b) in rows:
        if b <= T or t >= B:
            continue
        if not nm.strip():
            continue
        inside.append((t, cls, aid, nm, (l, t, r, b)))
    inside.sort()

    seps = 0
    msgs = 0
    for idx, (t, cls, aid, nm, rect) in enumerate(inside, 1):
        is_time = bool(TIME_RE.match(nm.strip()))
        tag = ""
        if is_time:
            seps += 1
            tag = "   <<< 时间分隔符"
        elif cls.endswith("ItemView"):
            msgs += 1
        else:
            tag = "   （非 ItemView）"
        print("  %2d. y=%-5d %-30s aid=%-6s %r%s"
              % (idx, t, cls[:30], (aid or "-")[:6], nm[:38], tag))

    print()
    print("小计：容器内元素 %d 个，其中消息 %d 条、时间分隔符 %d 个"
          % (len(inside), msgs, seps))
    if msgs:
        print("      => 平均每 %.1f 条消息才有 1 个分隔符"
              % (msgs / seps if seps else float("inf")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
