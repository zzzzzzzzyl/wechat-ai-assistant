# -*- coding: utf-8 -*-
"""验证：PrintWindow 抓回来的位图里，**图片/表情包的像素到底有没有**。

为什么必须先验这个
------------------
`sender.py` 用 `capture_window(hwnd)` 判发送者是**验证过的**，但它读的是
气泡的纯色矩形。照片/表情包很可能走另一条渲染路径（GPU 合成、独立 surface），
PrintWindow 抓这类区域有时是空白的。**这一条不成立，图片 OCR 整套都白搭。**

做法：读当前聊天区 → 找出「图片 / 动画表情」的条目的 rect →
从窗口位图裁下来 → 量颜色方差 / 不同颜色数 → 存 PNG 到 out/image_capture/。

判据：
    空白     -> 不同颜色数 <= 3，或 is_blank() 为真
    有内容   -> 不同颜色数几十以上

纯只读。不改微信、不点鼠标、不写微信数据。
"""

import ctypes
import os
import sys

sys.path.insert(0, ".")

from wechat_assistant.capture import capture_window, crop_window_region, is_blank  # noqa: E402
from wechat_assistant.config import load_config                                   # noqa: E402
from wechat_assistant.reader import WeChatReader                                  # noqa: E402
from wechat_assistant.uia_worker import UiaWorker                                 # noqa: E402

import win32gui                                                                   # noqa: E402

PLACEHOLDERS = ("图片", "动画表情", "视频", "文件")
OUT_DIR = os.path.join("out", "image_capture")


def stats(img):
    """返回 (宽, 高, 不同颜色数, 灰度标准差)。"""
    if img is None:
        return None
    small = img.convert("RGB")
    colors = small.getcolors(maxcolors=200000)
    ncolor = len(colors) if colors is not None else 200000
    g = small.convert("L")
    px = list(g.getdata())
    mean = sum(px) / len(px)
    var = sum((p - mean) ** 2 for p in px) / len(px)
    return (small.width, small.height, ncolor, var ** 0.5)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    cfg = load_config()
    worker = UiaWorker(20.0)
    reader = WeChatReader(cfg, worker)

    win = reader.find_window()
    if not win:
        print("没找到微信窗口")
        return 1
    hwnd = win["hwnd"]

    snap = reader.read_chat(hwnd)
    if snap is None:
        print("读聊天失败")
        return 1

    print("当前会话：%s" % (snap.title or "?"))
    picks = [m for m in snap.messages
             if (m.text or "").strip() in PLACEHOLDERS and m.rect]
    print("这一屏里的占位符消息：%d 条" % len(picks))
    for m in snap.messages:
        t = (m.text or "").strip()
        if t in PLACEHOLDERS:
            print("   %-8s rect=%s cls=%s" % (t, m.rect, m.cls))

    if not picks:
        print("\n这一屏没有图片/表情，换一个会话或滚到有图的地方再跑。")
        return 2

    wrect = win32gui.GetWindowRect(hwnd)
    print("\n窗口 rect = %s" % (wrect,))
    img = capture_window(hwnd)
    if img is None:
        print("capture_window 返回 None —— PrintWindow 直接失败")
        return 3
    print("窗口位图 %dx%d" % (img.width, img.height))
    print("整窗 is_blank = %s" % is_blank(img))

    os.makedirs(OUT_DIR, exist_ok=True)
    print()
    print("%-10s %-26s %-12s %8s %8s" % ("类型", "rect(屏幕)", "裁剪后尺寸", "颜色数", "灰度σ"))
    ok = 0
    for i, m in enumerate(picks, 1):
        crop = crop_window_region(img, wrect, m.rect)
        st = stats(crop)
        if st is None:
            print("%-10s %-26s 裁剪失败（越界）" % (m.text, str(m.rect)))
            continue
        blank = is_blank(crop) or st[2] <= 3
        print("%-10s %-26s %-12s %8d %8.1f  %s"
              % (m.text, str(m.rect), "%dx%d" % (st[0], st[1]), st[2], st[3],
                 "空白!!" if blank else "有内容"))
        if not blank:
            ok += 1
        path = os.path.join(OUT_DIR, "%02d_%s.png" % (i, m.text))
        crop.save(path)

    print()
    print("存到 %s/" % OUT_DIR)
    if ok == 0:
        print(">>> 结论：PrintWindow 抓不到图片像素。图片 OCR 这条路走不通，"
              "要另想办法（例如只对当前可见屏用屏幕截图，但要处理遮挡）。")
    else:
        print(">>> 结论：%d/%d 张图有真实像素，PrintWindow 可以用于图片 OCR。"
              % (ok, len(picks)))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
