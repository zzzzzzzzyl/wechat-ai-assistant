# -*- coding: utf-8 -*-
"""给**真的** `read_chat()` 打真实中间量（monkeypatch 打印，不改产品代码）。

    python probes\diag_read_trace.py <截图.png> [--scale 1.54]

为什么还要这个（`diag_read_steps.py` 复现的那份**和产品漂了**）：
    · 探针手动算 `_crop_left(430)` 得到 421，产品实际用了 424；
    · 探针里的块 y 是屏坐标，产品里是裁剪坐标 —— 差 108。
    于是"某条消息是不是被丢了"这种判断**必须看产品自己的数**。

做法：把 `rows_from_text` / `bubble_boxes` / `_row_has_ink` 包一层，
把入参和返回值打出来，再调真 `rd.read_chat()`。

⚠️ 纯只读一张 PNG；`time` 等都不碰。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402

from wechat_assistant import ocr_reader as R  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--scale", type=float, default=1.54)
    args = ap.parse_args()

    cfg = load_config()
    img = Image.open(args.image).convert("RGB")
    rd = R.OcrReader(cfg)
    rd.grab = lambda _hwnd: img
    rd._scale = lambda: args.scale

    orig_recognize = R.OcrReader._recognize

    def traced_recognize(self, im):
        out = orig_recognize(self, im)
        print("   [trace] _recognize(%dx%d) -> %d 块" % (im.width, im.height, len(out)))
        for b in out:
            print("            %-24s x=%4d..%-5d y=%4d..%-5d"
                  % ((b.text or "")[:22], b.x1, b.x2, b.y1, b.y2))
        return out

    orig_bubble = R.bubble_boxes

    def traced_bubble(a, bg, tol=45, min_x=0):
        out = orig_bubble(a, bg, tol=tol, min_x=min_x)
        print("   [trace] bubble_boxes(min_x=%d, 数组=%s) -> %d 个：%s"
              % (min_x, getattr(a, "shape", None), len(out), out[:6]))
        return out

    orig_has_ink = R._row_has_ink

    def traced_ink(row, ca, bg, need=0.02):
        ok = orig_has_ink(row, ca, bg, need=need)
        print("   [trace] has_ink %-22s y=%4d..%-4d x=%4d..%-4d -> %s"
              % ((row.text() or "")[:20], row.y1, row.y2, row.x1, row.x2, ok))
        return ok

    R.OcrReader._recognize = traced_recognize
    R.bubble_boxes = traced_bubble
    R._row_has_ink = traced_ink
    try:
        print("== 真 read_chat() ==")
        snap = rd.read_chat(0)
    finally:
        R.OcrReader._recognize = orig_recognize
        R.bubble_boxes = orig_bubble
        R._row_has_ink = orig_has_ink

    print("   裁剪框 = %s" % (rd.last_crop,))
    print("   stats  = %s" % rd.stats)
    if snap is None:
        print("   **None**（_geom_bad=%s）" % rd._geom_bad)
        return 1
    print("   标题=%r  消息 %d 条  时间分隔 %d 个"
          % (snap.title, len(snap.messages), len(snap.markers)))
    for m in snap.messages:
        r = m.rect or (0, 0, 0, 0)
        print("      [%s] y=%-4s x=%-4s..%-5s %s"
              % ("我" if m.is_self else "对方", r[1], r[0], r[2],
                 (m.text or "")[:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
