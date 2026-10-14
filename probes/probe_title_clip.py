# -*- coding: utf-8 -*-
"""看标题栏那条带**左边到底从哪开始切** —— 放大存图 + 对比几种左边距。

    python probes/probe_title_clip.py

起因（用户 2026-10-11）：「公众号会被识别成众号，缺少一个字」——
也就是标题栏里**第一个字被裁掉了一半**。

判据：把同一条标题栏用**几个不同的左边距**各裁一次，放大后并排存成一张图：
    · 如果"现在用的左边界"那张里第一个字缺笔画，而"往左多给"那张是完整的，
      就是**左边切多了**
    · 再把每个版本各跑一次 OCR，看**哪一个读出了完整标题** —— 这是最硬的判据

⚠️ 纯只读：走 PrintWindow，不碰微信状态。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import dpi as _dpi  # noqa: E402

_dpi.enable()

from PIL import Image, ImageDraw  # noqa: E402

from wechat_assistant import capture  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.ocr_reader import OcrReader, chat_rect, detect_chat_left  # noqa: E402
from wechat_assistant.reader import list_windows, pick_window  # noqa: E402

#: 试这些**左边距**（物理像素，往左多给多少）
MARGINS = (0, 3, 6, 10, 16, 24)


def main():
    cfg = load_config()
    win = pick_window(list_windows(cfg), cfg)
    if win is None:
        print("挑不出微信主窗口")
        return 1
    img = capture.capture_window(win["hwnd"])
    if img is None or capture.is_blank(img):
        print("**PrintWindow 抓到空白** —— 先把微信恢复出来再跑")
        return 1
    W, H = img.width, img.height

    rd = OcrReader(cfg)
    rd.grab = lambda _h: img
    rd._engine().load()
    rd._engine().warmup()

    scale = rd._scale()
    x1, _y1, x2, _y2 = chat_rect((0, 0, W, H), scale, cfg, (W, H))
    top = int(round(float(cfg.get("ocr_read.chat_top", 70)) * scale))
    print("窗口 %dx%d  配置 chat_left=%d（%.0f 逻辑）  标题栏高 %d"
          % (W, H, x1, x1 / scale, top))

    print("\n== 每种左边距读出来的标题 ==")
    strips = []
    for m in MARGINS:
        left = max(0, x1 - m)
        strip = img.crop((left, 0, x2, top))
        blocks = rd._recognize(strip)
        # 和产品一样：取最靠左的那一块
        got = ""
        if blocks:
            b = min(blocks, key=lambda z: z.x1)
            got = (b.text or "").strip()
        print("   左边距 %2d（裁自 x=%4d）-> %r %s"
              % (m, left, got, "  ← 现在用的" if m == 0 else ""))
        strips.append((m, left, strip))

    # ---- 并排放大存图（每张上面标出左边距）
    zoom = 3
    pad = 6
    tw = max(s.width for _m, _l, s in strips) * zoom
    th = top * zoom
    canvas = Image.new("RGB",
                       (tw + pad * 2, (th + pad * 2 + 26) * len(strips)),
                       (24, 24, 24))
    d = ImageDraw.Draw(canvas)
    for i, (m, left, s) in enumerate(strips):
        big = s.resize((s.width * zoom, s.height * zoom), Image.NEAREST)
        y = i * (th + pad * 2 + 26) + 22
        canvas.paste(big, (pad, y))
        # 标出「左边距 0」那一条的左边界，便于比对
        if m == 0:
            d.line([(pad, y), (pad, y + th)], fill=(255, 0, 0), width=2)
        d.text((pad, y - 18), "left_margin=%d  (crop x=%d)  zoom=%dx"
               % (m, left, zoom), fill=(230, 230, 230))

    outdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
    os.makedirs(outdir, exist_ok=True)
    p = os.path.join(outdir, "title_clip_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    canvas.save(p)
    print("\n存了放大对比图: %s" % p)
    print("   （每一行是一种左边距；红线是**现在**的左边界；")
    print("     第一个字在红线左边还有笔画 = 现在被切掉了）")

    # ---- 顺带把会话列表右边那一小条也看一眼：往左多给会不会切进会话列表的文字
    print("\n== 往左多给会不会吃进会话列表的字（这决定能不能随便加左边距）==")
    a = strip = None
    for m in (0, 6, 16):
        left = max(0, x1 - m)
        col = img.crop((left, top, left + 30, min(H, top + 400)))
        blocks = rd._recognize(col)
        txt = " / ".join((b.text or "") for b in blocks[:3])
        print("   左边距 %2d：最左 30px 里认出的字 = %r" % (m, txt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
