# -*- coding: utf-8 -*-
"""对裁下来的图片消息跑 OCR —— 回答「这条路到底能不能用」。

顺带解决一个已经看到的问题：`ChatBubbleReferItemView` 的 rect 是**整行**
（含头像 + 群昵称 + 右边大片聊天区底色），不是图片的框。
所以这里做两件事：① 按 rect 直接 OCR；② 收紧到内容 bbox 再 OCR，对比。

用法：
    python probes/probe_image_ocr.py                # 现场从微信窗口取
    python probes/probe_image_ocr.py <png>          # 用已经存下来的图
"""

import ctypes
import sys
import time

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PLACEHOLDERS = ("图片", "动画表情", "视频", "文件")


def content_bbox(img, tol=18):
    """找出「不属于背景色」的像素范围。

    背景色取四条边里出现最多的那个颜色（聊天区底色 / 气泡底色）。
    返回 (x1, y1, x2, y2)，找不到返回 None。
    """
    import numpy as np
    a = np.asarray(img.convert("RGB")).astype(int)
    h, w, _ = a.shape
    edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    # 用中位数当背景（比众数快，且对少量前景不敏感）
    bg = np.median(edge, axis=0)
    diff = np.abs(a - bg).sum(axis=2)
    mask = diff > tol * 3
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def ocr_text(img):
    from wechat_assistant.ocr import get_engine
    eng = get_engine()
    t0 = time.perf_counter()
    res = eng.recognize(img)
    ms = (time.perf_counter() - t0) * 1000
    return res, ms


def run(img, tag):
    print("=" * 70)
    print("%s   尺寸 %dx%d" % (tag, img.width, img.height))
    box = content_bbox(img)
    if box:
        print("内容 bbox = %s   收紧后 %dx%d（原来的 %.0f%% 面积）"
              % (str(box), box[2] - box[0], box[3] - box[1],
                 100.0 * (box[2] - box[0]) * (box[3] - box[1]) / (img.width * img.height)))
    res, ms = ocr_text(img)
    print("OCR 用时 %.0f ms，识别出 %d 块：" % (ms, len(res)))
    for r in res:
        print("   %-6s (%.2f)  %s" % (str(r["x1"]) + "," + str(r["y1"]),
                                      r["score"], r["text"]))
    if box and (box[2] - box[0]) < img.width * 0.9:
        tight = img.crop(box)
        res2, ms2 = ocr_text(tight)
        print("-- 收紧后：%.0f ms，%d 块" % (ms2, len(res2)))
        for r in res2:
            print("   %-6s (%.2f)  %s" % (str(r["x1"]) + "," + str(r["y1"]),
                                          r["score"], r["text"]))
    return res


def main():
    import os

    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        from PIL import Image
        run(Image.open(sys.argv[1]).convert("RGB"), "文件: " + sys.argv[1])
        return 0

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    import win32gui
    from wechat_assistant.capture import capture_window, crop_window_region
    from wechat_assistant.config import load_config
    from wechat_assistant.reader import WeChatReader
    from wechat_assistant.uia_worker import UiaWorker

    cfg = load_config()
    reader = WeChatReader(cfg, UiaWorker(20.0))
    win = reader.find_window()
    if not win:
        print("没找到微信窗口")
        return 1
    hwnd = win["hwnd"]
    snap = reader.read_chat(hwnd)
    if snap is None:
        print("读聊天失败")
        return 1

    picks = [m for m in snap.messages
             if (m.text or "").strip() in PLACEHOLDERS and m.rect]
    print("会话：%s   占位符消息 %d 条" % (snap.title or "?", len(picks)))
    if not picks:
        print("这一屏没有图片，换一屏再跑")
        return 2

    wrect = win32gui.GetWindowRect(hwnd)
    img = capture_window(hwnd)
    if img is None:
        print("capture_window 失败")
        return 3

    for m in picks:
        crop = crop_window_region(img, wrect, m.rect)
        if crop is None:
            print("裁剪失败 %s" % (m.rect,))
            continue
        run(crop, "%s  rect=%s" % (m.text, m.rect))
    return 0


if __name__ == "__main__":
    sys.exit(main())
