# -*- coding: utf-8 -*-
"""量「微信左栏的会话列表」到底能不能拿来当『按时间排』的依据。

要回答两个问题：
  ① `read_sessions()` 返回的**顺序**是不是就是微信里的顺序（＝按最近消息时间）？
  ② 它是不是**只返回渲染出来的那些**（虚拟列表会只给可见项）？
     如果只给可见项，那"按时间排"就不能只靠它 —— 得配合本地记录的时间。

只读，不碰微信任何状态。用法：python probes/probe_session_order.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from wechat_assistant.config import load_config
    from wechat_assistant.reader import WeChatReader
    from wechat_assistant.uia_worker import UiaWorker

    cfg = load_config(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.yaml"))
    reader = WeChatReader(cfg, UiaWorker())
    win = reader.find_window()
    if not win:
        print("没找到微信窗口（没开微信？）—— 这个探针需要在微信开着时跑")
        return 1
    print("微信窗口 hwnd = %s" % win["hwnd"])

    items = reader.read_sessions(win["hwnd"])
    print("\nread_sessions() 返回 %d 条（顺序 = 微信里的顺序）\n" % len(items))
    print("  %-3s %-4s %-26s %-10s %s" % ("#", "y", "会话名", "时间", "最后一条预览"))
    for i, it in enumerate(items):
        y = getattr(it, "rect", None)
        print("  %-3d %-4s %-26s %-10s %s"
              % (i, (y[1] if y else "-"), (it.raw_title or "")[:26],
                 it.time_text or "", (it.preview or "")[:22]))

    # 跟注册表对一下：注册表里有、但这次没读到的，就是"没渲染出来"的
    import io
    import json
    state = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "state", "state.json")
    try:
        reg = (json.load(io.open(state, encoding="utf-8")) or {}).get("sessions", {})
    except Exception as e:
        print("\n读注册表失败: %s" % e)
        return 0
    got = set(it.raw_title for it in items)
    missing = [k for k in reg if k not in got]
    print("\n注册表里有 %d 个会话，本次读到 %d 个" % (len(reg), len(got)))
    print("注册表里有、但这次没读到的 %d 个：" % len(missing))
    for k in missing[:30]:
        print("   · %s" % k)
    print("\n结论：")
    print("  · 如果 missing 很多 -> 微信的会话列表是**虚拟列表**，只给了渲染出来的那些，")
    print("    『按时间排』不能只靠它，得配合本地记录的时间。")
    print("  · 如果 missing 很少 -> 直接拿它的**顺序**当排序键就是最准的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
