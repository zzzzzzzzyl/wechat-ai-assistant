# -*- coding: utf-8 -*-
"""探测：能否用 PrintWindow 抓微信窗口内容（绕过遮挡问题）。

为什么需要它
------------
`sender.py` 的发送者判定用的是「屏幕截图 + 左右像素分布」。
屏幕截图的致命弱点是**被遮挡就截错**：
实测用户按热键时，聊天区常常同时被浏览器和本项目自己的悬浮窗盖住，
`sender.resolve()` 于是整体放弃（返回 0 条），所有消息 `is_self=None`，
「同一人连发的几句话合并成一个回复目标块」这条逻辑随之失效。

替代方案 `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` 让**目标窗口自己**
把内容画进位图，不受遮挡影响，也不需要临时置顶去打扰用户。

本脚本只验证可行性：抓图 -> 落盘 PNG（人工看一眼）-> 对每条消息跑像素判定，
和已知结论（气泡靠左=别人 / 靠右=自己）对照。
只读：不改窗口状态、不置顶、不点击。
"""

import ctypes
import os
import sys

sys.path.insert(0, ".")

import win32gui
import win32ui  # noqa: E402


def print_window(hwnd, flags=2):
    """用 PrintWindow 抓窗口位图，返回 (user32 返回值, PIL.Image 或 None)。"""
    from PIL import Image

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return 0, None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    try:
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        try:
            bmp.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bmp)
            ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), flags)
            info = bmp.GetInfo()
            bits = bmp.GetBitmapBits(True)
            img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                                   bits, "raw", "BGRX", 0, 1)
        finally:
            win32gui.DeleteObject(bmp.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
    finally:
        win32gui.ReleaseDC(hwnd, hwnd_dc)
    return ok, img


def is_blank(img, threshold=8):
    """整幅图几乎是同一个颜色 -> 说明 PrintWindow 什么都没画出来。"""
    if img is None:
        return True
    small = img.resize((64, 64))
    px = list(small.getdata())
    lo = min(min(p) for p in px)
    hi = max(max(p) for p in px)
    return (hi - lo) < threshold


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    from wechat_assistant.config import load_config
    from wechat_assistant.reader import WeChatReader
    from wechat_assistant.sender import analyze_side
    from wechat_assistant.uia_worker import UiaWorker

    cfg = load_config()
    w = UiaWorker(20.0)
    r = WeChatReader(cfg, w)
    win = r.find_window()
    snap = r.read_chat(win["hwnd"])
    w.close()

    hwnd = win["hwnd"]
    wrect = win32gui.GetWindowRect(hwnd)
    crect = snap.container_rect
    print("窗口 rect = %s" % (wrect,))
    print("容器 rect = %s" % (crect,))
    print("会话 %r  消息 %d 条" % (snap.title, len(snap.messages)))
    print("遮挡情况：窗口当前是否前台 = %s" % (win32gui.GetForegroundWindow() == hwnd))
    print()

    outdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
    os.makedirs(outdir, exist_ok=True)

    results = {}
    for flags, name in ((2, "PW_RENDERFULLCONTENT(2)"), (1, "PW_CLIENTONLY(1)"), (0, "默认(0)")):
        ok, img = print_window(hwnd, flags)
        blank = is_blank(img)
        print("PrintWindow flags=%-22s 返回=%-3s 空白=%-5s 尺寸=%s"
              % (name, ok, blank, (img.size if img else None)))
        results[flags] = (ok, img, blank)
        if img is not None and not blank:
            path = os.path.join(outdir, "printwindow_flags%d.png" % flags)
            img.save(path)
            print("    已保存 %s" % path)

    print()
    print("=" * 74)
    print("对可用的抓图结果跑像素判定，看能否重现「左=别人 / 右=自己」")
    print("=" * 74)

    for flags in (2, 1, 0):
        ok, img, blank = results.get(flags, (0, None, True))
        if img is None or blank or not crect:
            print("\nflags=%d: 不可用，跳过" % flags)
            continue
        ox, oy = wrect[0], wrect[1]
        # 屏幕坐标 -> 窗口内容坐标
        box = (crect[0] - ox, crect[1] - oy, crect[2] - ox, crect[3] - oy)
        box = (max(0, box[0]), max(0, box[1]),
               min(img.width, box[2]), min(img.height, box[3]))
        print("\nflags=%d  裁剪框(窗口内坐标) = %s" % (flags, box))
        if box[2] - box[0] < 50 or box[3] - box[1] < 50:
            print("  裁剪框太小，说明窗口位图和屏幕 rect 对不上")
            continue
        for i, m in enumerate(snap.messages):
            # 消息 rect 也是屏幕坐标，同样换算
            mx1, my1, mx2, my2 = m.rect
            sub = img.crop((max(box[0], mx1 - ox), max(box[1], my1 - oy),
                            min(box[2], mx2 - ox), min(box[3], my2 - oy)))
            if sub.width < 30 or sub.height < 10:
                print("  %2d. %-24r 裁剪失败 %s" % (i, (m.text or "")[:22], sub.size))
                continue
            is_self, lr, gr = analyze_side(sub)
            print("  %2d. %-24r is_self=%-5s 左占比=%.2f 绿占比=%.3f"
                  % (i, (m.text or "")[:22], str(is_self), lr, gr))
