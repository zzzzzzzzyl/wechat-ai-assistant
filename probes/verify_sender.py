# -*- coding: utf-8 -*-
"""核对发送者判定：把每一行的截图、UIA 正文、OCR 明细全部摊开。

背景：
    sender.py 会给群聊消息判定发送者，但 OCR 昵称可能不稳
    （同一行在不同裁剪方式下曾识别出 '李四' 和 'ThikPad T14pGen1' 两种结果）。
    这个脚本把判定依据全部打印出来并保存行截图，便于人工核对到底哪个对。

用法：
    python probes/verify_sender.py            # 当前会话
    python probes/verify_sender.py --topmost  # 窗口被遮挡时临时置顶
"""

import argparse
import ctypes
import os
import sys
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

import win32api
import win32con
import win32gui
from PIL import ImageGrab

from wechat_assistant.config import load_config
from wechat_assistant.ocr import OcrEngine
from wechat_assistant.reader import WeChatReader
from wechat_assistant.sender import SenderResolver, looks_like_noise
from wechat_assistant.uia_worker import UiaWorker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topmost", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    logs = []
    log = lambda m: (print("  " + m), logs.append(m))
    worker = UiaWorker(timeout=20.0, logger=lambda m: None)
    reader = WeChatReader(cfg, worker, logger=lambda m: None)
    ocr = OcrEngine(logger=lambda m: None)
    ocr.load()
    resolver = SenderResolver(cfg, ocr, logger=lambda m: None)

    win = reader.find_window()
    if not win:
        print("!! 未找到微信窗口")
        return
    hwnd, pid = win["hwnd"], win["pid"]
    snap = reader.read_chat(hwnd)

    print("=" * 96)
    print("发送者判定核对")
    print("=" * 96)
    print("当前聊天: %r   群聊: %s   消息 %d 条"
          % (snap.title, snap.is_group, len(snap.messages)))
    print("消息容器: %s" % (snap.container_rect,))
    print("行高阈值(nickname_min_row_height): %s" % resolver.min_row_height)

    sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    shotdir = os.path.join(ROOT, "out", "sender_shots")
    os.makedirs(shotdir, exist_ok=True)
    ts = time.strftime("%H%M%S")

    topmost = False
    try:
        if args.topmost or resolver._is_obstructed(hwnd, pid, snap.container_rect):
            print("\n窗口被遮挡，临时置顶以便截图…")
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
            topmost = True
            time.sleep(0.5)

        crect = snap.container_rect
        print("\n" + "-" * 96)
        for i, m in enumerate(snap.messages):
            if not m.rect:
                continue
            x1, y1, x2, y2 = m.rect
            h = y2 - y1
            gx1 = max(0, x1, crect[0]); gy1 = max(0, y1, crect[1])
            gx2 = min(sw, x2, crect[2]); gy2 = min(sh, y2, crect[3])
            need = h >= resolver.min_row_height

            print("消息 %d  UIA正文=%r" % (i, m.text[:50]))
            print("    行高=%d  是否做OCR=%s  rect=%s" % (h, "是" if need else "否(判为自己)", m.rect))
            if not need or gx2 - gx1 < 30 or gy2 - gy1 < 10:
                continue

            # 保存两种裁剪：整行 + 上部，便于对比
            full = ImageGrab.grab(bbox=(gx1, gy1, gx2, gy2), all_screens=True)
            p_full = os.path.join(shotdir, "%s_%02d_full.png" % (ts, i))
            full.save(p_full)

            crop_h = max(12, int((gy2 - gy1) * resolver.top_nick_ratio))
            top = ImageGrab.grab(bbox=(gx1, gy1, gx2, gy1 + crop_h), all_screens=True)
            p_top = os.path.join(shotdir, "%s_%02d_top.png" % (ts, i))
            top.save(p_top)

            # 直接看 OCR 明细（不做筛选），看清到底识别出了什么
            for tag, im in (("上部", top), ("整行", full)):
                items = ocr.recognize(im, min_score=0.3)
                print("    [%s OCR] %d 个文本块:" % (tag, len(items)))
                for it in items:
                    mark = ""
                    if "".join(it["text"].split()) == "".join(m.text.split()):
                        mark = " <- 与正文相同"
                    elif looks_like_noise(it["text"]):
                        mark = " <- 噪声"
                    print("        x=%4d y=%3d score=%.2f %r%s"
                          % (it["x1"], it["y1"], it["score"], it["text"], mark))

            nick = resolver._pick_nickname(top, m.text)
            print("    => 采纳的昵称: %r" % nick)
            print("    截图: %s" % os.path.basename(p_top))
            print("")
    finally:
        if topmost:
            for _ in range(3):
                try:
                    win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
                    break
                except Exception:
                    time.sleep(0.2)
            print("[已恢复窗口层级]")

    print("\n行截图目录: %s" % shotdir)
    print("请打开对照：上部截图里最上面那行小字就是昵称所在位置。")
    worker.close()


if __name__ == "__main__":
    main()
