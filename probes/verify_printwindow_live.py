# -*- coding: utf-8 -*-
"""验证：PrintWindow 抓到的聊天区，内容是**当前实时**的吗？

不依赖视觉的交叉验证思路
------------------------
UIA 读到的文本是精确可信的（已知结论），OCR 是从像素里认出来的。
把两组文本比对：

  - 若高度重合、**尤其最新一条也在** -> PrintWindow 抓到的是实时内容；
  - 若对不上 / 缺最新几条        -> 抓到的可能是窗口上次可见时的残留位图，
                                    那这条路就不能用。

顺带对比「屏幕截图」在同一时刻的结果，直观展示遮挡的破坏性。
只读：不置顶、不激活、不点击、不改任何状态。
"""

import ctypes
import sys

sys.path.insert(0, ".")

from wechat_assistant.capture import (capture_window, crop_window_region,  # noqa: E402
                                      is_blank, to_window_coords)
from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.reader import WeChatReader  # noqa: E402
from wechat_assistant.sender import analyze_side  # noqa: E402
from wechat_assistant.uia_worker import UiaWorker  # noqa: E402


def norm(s):
    """归一化：去掉所有空白和标点，只留文字，便于模糊比对。"""
    if not s:
        return ""
    drop = " \t\r\n，。！？、：；,.!?:;\"'“”‘’()（）[]【】-—_~"
    return "".join(ch for ch in s if ch not in drop)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    import win32gui

    cfg = load_config()
    w = UiaWorker(20.0)
    r = WeChatReader(cfg, w)
    win = r.find_window()
    snap = r.read_chat(win["hwnd"])
    w.close()

    hwnd = win["hwnd"]
    wrect = win32gui.GetWindowRect(hwnd)
    crect = snap.container_rect
    is_fg = win32gui.GetForegroundWindow() == hwnd

    print("会话 %r  群聊=%s  消息 %d 条" % (snap.title, snap.is_group, len(snap.messages)))
    print("窗口是否前台: %s   （非前台时屏幕截图会被别的窗口污染）" % is_fg)
    print("窗口 rect = %s" % (wrect,))
    print("容器 rect = %s" % (crect,))
    print()

    # ---------- UIA 文本（基准） ----------
    uia_texts = [m.text or "" for m in snap.messages]
    print("UIA 读到的 %d 条：" % len(uia_texts))
    for i, t in enumerate(uia_texts):
        print("  %2d. %r" % (i, t[:40]))
    print()

    # ---------- 取图 ----------
    pw_img = capture_window(hwnd, 2)
    print("PrintWindow(PW_RENDERFULLCONTENT): 空白=%s 尺寸=%s"
          % (is_blank(pw_img), (pw_img.size if pw_img else None)))

    screen_img = None
    try:
        from PIL import ImageGrab
        screen_img = ImageGrab.grab(bbox=tuple(crect), all_screens=True)
        print("屏幕截图: 尺寸=%s（内容可能已被遮挡污染）" % (screen_img.size,))
    except Exception as e:
        print("屏幕截图失败: %s: %s" % (type(e).__name__, e))
    print()

    if pw_img is None or is_blank(pw_img):
        print("PrintWindow 不可用，结论：这条路走不通")
        return

    # ---------- OCR 交叉验证 ----------
    from wechat_assistant.ocr import get_engine

    ocr = get_engine(cfg.get("ocr"))

    def ocr_text_of(img):
        """对 PIL 图做 OCR，返回拼接后的文本。OcrEngine.recognize 直接吃 PIL.Image。"""
        if img is None:
            return ""
        try:
            items = ocr.recognize(img)
        except Exception as e:
            print("  OCR 失败: %s: %s" % (type(e).__name__, e))
            return ""
        return "".join(str(d.get("text", "")) for d in items)

    crop_pw = crop_window_region(pw_img, wrect, crect)
    pw_text = ocr_text_of(crop_pw)
    screen_text = ocr_text_of(screen_img)

    print("=" * 74)
    print("交叉验证：OCR 文本 vs UIA 文本")
    print("=" * 74)
    print("PrintWindow OCR : %r" % pw_text[:400])
    print("屏幕截图  OCR : %r" % screen_text[:400])
    print()

    npw = norm(pw_text)
    nsur = norm(screen_text)
    hits_pw = [i for i, t in enumerate(uia_texts) if t and norm(t) and norm(t) in npw]
    hits_screen = [i for i, t in enumerate(uia_texts) if t and norm(t) and norm(t) in nsur]

    print("PrintWindow 命中 UIA 消息 %d/%d 条：%s" % (len(hits_pw), len(uia_texts), hits_pw))
    print("屏幕截图    命中 UIA 消息 %d/%d 条：%s" % (len(hits_screen), len(uia_texts), hits_screen))
    print()

    if uia_texts:
        last = norm(uia_texts[-1])
        print("最新一条 %r 是否出现在 PrintWindow 抓图里: %s"
              % (uia_texts[-1][:20], last in npw if last else "N/A"))
    print()

    if len(hits_pw) >= max(1, len(uia_texts) - 1):
        print("结论：PrintWindow 抓到的就是**当前实时内容**，且不受遮挡影响 -> 可用")
    elif len(hits_pw) > len(hits_screen):
        print("结论：PrintWindow 明显优于屏幕截图，但仍需复核（可能有延迟或裁剪偏移）")
    else:
        print("结论：PrintWindow 内容对不上，不能用")

    # ---------- 像素判定对照 ----------
    print()
    print("=" * 74)
    print("像素判定对照（左占比 ~0=自己 / ~1=对方）")
    print("=" * 74)
    for i, m in enumerate(snap.messages):
        pw_sub = crop_window_region(pw_img, wrect, m.rect)
        s_pw = analyze_side(pw_sub) if pw_sub is not None else (None, -1, -1)
        print("  %2d. %-22r PrintWindow: is_self=%-5s 左占比=%.2f"
              % (i, (m.text or "")[:20], str(s_pw[0]), s_pw[1]))


if __name__ == "__main__":
    main()
