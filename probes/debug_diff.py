# -*- coding: utf-8 -*-
"""调试：连续两次读取当前聊天，找出消息列表到底哪里不一致。

用来排查「没有新消息却报新消息」的误报问题。

用法：python probes/debug_diff.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.config import load_config
from wechat_assistant.reader import WeChatReader
from wechat_assistant.uia_worker import UiaWorker


def main():
    cfg = load_config()
    worker = UiaWorker(timeout=20.0, logger=lambda m: print("  [worker] %s" % m))
    reader = WeChatReader(cfg, worker)

    win = reader.find_window()
    if not win:
        print("!! 找不到微信窗口")
        return
    hwnd = win["hwnd"]

    print("=" * 78)
    print("连续两次读取当前聊天，比较消息列表")
    print("=" * 78)

    snap1 = reader.read_chat(hwnd)
    print("\n--- 第 1 次 ---")
    print("标题: %r   群聊: %s   消息数: %d"
          % (snap1.title, snap1.is_group, len(snap1.messages)))
    print("容器 rect: %s" % (snap1.container_rect,))
    for i, m in enumerate(snap1.messages):
        print("  %2d. [%-10s] rect=%-28s text=%r"
              % (i, m.cls.replace("mmui::", ""), m.rect, m.text))

    time.sleep(2.0)

    snap2 = reader.read_chat(hwnd)
    print("\n--- 第 2 次（2 秒后）---")
    print("标题: %r   群聊: %s   消息数: %d"
          % (snap2.title, snap2.is_group, len(snap2.messages)))
    for i, m in enumerate(snap2.messages):
        print("  %2d. [%-10s] rect=%-28s text=%r"
              % (i, m.cls.replace("mmui::", ""), m.rect, m.text))

    t1 = [(m.is_self, m.sender_name, m.text) for m in snap1.messages]
    t2 = [(m.is_self, m.sender_name, m.text) for m in snap2.messages]

    print("\n--- 差异分析 ---")
    if t1 == t2:
        print("两次完全相同 -> 不该报新消息")
    else:
        print("两次不同！")
        print("  条数: %d -> %d" % (len(t1), len(t2)))
        n = max(len(t1), len(t2))
        for i in range(n):
            a = t1[i] if i < len(t1) else "<无>"
            b = t2[i] if i < len(t2) else "<无>"
            if a != b:
                print("  第 %d 项不同:" % i)
                print("     第1次: %r" % (a,))
                print("     第2次: %r" % (b,))

    # 模拟增量识别
    from wechat_assistant.watcher import find_new_messages

    new = find_new_messages(t1, t2)
    print("\nfind_new_messages 的结果: %r" % (new,))
    if new and t1 == t2:
        print("  !! 算法在两次相同时也返回了内容，这是 bug")

    worker.close()


if __name__ == "__main__":
    main()
