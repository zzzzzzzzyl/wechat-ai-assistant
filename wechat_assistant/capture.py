# -*- coding: utf-8 -*-
"""把微信窗口内容抓成位图 —— 默认用不受遮挡影响的 PrintWindow。

为什么不能简单用屏幕截图
------------------------
`sender.py` 判断「这条消息是自己发的还是对方发的」靠的是**气泡的左右位置**，
必须先拿到消息行的像素。最初实现用 `PIL.ImageGrab.grab(bbox)` 抓屏幕，
而屏幕截图有致命弱点：**窗口被别的窗口盖住就截到别人的内容**。

实测（2026-09-24，用户真实环境）：按下快捷键时聊天区常常同时被浏览器
（`Chrome_RenderWidgetHostHWND`）和本项目自己的悬浮窗（`TkChild`）盖住，
`SenderResolver` 只好整体放弃 -> 所有消息 `is_self=None`
-> 「同一个人连发的几句话合并成一个回复目标块」这条逻辑完全失效
（表现为「回复目标块」永远只有 1 条）。

`PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` 让**目标窗口自己**把内容画进
位图：不受遮挡影响，也不需要把微信临时置顶去打扰用户。

实测对照（同一时刻，微信处于非前台且被遮挡）：

    flags=2  PW_RENDERFULLCONTENT  非空白；消息左占比 0.01(自己) / 1.00(对方)
    flags=1  PW_CLIENTONLY         整幅空白，不可用
    flags=0  默认                   整幅全黑，不可用

坐标系
------
`GetWindowRect()` 与 UIA 给出的 rect 都是**屏幕坐标**（本进程设为每显示器 DPI
感知时为物理像素）。PrintWindow 位图的原点却是**窗口左上角**，
所以裁剪前必须减去窗口原点 —— 见 `to_window_coords()`，两种取图方式共用。
"""

from typing import Optional, Tuple

import ctypes

import win32gui
import win32ui

PW_RENDERFULLCONTENT = 0x00000002
PW_CLIENTONLY = 0x00000001


def to_window_coords(rect, wrect) -> Optional[Tuple[int, int, int, int]]:
    """屏幕坐标 rect -> PrintWindow 位图内的坐标。

    rect   : (left, top, right, bottom) 屏幕坐标
    wrect  : 窗口的屏幕坐标 rect（`win32gui.GetWindowRect(hwnd)`）
    """
    if not rect or not wrect:
        return None
    ox, oy = wrect[0], wrect[1]
    return (rect[0] - ox, rect[1] - oy, rect[2] - ox, rect[3] - oy)


def capture_window(hwnd: int, flags: int = PW_RENDERFULLCONTENT):
    """用 PrintWindow 抓整个窗口，返回 PIL.Image（失败返回 None）。

    只读取窗口内容，不改变窗口状态（不置顶、不激活、不移动）。
    """
    from PIL import Image

    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0 or w > 20000 or h > 20000:
        return None

    hwnd_dc = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        try:
            bmp.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bmp)
            # 用 ctypes 直接调 user32：本机 pywin32 的 win32gui.PrintWindow
            # 成功时返回 None 而不是 1，写成 `if not ok: return None` 会把
            # **成功**的抓图误判成失败（实测踩过：同样的窗口，ctypes 调返回 1、
            # win32gui 调返回 None，结果被判成「PrintWindow 不可用」）。
            # 因此这里不看返回值，是否真的抓到内容交给 is_blank() 判断。
            ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), flags)
            info = bmp.GetInfo()
            bits = bmp.GetBitmapBits(True)
            img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                                   bits, "raw", "BGRX", 0, 1)
            # frombuffer 是只读视图，复制出来避免底层 buffer 被回收后失效
            return img.copy()
        finally:
            try:
                win32gui.DeleteObject(bmp.GetHandle())
            except Exception:
                pass
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
    except Exception:
        return None
    finally:
        if hwnd_dc is not None:
            try:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass


def is_blank(img, threshold: int = 8) -> bool:
    """整幅图几乎是同一个颜色 -> PrintWindow 什么都没画出来。"""
    if img is None:
        return True
    try:
        small = img.resize((64, 64)).convert("L")
        px = list(small.getdata())
        return (max(px) - min(px)) < threshold
    except Exception:
        return True


def crop_window_region(img, wrect, rect):
    """从窗口位图里裁出屏幕坐标 rect 对应的区域；越界自动收敛，无效返回 None。"""
    if img is None:
        return None
    box = to_window_coords(rect, wrect)
    if box is None:
        return None
    x1 = max(0, box[0])
    y1 = max(0, box[1])
    x2 = min(img.width, box[2])
    y2 = min(img.height, box[3])
    if x2 - x1 < 1 or y2 - y1 < 1:
        return None
    return img.crop((x1, y1, x2, y2))
