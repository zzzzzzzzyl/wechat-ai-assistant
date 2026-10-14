# -*- coding: utf-8 -*-
"""
截图可靠性对比 v2（带 OCR 自动判定）。

要回答的问题：
    PrintWindow 到底能不能在「微信被其他窗口遮住」时截到完整的微信内容？
    （这决定 OCR 方案能否在后台无人值守地运行）

做法：
    在微信窗口被本终端盖住的真实情况下，分别用
      A. PrintWindow(flag=2) + GetWindowRect 尺寸
      B. PrintWindow(flag=2) + DWM 扩展边框尺寸
      C. 屏幕截取 + GetWindowRect
    各截一张，再用 RapidOCR 识别，统计图里出现了多少「微信关键词」和多少
    「终端关键词」。微信关键词多且终端关键词少 = 该方式抗遮挡有效。

用法：python test_capture.py
"""

import ctypes
import datetime as dt
import os
import sys

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
import win32process
import win32ui
from PIL import Image, ImageGrab

TARGET_PROC = "weixin.exe"

WX_KEYS = ["张三", "发送", "搜索", "通讯录", "收藏", "发现", "聊天记录", "会话"]
TERM_KEYS = ["class=", "ListItemControl", "python", "Claude", "probes", "识别", "虚拟列表", "score"]


def find_wechat_hwnds():
    found = []

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
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found


def dwm_rect(hwnd):
    try:
        import ctypes.wintypes as wt

        r = wt.RECT()
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) == 0:
            return (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    return None


def printwindow(hwnd, path, flag=2):
    rect = win32gui.GetWindowRect(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    try:
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), flag)
        info = bmp.GetInfo()
        img = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1
        )
        img.save(path)
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        return ok, img
    finally:
        win32gui.ReleaseDC(hwnd, hwnd_dc)


_engine = None


def ocr_judge(path):
    global _engine
    from rapidocr_onnxruntime import RapidOCR

    if _engine is None:
        _engine = RapidOCR()
    img = Image.open(path)
    img = img.resize((img.width * 2, img.height * 2), 3)
    tmp = os.path.join(os.path.dirname(path), "_tmp_scaled.png")
    img.save(tmp)
    res, _ = _engine(tmp)
    os.remove(tmp)
    texts = [t for _, t, _ in res] if res else []
    joined = " ".join(texts)
    wx = [k for k in WX_KEYS if k in joined]
    term = [k for k in TERM_KEYS if k in joined]
    return wx, term, texts


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    os.makedirs(outdir, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    hwnds = find_wechat_hwnds()
    if not hwnds:
        print("!! 未找到微信主窗口")
        return
    hwnd = hwnds[0]
    gr = win32gui.GetWindowRect(hwnd)
    dr = dwm_rect(hwnd)
    print("微信 hwnd = %s" % hex(hwnd))
    print("GetWindowRect  = %s  尺寸 %dx%d" % (gr, gr[2] - gr[0], gr[3] - gr[1]))
    print("DWM 扩展边框    = %s  %s" % (dr, "尺寸 %dx%d" % (dr[2] - dr[0], dr[3] - dr[1]) if dr else ""))
    print("是否被遮挡: 主窗口 = %s" % hex(win32gui.GetForegroundWindow()))

    cases = []
    p = os.path.join(outdir, "v2_pw_%s.png" % ts)
    try:
        ok, _ = printwindow(hwnd, p, 2)
        cases.append(("A.PrintWindow(2)", p, "ret=%s" % ok))
    except Exception as e:
        print("A 失败: %s" % e)

    p = os.path.join(outdir, "v2_screen_%s.png" % ts)
    try:
        ImageGrab.grab(bbox=gr, all_screens=True).save(p)
        cases.append(("C.屏幕截取", p, ""))
    except Exception as e:
        print("C 失败: %s" % e)

    print("")
    print("=" * 78)
    print("%-22s %-28s %-28s" % ("方式", "命中的微信关键词", "命中的终端关键词"))
    print("=" * 78)
    for name, path, note in cases:
        try:
            wx, term, texts = ocr_judge(path)
            print("%-22s %-28s %-28s %s" % (name, ",".join(wx)[:26], ",".join(term)[:26], note))
        except Exception as e:
            print("%-22s OCR 失败: %s" % (name, e))

    print("")
    print("判读：微信关键词多 + 终端关键词少 => 该方式抗遮挡有效，可用于后台截图。")


if __name__ == "__main__":
    main()
