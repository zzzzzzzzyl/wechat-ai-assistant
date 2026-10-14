# -*- coding: utf-8 -*-
"""
群聊发送者识别：UIA 取行 rect + UIA 取消息正文 + OCR 补昵称。

问题背景（已实测确认）：
    群聊里 UIA 完全不暴露昵称 —— 消息项子元素为 0，行内无带名元素，
    全窗口 Text 控件只有群名/成员数/群描述/发送按钮。
    但群聊界面上昵称是画出来的，所以只能靠 OCR。

方案：
    1. UIA 拿到每条消息的「正文」和「整行 rect」；
    2. 把微信窗口临时置顶，按行截图；
    3. 对该行 OCR，得到若干「文本 + 坐标」；
    4. 与 UIA 正文比对：
       - 若 OCR 结果里有 UIA 没有的文本 -> 那多半就是昵称；
       - 结合水平位置判断左右（谁发的）。

只读：仅 UIA 查询 + 截图 + OCR。不发消息、不点击、不输入。

用法：
    python probe_group_sender.py
    python probe_group_sender.py --scale 3      # OCR 放大倍数
"""

import argparse
import ctypes
import datetime as dt
import os
import sys
import threading
import time

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
import win32process

TARGET_PROC = "weixin.exe"
SCOPE_DESCENDANTS = 4

_engine = None


def find_wechat_hwnd():
    best = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if "QWindowIcon" not in win32gui.GetClassName(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            try:
                path = win32process.GetModuleFileNameEx(h, 0)
            finally:
                win32api.CloseHandle(h)
            if os.path.basename(path).lower() == TARGET_PROC:
                best.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return best[0] if best else None


def read_uia(hwnd, out):
    import comtypes

    comtypes.CoInitialize()
    import uiautomation as auto
    from uiautomation import uiautomation as core

    with auto.UIAutomationInitializerInThread(debug=False):
        uia = core._AutomationClient.instance().IUIAutomation
        el = auto.ControlFromHandle(hwnd).Element
        arr = el.FindAll(SCOPE_DESCENDANTS, uia.CreateTrueCondition())
        title, ml, msgs = "", None, []
        for i in range(arr.Length):
            e = arr.GetElement(i)
            cls = e.CurrentClassName or ""
            aid = e.CurrentAutomationId or ""
            if "current_chat_name_label" in aid:
                title = e.CurrentName or ""
            elif aid == "chat_message_list":
                r = e.CurrentBoundingRectangle
                ml = (r.left, r.top, r.right, r.bottom)
            elif aid.startswith("chat_message_list") and cls.endswith("ItemView"):
                r = e.CurrentBoundingRectangle
                msgs.append({
                    "name": e.CurrentName or "",
                    "cls": cls,
                    "rect": (r.left, r.top, r.right, r.bottom),
                })
        out["title"] = title
        out["container"] = ml
        out["msgs"] = msgs


def _safe_read(hwnd, out):
    try:
        read_uia(hwnd, out)
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)


def ocr(img):
    global _engine
    from rapidocr_onnxruntime import RapidOCR

    if _engine is None:
        _engine = RapidOCR()
    arr = np.asarray(img.convert("RGB"))
    res, _ = _engine(arr)
    if not res:
        return []
    items = []
    for box, text, score in res:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append({
            "text": text,
            "score": float(score),
            "x1": int(min(xs)), "y1": int(min(ys)),
            "x2": int(max(xs)), "y2": int(max(ys)),
        })
    return items


def norm(s):
    """去掉空白与常见 OCR 噪声后比较。"""
    return "".join(ch for ch in s if not ch.isspace())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=3.0)
    ap.add_argument("--no-topmost", action="store_true")
    args = ap.parse_args()

    buf = []

    def rec(s=""):
        print(s, flush=True)
        buf.append(str(s))

    hwnd = find_wechat_hwnd()
    if not hwnd:
        rec("!! 未找到微信主窗口")
        return
    rec("=" * 90)
    rec("群聊发送者识别：UIA 正文 + OCR 昵称")
    rec("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    rec("=" * 90)

    out = {}
    t = threading.Thread(target=_safe_read, args=(hwnd, out), daemon=True)
    t.start()
    t.join(30)
    if t.is_alive() or out.get("error"):
        rec("!! UIA 读取失败: %s" % (out.get("error") or "超时"))
        return

    msgs = out.get("msgs", [])
    c = out.get("container")
    rec("当前聊天: %r" % out.get("title"))
    rec("消息容器: %s" % (c,))
    rec("消息条数: %d" % len(msgs))
    if not msgs:
        rec("!! 无可见消息")
        return

    sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    shotdir = os.path.join(outdir, "group_shots")
    os.makedirs(shotdir, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    topmost = False
    try:
        if not args.no_topmost:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            topmost = True
            time.sleep(1.0)

        rec("")
        rec("-" * 90)
        rec("逐行分析")
        rec("-" * 90)

        for i, m in enumerate(msgs, 1):
            x1, y1, x2, y2 = m["rect"]
            gx1, gy1 = max(0, x1), max(0, y1)
            gx2, gy2 = min(sw, x2), min(sh, y2)
            if gx2 - gx1 < 20 or gy2 - gy1 < 10:
                rec("消息 %d: 行越界，跳过" % i)
                continue

            img = ImageGrab.grab(bbox=(gx1, gy1, gx2, gy2), all_screens=True)
            shot = os.path.join(shotdir, "%s_msg%02d.png" % (ts, i))
            img.save(shot)

            big = img.resize((int(img.width * args.scale), int(img.height * args.scale)), 3)
            items = ocr(big)
            for it in items:
                it["x1"] = int(it["x1"] / args.scale)
                it["y1"] = int(it["y1"] / args.scale)
                it["x2"] = int(it["x2"] / args.scale)
                it["y2"] = int(it["y2"] / args.scale)
            items.sort(key=lambda d: (d["y1"] // 10, d["x1"]))

            uia_text = m["name"]
            w = gx2 - gx1
            rec("")
            rec("消息 %d  UIA正文=%r  [%s]  行高=%d" % (
                i, uia_text, m["cls"].replace("mmui::", ""), gy2 - gy1))
            rec("   行 rect: %s" % (m["rect"],))
            if not items:
                rec("   OCR: 未识别到文本")
                continue
            rec("   OCR 结果:")
            extra = []
            for it in items:
                side = "左" if (it["x1"] + it["x2"]) / 2 < w / 2 else "右"
                same = norm(it["text"]) == norm(uia_text)
                tag = "=正文" if same else ("?可能是昵称" if side == "左" else "其它")
                rec("      x=%-4d y=%-4d %-4s score=%.2f  %-24r  %s" % (
                    it["x1"], it["y1"], side, it["score"], it["text"][:24], tag))
                if not same and side == "左":
                    extra.append(it["text"])
            if extra:
                rec("   => 该行多出的文本（候选昵称）: %s" % extra)

    finally:
        if topmost:
            for _ in range(3):
                try:
                    win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
                    break
                except Exception:
                    time.sleep(0.2)
            rec("")
            rec("[已恢复] 微信窗口层级还原为非置顶")

    path = os.path.join(outdir, "group_sender_%s.txt" % ts)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(buf))
    rec("")
    rec("[已保存] %s" % path)
    rec("[行截图] %s" % shotdir)


if __name__ == "__main__":
    main()
