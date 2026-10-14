# -*- coding: utf-8 -*-
"""标题栏左边到底该从哪切 —— **自动检测有没有生效**，还是悄悄退回了配置值。

    python probes/diag_chat_left.py

起因（用户 2026-10-11）：
    先报「**公众号** → **众号**」（掉第一个字），加了 `left_margin: 6` 好了；
    又报「**广财**信息学院23级级群(499) → **财**信息学院23级级群(499)」
    —— **又是掉第一个字**。

⚠️ 加边距只是**治标**：`_crop_left` 是在算出来的左边界上再往左让几个像素。
   如果**左边界本身就偏右**（检测失败 → 悄悄退回配置里的 `chat_left`），
   那让 6 个逻辑像素根本不够 —— 而这正是要查的。

判据：
  ① `detect_chat_left()` 到底返回了多少（还是 None = 认不出来）
  ② `chat_rect()` 最后用了哪个值 —— **检测值**还是**配置兜底值**
  ③ 逐列的底色占比长什么样（真边界是"从 0 跳到接近 1"那一列）

⚠️ 纯只读：走 PrintWindow，不碰微信状态。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import dpi as _dpi  # noqa: E402

_dpi.enable()

from wechat_assistant import capture  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.ocr_reader import (  # noqa: E402
    OcrReader, chat_rect, detect_chat_left)
from wechat_assistant.reader import list_windows, pick_window  # noqa: E402


def main():
    cfg = load_config()
    win = pick_window(list_windows(cfg), cfg)
    if not win:
        print("没找到微信窗口")
        return 1
    img = capture.capture_window(win["hwnd"])
    if img is None:
        print("抓图失败")
        return 1

    rd = OcrReader(cfg)
    scale = rd._scale()
    W, H = img.width, img.height
    cfg_left = int(float(cfg.get("ocr_read.chat_left", 310)) * scale)

    print("窗口 %dx%d   缩放 %.2f" % (W, H, scale))
    print("配置里的 chat_left = %d 物理（%s 逻辑）"
          % (cfg_left, cfg.get("ocr_read.chat_left")))
    print("auto_geometry = %r" % cfg.get("ocr_read.auto_geometry", True))
    print("left_margin = %r 逻辑（= %d 物理）"
          % (cfg.get("ocr_read.left_margin", 6),
             int(round(float(cfg.get("ocr_read.left_margin", 6)) * scale))))

    print("\n== ① 检测本身 ==")
    try:
        det = detect_chat_left(img, scale, cfg)
    except Exception as e:
        det = None
        print("   detect_chat_left 抛异常: %s: %s" % (type(e).__name__, e))
    if det is None:
        print("   detect_chat_left -> **None（认不出来）**")
    else:
        print("   detect_chat_left -> %d" % det)

    print("\n== ② chat_rect 最后用了哪个 ==")
    x1, y1, x2, y2 = chat_rect((0, 0, W, H), scale, cfg, (W, H))
    print("   chat_rect 左边界 = %d" % x1)
    if det is not None and abs(x1 - det) <= 2:
        print("   -> 用的是**检测值** ✅（自动检测生效）")
    elif abs(x1 - cfg_left) <= 2:
        print("   -> 用的是**配置兜底值** ⚠️（检测没生效，或者被判定为不可信）")
        if det is not None:
            print("      但检测明明给了 %d —— 差了 %d 像素！" % (det, x1 - det))
    else:
        print("   -> 既不是检测值也不是配置值（%d vs 检测 %s / 配置 %d）"
              % (x1, det, cfg_left))

    print("\n== ③ 逐列底色占比（真边界 = 从 ~0 跳到 ~1 那一列）==")
    try:
        from wechat_assistant.ocr_reader import _bg_colors  # type: ignore
        have = True
    except Exception:
        have = False
    if have:
        print("   （有 _bg_colors，下面自己算）")
    # 自己算：取一条竖直样带，数每列有多少像素接近底色
    y0, y1s, y2s = max(0, y1), min(H, max(y2, 1)), None
    from collections import Counter
    mid0, mid1 = int(H * 0.25), int(H * 0.75)
    band = img.crop((0, mid0, min(W, 700), mid1)).convert("RGB")
    px = band.load()
    bw, bh = band.size
    # 用**聊天气泡那一侧**的底色众数（取 x=520..560 这一小段）
    sample = []
    for x in range(min(bw - 1, 520), min(bw, 560)):
        for y in range(0, bh, 3):
            sample.append(px[x, y])
    bg = Counter(sample).most_common(1)[0][0] if sample else (28, 28, 28)
    print("   采样到的聊天区底色 = %s" % (bg,))
    print("   x     占比     （1.00 = 整列都是聊天区底色）")
    for x in range(400, min(bw, 500), 2):
        n = sum(1 for y in range(0, bh, 2)
                if max(abs(px[x, y][i] - bg[i]) for i in range(3)) <= 8)
        frac = n / float(len(range(0, bh, 2)))
        mark = ""
        if x == x1:
            mark = "   <== chat_rect 用的左边界"
        if det is not None and x == det:
            mark += "   <== 检测值"
        print("   %-5d %.2f%s" % (x, frac, mark))
    return 0


if __name__ == "__main__":
    sys.exit(main())
