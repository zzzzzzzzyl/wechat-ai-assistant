# -*- coding: utf-8 -*-
"""判断当前打开的会话是「群聊」还是「单聊」，并把所有判据列出来。

为什么要单独做这个：
    群聊和单聊的**数据获取方式完全不同**：
      - 单聊只需要区分「我 / 对方」两方 -> 靠气泡左右（像素分析）
      - 群聊需要知道「具体是谁」    -> 靠 OCR 读昵称
    所以第一步必须先准确判断会话类型。

实测得到的判据（微信 4.1.13.65）：
    1. 【最可靠】标题栏控件的类名
         群聊: mmui::ChatTitleBarChatRoomView
         单聊: mmui::ChatTitleBarChatSingleView
    2. 标题文本是否带成员数，群聊形如「群名(4)」，且成员数是独立元素
    3. 是否存在群描述文本
    4. 发送者判定结果是否符合群聊特征（行高 115/84 交替出现）

只读：仅 UIA 查询。

用法：python probes/detect_chat_type.py
"""

import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

import re

from wechat_assistant.config import load_config
from wechat_assistant.reader import WeChatReader
from wechat_assistant.uia_worker import UiaWorker

SCOPE_DESCENDANTS = 4
CLS_GROUP = "mmui::ChatTitleBarChatRoomView"
CLS_SINGLE = "mmui::ChatTitleBarChatSingleView"
CLS_TEXT = "mmui::XTextView"


def collect(worker, hwnd):
    def job(auto, uia):
        el = auto.ControlFromHandle(hwnd).Element
        arr = el.FindAll(SCOPE_DESCENDANTS, uia.CreateTrueCondition())
        out = []
        for i in range(arr.Length):
            e = arr.GetElement(i)
            try:
                r = e.CurrentBoundingRectangle
                rect = (r.left, r.top, r.right, r.bottom)
            except Exception:
                rect = None
            try:
                out.append({
                    "name": e.CurrentName or "",
                    "cls": e.CurrentClassName or "",
                    "id": e.CurrentAutomationId or "",
                    "rect": rect,
                })
            except Exception:
                pass
        return out

    return worker.call(job)


def main():
    cfg = load_config()
    worker = UiaWorker(timeout=20.0, logger=lambda m: None)
    reader = WeChatReader(cfg, worker, logger=lambda m: None)

    win = reader.find_window()
    if not win:
        print("!! 未找到微信窗口")
        return
    hwnd = win["hwnd"]

    snap = reader.read_chat(hwnd)
    elems = collect(worker, hwnd)

    print("=" * 78)
    print("会话类型判据检测")
    print("=" * 78)
    print("当前聊天标题: %r" % snap.title)
    print("消息条数    : %d" % len(snap.messages))
    print("")

    classes = {e["cls"] for e in elems}
    has_group_cls = CLS_GROUP in classes
    has_single_cls = CLS_SINGLE in classes

    # 标题栏区域的文本（y < 消息容器上边）
    top_y = snap.container_rect[1] if snap.container_rect else 600
    top_texts = [e for e in elems
                 if e["cls"] == CLS_TEXT and e["name"].strip()
                 and e["rect"] and e["rect"][1] < top_y]

    member_num = []
    for e in top_texts:
        m = re.fullmatch(r"\((\d+)\)", e["name"].strip())
        if m:
            member_num.append(int(m.group(1)))

    print("-" * 78)
    print("【判据 1】标题栏控件类名（最可靠）")
    print("-" * 78)
    print("  群聊类 %-38s 存在: %s" % (CLS_GROUP, "是" if has_group_cls else "否"))
    print("  单聊类 %-38s 存在: %s" % (CLS_SINGLE, "是" if has_single_cls else "否"))
    print("  窗口内所有 ChatTitleBar* 类: %s"
          % sorted(c for c in classes if "ChatTitleBar" in c))

    print("")
    print("-" * 78)
    print("【判据 2】标题栏文本 / 成员数")
    print("-" * 78)
    for e in top_texts:
        print("  %-30r rect=%s" % (e["name"][:30], e["rect"]))
    if member_num:
        print("  -> 检测到成员数: %s  => 群聊特征" % member_num)
    else:
        print("  -> 没有独立的成员数元素  => 单聊特征")

    print("")
    print("-" * 78)
    print("【判据 3】消息行高分布（群聊里别人消息会多一行昵称）")
    print("-" * 78)
    heights = []
    for m in snap.messages:
        if m.rect:
            heights.append(m.rect[3] - m.rect[1])
    print("  各行高度: %s" % heights)
    uniq = sorted(set(heights))
    print("  去重后 : %s" % uniq)

    print("")
    print("-" * 78)
    print("【结论】")
    print("-" * 78)
    if has_group_cls and not has_single_cls:
        verdict = "群聊"
        conf = "高"
    elif has_single_cls and not has_group_cls:
        verdict = "单聊"
        conf = "高"
    elif member_num:
        verdict = "群聊"
        conf = "中（靠成员数推断）"
    else:
        verdict = "无法判定"
        conf = "低"
    print("  当前会话类型: 【%s】  置信度: %s" % (verdict, conf))
    print("  reader.read_chat() 给出的 is_group = %s" % snap.is_group)

    print("")
    print("-" * 78)
    print("【两种会话的读取策略差异】")
    print("-" * 78)
    if verdict == "群聊":
        print("  群聊：需要知道「具体是谁」")
        print("    - 正文      : UIA（ChatTextItemView 的 Name）")
        print("    - 发送者昵称: UIA 读不到 -> OCR 读行内左上部")
        print("    - 是否自己发: 行高 115=有昵称(别人) / 84=无昵称(自己)")
    elif verdict == "单聊":
        print("  单聊：只需要区分「我 / 对方」两方")
        print("    - 正文      : UIA")
        print("    - 没有昵称概念，行高不携带发送者信息")
        print("    - 是否自己发: 按行截图做像素分析（内容像素靠左=对方 / 靠右=自己）")
    print("    两种会话的正文与行坐标读取方式完全一致。")

    worker.close()


if __name__ == "__main__":
    main()
