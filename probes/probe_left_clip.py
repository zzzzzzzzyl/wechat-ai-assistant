# -*- coding: utf-8 -*-
"""看「聊天区左边到底切到哪」—— 存图 + 打印每块 OCR 文本的 x 起点。

    python probes/probe_left_clip.py

起因（用户 2026-10-11）：「左边有被截到，有些左边第一个字没有识别」。

判据用**两块证据**，不靠猜：
  1. **存图**（`out/left_clip_<时间>.png`）—— 人眼（或看图能力）直接看左边
     有没有把字切掉。⚠️ 裁剪区要画一条参考线，否则"看着像切了"没法确认。
  2. **块 x 起点**：任何一块的 `x1` **贴着 0** 就是强证据 ——
     文字被裁掉了才会紧贴裁剪区左边缘。正常情况左边那侧的头像/留白
     会占掉几十像素，正文不可能落在 x=0。
     ⚠️ 反过来也要看：**最左边那个块是头像还是字**。头像是方块、没文字。

⚠️ 纯只读：走 `PrintWindow`，不碰微信状态。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import dpi as _dpi  # noqa: E402

_dpi.enable()

import numpy as np  # noqa: E402
from PIL import ImageDraw  # noqa: E402

from wechat_assistant import capture  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.ocr_reader import (  # noqa: E402
    OcrReader, bubble_boxes, chat_rect, detect_chat_left)
from wechat_assistant.reader import list_windows, pick_window  # noqa: E402


def main():
    cfg = load_config()
    win = pick_window(list_windows(cfg), cfg)
    if win is None:
        print("挑不出微信主窗口")
        return 1
    hwnd = win["hwnd"]
    img = capture.capture_window(hwnd)
    if img is None or capture.is_blank(img):
        print("**PrintWindow 抓到空白** —— 先把微信恢复出来再跑")
        return 1
    W, H = img.width, img.height
    print("微信窗口 %dx%d" % (W, H))

    rd = OcrReader(cfg)
    rd.grab = lambda _h: img
    scale = rd._scale()
    cfg_left = chat_rect((0, 0, W, H), scale, cfg, (W, H))[0]
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    bg = rd._bg(a, cfg_left, W, int(round(float(cfg.get("ocr_read.chat_top", 70)) * scale)))
    det = detect_chat_left(a, bg)

    print("\n== 左边界 ==")
    print("   配置 chat_left = %d 物理（%.0f 逻辑）" % (cfg_left, cfg_left / scale))
    print("   自动检测       = %s 物理%s"
          % (det, "" if det is None else "（%.0f 逻辑）" % (det / scale)))
    print("   候选顺序       = %s" % list(rd._geometry_candidates(a, bg, cfg_left)))

    # 真的读一屏（走产品路径），拿到实际用的裁剪框
    rd._engine().load()
    rd._engine().warmup()
    snap = rd.read_chat(hwnd)
    if snap is None:
        print("read_chat 返回 None")
        return 1
    crop = rd.last_crop
    print("   实际裁剪框     = %s  = %dx%d"
          % (crop, crop[2] - crop[0], crop[3] - crop[1]))

    # ---- 证据 2：每块文字贴在裁剪区哪里
    print("\n== 认出来的块（按 x 排序，只看最左边 8 块）==")
    area = img.crop(crop)
    blocks = rd._recognize(area)
    for b in sorted(blocks, key=lambda z: z.x1)[:8]:
        flag = ""
        if b.x1 <= 1:
            flag = "   ← ⚠️ 紧贴左边缘（文字被切了才会这样）"
        elif b.x1 <= 6:
            flag = "   ← 很靠左，看看是不是被切了"
        print("   x1=%4d x2=%4d y=%4d..%-4d %r%s"
              % (b.x1, b.x2, b.y1, b.y2, (b.text or "")[:22], flag))

    # 气泡盒也看一眼：左边那侧的气泡盒 x1 是多少
    boxes = bubble_boxes(np.asarray(area.convert("RGB")).astype(np.int16), bg)
    print("\n== 气泡盒（只列左边的几个）==")
    mid = area.width / 2.0
    for bx in sorted(boxes, key=lambda z: z[0])[:6]:
        side = "我" if (area.width - bx[2]) < bx[0] else "对方"
        print("   x1=%4d x2=%4d w=%4d  离左%3d 离右%3d  -> %s"
              % (bx[0], bx[2], bx[2] - bx[0], bx[0], area.width - bx[2], side))

    # ---- 证据 1：把裁剪区存下来，并在左边画一条参考线便于比对
    outdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
    os.makedirs(outdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    p1 = os.path.join(outdir, "left_clip_%s.png" % stamp)

    # 版本 A：原样的裁剪区
    area.save(p1)
    # 版本 B：往左多给 80px（如果多给之后左边多出来的是**字**，说明现在切掉了）
    wide = img.crop((max(0, crop[0] - 80), crop[1], crop[2], crop[3]))
    p2 = os.path.join(outdir, "left_clip_wide_%s.png" % stamp)
    d = ImageDraw.Draw(wide)
    d.line([(80, 0), (80, wide.height)], fill=(255, 0, 0), width=3)
    wide.save(p2)

    print("\n== 存了两张图 ==")
    print("   A 现在用的裁剪区 : %s" % p1)
    print("   B 往左多给 80px  : %s" % p2)
    print("      （B 里那条**红线**就是现在裁剪区的左边界；")
    print("        红线**左边**如果还有字，就是被切掉的那部分）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
