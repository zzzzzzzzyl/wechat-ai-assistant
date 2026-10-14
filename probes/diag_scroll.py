# -*- coding: utf-8 -*-
"""诊断：消息列表滚动位置 —— 验证「底部留白」能否区分「在底部」和「翻历史中」。

背景：
    UIA 只能读当前可见的消息。用户往上滚看历史时，可见区域的最后一条
    是**中间某条**消息，不是对话真正的最新一条。若拿它生成建议，
    就是在回复一条早就回过的旧消息。

假设（待验证）：
    滚到底部时，最后一条消息的底部 ≈ 消息容器底部（留白 0px）；
    往上滚之后下面还有没显示的消息，底部留白会明显变大。

用法：
    python probes/diag_scroll.py            # 采一次
    python probes/diag_scroll.py --watch 30 # 连续采 30 秒，边滚边看
"""

import ctypes
import sys
import time

import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from wechat_assistant.config import load_config
from wechat_assistant.reader import WeChatReader
from wechat_assistant.uia_worker import UiaWorker


def sample(reader, hwnd):
    snap = reader.read_chat(hwnd)
    c = snap.container_rect
    if not c or not snap.messages:
        return None
    last = snap.messages[-1]
    first = snap.messages[0]
    return {
        "title": snap.title,
        "n": len(snap.messages),
        "container": c,
        "bottom_gap": (c[3] - last.rect[3]) if last.rect else None,
        "top_gap": (first.rect[1] - c[1]) if first.rect else None,
        "at_bottom": snap.at_bottom(),
        "last_text": last.text[:24],
        "first_text": first.text[:24],
    }


def main():
    watch = 0
    if "--watch" in sys.argv:
        i = sys.argv.index("--watch")
        watch = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 20

    cfg = load_config()
    worker = UiaWorker(timeout=20.0, logger=lambda m: None)
    reader = WeChatReader(cfg, worker, logger=lambda m: None)
    win = reader.find_window()
    if not win:
        print("!! 未找到微信窗口")
        return
    hwnd = win["hwnd"]

    print("=" * 76)
    print("滚动位置诊断")
    print("=" * 76)

    if watch:
        print("连续采样 %d 秒。请在这段时间里把微信聊天**往上滚**，再滚回底部。\n" % watch)
        print("%-8s %-12s %-10s %-10s %-10s %s" % ("时间", "底部留白", "顶部留白", "消息数", "判定", "最后一条"))
        print("-" * 76)
        t0 = time.time()
        while time.time() - t0 < watch:
            s = sample(reader, hwnd)
            if s:
                print("%-8.1fs %-12s %-10s %-10d %-10s %r"
                      % (time.time() - t0, s["bottom_gap"], s["top_gap"], s["n"],
                         "在底部" if s["at_bottom"] else "翻历史中", s["last_text"]))
            time.sleep(1.0)
    else:
        s = sample(reader, hwnd)
        if not s:
            print("!! 读不到消息")
            return
        print("会话: %r   消息 %d 条" % (s["title"], s["n"]))
        print("消息容器: %s" % (s["container"],))
        print("第一条可见: %r   顶部留白 %s px" % (s["first_text"], s["top_gap"]))
        print("最后一条可见: %r  底部留白 %s px" % (s["last_text"], s["bottom_gap"]))
        print()
        print("判定: %s" % ("在底部（最后一条就是最新消息）" if s["at_bottom"]
                            else "翻历史中（最后一条不是最新消息，会跳过生成）"))

    worker.close()


if __name__ == "__main__":
    main()
