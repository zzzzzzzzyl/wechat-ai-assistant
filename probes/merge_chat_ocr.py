# -*- coding: utf-8 -*-
"""右侧聊天区融合实验：UIA 取正文 + OCR 取昵称，看能否重建「某人: 内容」。

为什么只测右侧聊天区（消息列表容器内）：
    左侧会话列表的预览虽然有「昵称: 内容」的前缀，但那只覆盖**最后一条**消息，
    它只适合用来判断「要不要提醒」，**不能**用来构建对话上下文。
    真正要读的是右侧聊天区，而这里的昵称 UIA 完全读不到（实测确认）。

做法：
    1. UIA 读消息列表容器 rect + 每条消息的正文与行 rect；
    2. 临时置顶窗口，按行截图；
    3. 逐行 OCR，行内**左上角**的短文本大概率是昵称，下方是正文；
    4. 融合输出 -> 看能否还原成「李四: 这是示例消息」这样。

只读：UIA 查询 + 截图 + OCR。不发消息、不点击、不输入。

用法：python probes/merge_chat_ocr.py
"""

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

import numpy as np
from PIL import Image, ImageGrab
import win32api
import win32con
import win32gui

from wechat_assistant.config import load_config
from wechat_assistant.reader import WeChatReader
from wechat_assistant.uia_worker import UiaWorker

_engine = None


def ocr(img):
    global _engine
    from rapidocr_onnxruntime import RapidOCR

    if _engine is None:
        _engine = RapidOCR()
    res, _ = _engine(np.asarray(img.convert("RGB")))
    out = []
    if res:
        for box, text, score in res:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.append({
                "text": text, "score": float(score),
                "x1": int(min(xs)), "y1": int(min(ys)),
                "x2": int(max(xs)), "y2": int(max(ys)),
            })
    return out


def norm(s):
    return "".join(ch for ch in (s or "") if not ch.isspace())


def looks_like_noise(t: str) -> bool:
    """过滤明显不是昵称的 OCR 噪声（纯数字、版本号形态、单字符符号等）。"""
    import re

    t = (t or "").strip()
    if not t:
        return True
    if re.fullmatch(r"[\d\.\s\-:：/\\]+", t):        # 32.0.101.7085 / 01:48
        return True
    if len(norm(t)) <= 1 and not t.isalnum():          # 单个标点
        return True
    return False


def main():
    cfg = load_config()
    worker = UiaWorker(timeout=20.0, logger=lambda m: None)
    reader = WeChatReader(cfg, worker, logger=lambda m: None)

    win = reader.find_window()
    if not win:
        print("!! 未找到微信窗口")
        return
    hwnd = win["hwnd"]

    print("=" * 86)
    print("右侧聊天区融合实验：UIA 正文 + OCR 昵称")
    print("=" * 86)

    snap = reader.read_chat(hwnd)
    print("\n当前聊天: %r   群聊: %s   消息 %d 条"
          % (snap.title, snap.is_group, len(snap.messages)))
    if not snap.container_rect:
        print("!! 没找到消息列表容器")
        worker.close()
        return
    print("消息容器 rect: %s" % (snap.container_rect,))

    print("\n--- UIA 单独能给出的内容（注意：没有发送者）---")
    for i, m in enumerate(snap.messages):
        print("  %2d. [%-8s] %r" % (i, m.kind, m.text[:56]))

    # 置顶窗口准备截图
    sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    outdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "merge_shots")
    os.makedirs(outdir, exist_ok=True)
    ts = time.strftime("%H%M%S")

    topmost = False
    rows = []
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

        print("\n--- 逐行截图 + OCR ---")
        crect = snap.container_rect
        for i, m in enumerate(snap.messages):
            if not m.rect:
                continue
            x1, y1, x2, y2 = m.rect
            # 关键：截图区域必须裁到消息容器内。
            # 虚拟列表滚动后消息项的 rect 可能超出容器（实测遇到），
            # 不裁剪就会把标题栏的群描述、或左侧会话列表的内容一起截进来，
            # 被 OCR 误当成「昵称」。
            gx1 = max(0, x1, crect[0])
            gy1 = max(0, y1, crect[1])
            gx2 = min(sw, x2, crect[2])
            gy2 = min(sh, y2, crect[3])
            if gx2 - gx1 < 30 or gy2 - gy1 < 12:
                print("  %2d. 行不在可视区内，跳过 rect=%s" % (i, m.rect))
                continue
            img = ImageGrab.grab(bbox=(gx1, gy1, gx2, gy2), all_screens=True)
            img.save(os.path.join(outdir, "%s_msg%02d.png" % (ts, i)))
            big = img.resize((int(img.width * 3), int(img.height * 3)), 3)
            items = ocr(big)
            for it in items:
                for k in ("x1", "y1", "x2", "y2"):
                    it[k] = int(it[k] / 3)
            items.sort(key=lambda d: (d["y1"] // 10, d["x1"]))
            rows.append((i, m, items, (gx2 - gx1, gy2 - gy1)))
    finally:
        if topmost:
            for _ in range(3):
                try:
                    win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
                    break
                except Exception:
                    time.sleep(0.2)
            print("  [已恢复窗口层级]")

    # ---------- 融合分析
    print("\n" + "=" * 86)
    print("融合结果：每行 = OCR 行内元素 + UIA 正文")
    print("=" * 86)
    print("%-3s %-6s %-22s %-30s %s" % ("#", "行高", "OCR 左上角(推测昵称)", "UIA 正文", "正文靠左/右"))
    print("-" * 86)

    rebuilt = []
    for i, m, items, (w, h) in rows:
        uia_txt = m.text
        # 昵称候选：在行内上半部分、且不等于 UIA 正文的文本
        nickname = ""
        for it in items:
            if it["y1"] > h * 0.55:
                continue
            t = it["text"]
            if norm(t) == norm(uia_txt):
                continue
            if len(norm(t)) > 16:
                continue
            if looks_like_noise(t):
                continue
            nickname = t.strip()
            break
        # 正文左右：OCR 里与 UIA 正文匹配的那条的水平位置
        side = "?"
        for it in items:
            if norm(it["text"]) == norm(uia_txt):
                side = "左" if (it["x1"] + it["x2"]) / 2 < w / 2 else "右"
                break
        print("%-3d %-6d %-22s %-30s %s"
              % (i, h, repr(nickname)[:22], repr(uia_txt)[:30], side))
        rebuilt.append((nickname, uia_txt, side))

    print("\n--- 重建出的对话 ---")
    for nickname, txt, side in rebuilt:
        who = nickname if nickname else ("我" if side == "右" else "?")
        print("  %s: %s" % (who, txt))

    print("\n--- 结论 ---")
    with_nick = sum(1 for n, _, _ in rebuilt if n)
    print("  带昵称的行: %d / %d" % (with_nick, len(rebuilt)))
    print("  UIA 单独只能给出正文（没有发送者）；")
    print("  OCR 补上昵称后可以重建出「某人: 内容」；左右位置可用于辅助判断是否自己发的。")

    worker.close()


if __name__ == "__main__":
    main()
