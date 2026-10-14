# -*- coding: utf-8 -*-
"""对比实验：同一个界面，UIA 读到什么 vs OCR 看到什么。

要回答的疑问：
    「群聊界面上明明显示着发送者昵称，为什么说 UIA 读不到？」

核心概念：**「画在屏幕上」不等于「暴露给 UIA」**。
    - 渲染层：微信把昵称画进了自绘 canvas -> 人眼能看到
    - 无障碍层：没有生成对应的 UIA 元素 -> 程序读不到
这两个是相互独立的事情。

做法：
    1. 用 UIA 把当前窗口所有元素的 Name 抓出来；
    2. 把窗口临时置顶，截图，用 OCR 识别画面上的所有文字；
    3. 逐条比对：OCR 看到的文字里，哪些在 UIA 的 Name 里根本不存在。
       —— 这些就是「显示了但 UIA 不暴露」的内容。

只读：UIA 查询 + 截图 + OCR，不发消息、不点击、不输入。

用法：python probes/compare_uia_ocr.py
"""

import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from PIL import ImageGrab
import win32api
import win32con
import win32gui

from wechat_assistant.config import load_config
from wechat_assistant.reader import WeChatReader
from wechat_assistant.uia_worker import UiaWorker

SCOPE_DESCENDANTS = 4


def collect_uia(cfg, worker, hwnd):
    """抓取窗口内所有元素的 Name / ClassName / AutomationId / rect。"""
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


def ocr_image(path, scale=2.0):
    from rapidocr_onnxruntime import RapidOCR
    from PIL import Image
    import numpy as np

    img = Image.open(path)
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), 3)
    engine = RapidOCR()
    res, _ = engine(np.asarray(img.convert("RGB")))
    items = []
    if res:
        for box, text, score in res:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            items.append({
                "text": text,
                "x": int(min(xs) / scale),
                "y": int(min(ys) / scale),
                "score": float(score),
            })
    return items


def norm(s):
    return "".join(ch for ch in (s or "") if not ch.isspace())


def main():
    cfg = load_config()
    log = lambda m: None
    worker = UiaWorker(timeout=20.0, logger=log)
    reader = WeChatReader(cfg, worker, logger=log)

    win = reader.find_window()
    if not win:
        print("!! 未找到微信窗口")
        return
    hwnd = win["hwnd"]

    print("=" * 84)
    print("对比实验：UIA 读到什么 vs OCR 看到什么")
    print("=" * 84)

    # ---------- 1. UIA 能读到什么
    elems = collect_uia(cfg, worker, hwnd)
    uia_names = [e["name"] for e in elems if e["name"].strip()]
    print("\n【1】UIA 能读到的带名字元素：共 %d 个（窗口内总元素 %d 个）"
          % (len(uia_names), len(elems)))
    for n in uia_names:
        print("    %r" % n[:60])

    # ---------- 2. 截图 + OCR
    print("\n【2】正在临时置顶微信窗口以便截图…")
    topmost = False
    shotdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
    os.makedirs(shotdir, exist_ok=True)
    shot = os.path.join(shotdir, "compare_uia_ocr.png")
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        topmost = True
        time.sleep(1.0)

        r = win32gui.GetWindowRect(hwnd)
        sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
        bbox = (max(0, r[0]), max(0, r[1]), min(sw, r[2]), min(sh, r[3]))
        ImageGrab.grab(bbox=bbox, all_screens=True).save(shot)
        print("    截图已保存: %s  bbox=%s" % (shot, bbox))

        items = ocr_image(shot, scale=2.0)
    finally:
        if topmost:
            for _ in range(3):
                try:
                    win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
                    break
                except Exception:
                    time.sleep(0.2)
            print("    [已恢复窗口层级]")

    print("\n【3】OCR 从画面上识别到的文字：共 %d 条" % len(items))

    # ---------- 3. 比对
    joined = " || ".join(norm(n) for n in uia_names)
    only_ocr = []
    for it in items:
        t = norm(it["text"])
        if not t:
            continue
        if t in joined:
            continue
        # 再放宽一点：UIA 的 name 里包含这段文字也算能读到
        if any(t in norm(n) for n in uia_names):
            continue
        only_ocr.append(it)

    print("\n【4】★关键结果★ 画面上有、但 UIA 完全读不到的文字：%d 条"
          % len(only_ocr))
    print("    （这些就是「微信画出来了，但没暴露给无障碍接口」的内容）")
    print("    %-6s %-6s %s" % ("x", "y", "文字"))
    print("    " + "-" * 70)
    for it in sorted(only_ocr, key=lambda d: (d["y"] // 14, d["x"])):
        print("    %-6d %-6d %r" % (it["x"], it["y"], it["text"][:56]))

    print("\n【5】结论")
    if only_ocr:
        print("    UIA 能读到的只是窗口骨架（按钮/标签/会话名/标题）。")
        print("    聊天区里画出来的这些文字，UIA 的树里**根本没有对应元素**：")
        for it in sorted(only_ocr, key=lambda d: (d["y"] // 14, d["x"]))[:8]:
            print("      - %r" % it["text"][:50])
    else:
        print("    OCR 识别到的文字 UIA 也能读到（本次没发现差异）")

    worker.close()


if __name__ == "__main__":
    main()
