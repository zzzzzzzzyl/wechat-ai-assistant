# -*- coding: utf-8 -*-
"""滚动位置检测测试：确认「在底部」和「翻历史中」能正确区分。

这个判据很容易写反（gap 是负数才代表翻历史），所以用测试锁住。

用法：python tests/test_scroll.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.reader import ChatSnapshot, MessageItem

PASS, FAIL = 0, 0

# 消息容器的上下边界，统一用这个
TOP, BOTTOM = 200, 800


def make_snap(last_bottom):
    """造一个快照：容器底部固定 800，最后一条消息的底部由参数决定。"""
    msgs = [
        MessageItem(text="第一条", cls="mmui::ChatTextItemView", rect=(0, 116, 500, 200)),
        MessageItem(text="中间", cls="mmui::ChatTextItemView", rect=(0, 200, 500, 284)),
        MessageItem(text="最后一条", cls="mmui::ChatTextItemView", rect=(0, last_bottom - 84, 500, last_bottom)),
    ]
    return ChatSnapshot(title="测试", is_group=False, messages=msgs,
                        container_rect=(0, TOP, 500, BOTTOM))


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def main():
    print("=" * 72)
    print("滚动位置检测测试")
    print("=" * 72)
    print("容器底部 = %d，判据 gap = 容器底部 - 最后一条底部\n" % BOTTOM)

    print("1. 在底部")
    s = make_snap(BOTTOM)                     # gap = 0
    check("gap=0 -> 在底部", s.at_bottom() is True, "gap=%d" % s.bottom_gap())
    s = make_snap(BOTTOM - 5)                 # gap = 5，轻微误差
    check("gap=+5 -> 在底部", s.at_bottom() is True, "gap=%d" % s.bottom_gap())

    print("\n2. 消息不满一屏（最后一条下面本来就是空的）")
    s = make_snap(400)                        # gap = 400
    check("gap=+400 -> 在底部", s.at_bottom() is True, "gap=%d" % s.bottom_gap())

    print("\n3. 往上翻历史（最后一条被推到底部以下，gap 为负）")
    for d in (30, 50, 84, 200):
        s = make_snap(BOTTOM + d)             # gap = -d
        check("gap=-%d -> 翻历史中" % d, s.at_bottom() is False, "gap=%d" % s.bottom_gap())

    print("\n4. 边界：容差必须很小（原来给 20 太大，有系统性漏洞）")
    s = make_snap(BOTTOM + 3)                 # gap = -3
    check("gap=-3 -> 仍在底部（4 的容差内）", s.at_bottom() is True, "gap=%d" % s.bottom_gap())
    s = make_snap(BOTTOM + 10)                # gap = -10
    check("gap=-10 -> 翻历史中（容差只有 4，不再算在底部）",
          s.at_bottom() is False, "gap=%d" % s.bottom_gap())
    s = make_snap(BOTTOM + 25)                # gap = -25
    check("gap=-25 -> 翻历史中", s.at_bottom() is False, "gap=%d" % s.bottom_gap())

    print("\n4b. 用户指出的对齐陷阱：滚动距离正好是消息高度时 gap 会归零")
    # 列表是虚拟化的：最后一条滚出渲染区后就从 UIA 树里消失，
    # 「最后一条」换成上一条。设往上滚了 S、那一条高 h：
    #     滚出前 gap = -S     滚出后 gap = h - S      <- 在 S = h 处归零
    # 单聊消息高度都是 84，于是 S 落在 [84, 84+tolerance] 会被误判成「在底部」。
    # 容差 4 时盲区只有 4px；容差 20 时是 20px（每 84px 就有四分之一踩雷）。
    s = make_snap(BOTTOM)                     # 换成上一条后，它的底部正好贴容器底部
    check("换成上一条后 gap 恰好为 0（判据的天然盲区，无法靠 gap 分辨）",
          s.bottom_gap() == 0 and s.at_bottom() is True)
    check("但容差 4 让盲区只剩 4px：再往下 5px 就判得出翻历史",
          make_snap(BOTTOM + 5).at_bottom() is False, "gap=-5")
    from wechat_assistant.reader import BOTTOM_TOLERANCE
    check("容差是个很小的常数（不是随手写的大数）",
          BOTTOM_TOLERANCE <= 5, "BOTTOM_TOLERANCE=%d" % BOTTOM_TOLERANCE)

    print("\n5. 异常输入不该崩")
    check("空消息列表 -> 当作在底部",
          ChatSnapshot(container_rect=(0, TOP, 500, BOTTOM)).at_bottom() is True)
    check("没有容器 -> 当作在底部",
          ChatSnapshot(messages=[MessageItem("x", "c")]).at_bottom() is True)
    s = ChatSnapshot(messages=[MessageItem("x", "c")], container_rect=(0, TOP, 500, BOTTOM))
    check("消息没有 rect -> 当作在底部", s.at_bottom() is True)

    print("\n6. 反向验证：别写成 gap <= tolerance")
    s = make_snap(BOTTOM + 50)
    check("gap=-50 不能被判为在底部", s.at_bottom() is False,
          "这一条就是当初写反时漏掉的 case")

    print("\n" + "=" * 72)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
