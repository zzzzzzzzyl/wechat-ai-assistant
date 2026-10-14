# -*- coding: utf-8 -*-
"""逐步复现 `read_chat` 的**真实**中间量（真机截图 + 真产品函数）。

    python probes\diag_read_steps.py <截图.png> [--scale 1.54]

和 `diag_live_pipeline.py` 的区别：那个是自己拼几何（**会和产品漂**，实测已经漂了：
`input_top` 一个 708 一个 789、`cl` 一个 421 一个 424），所以它报出来的数
**不能当判据**。这个脚本**按产品代码的顺序**走一遍 `read_chat` 里的每一步，
并且每一步都打印**产品的实际取值**：

  ① 配 `chat_left` / 自动检测 / 最终采纳
  ② `_bg()` / `input_top` / 裁剪框 / 分析数组的形状
  ③ `bubble_boxes()` 与分析数组（**和裁剪图同坐标系**，不需要平移）
     —— 每个盒打：位置、颜色数、是不是假盒子、里面装了哪些文字块
  ④ `_image_regions()` 判出来哪些是图片
  ⑤ `read_chat()` 的最终结果

⚠️ 纯只读一张 PNG。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
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
    W, H = img.size
    rd = R.OcrReader(cfg)
    rd.grab = lambda _hwnd: img
    rd._scale = lambda: args.scale
    scale = args.scale

    a = np.asarray(img.convert("RGB")).astype(np.int16)
    x1, y1, x2, _y2 = R.chat_rect((0, 0, W, H), scale, cfg, (W, H))
    bg = rd._bg(a, x1, x2, y1)
    input_top = rd._input_top(a, bg, H, x1, x2, y1)
    print("① 配置 chat_left=%d  自动检测=%s  候选=%s"
          % (x1, R.detect_chat_left(a, bg), list(rd._geometry_candidates(a, bg, x1))))
    print("② _bg()=%s  input_top=%s  chat_rect=(%d,%d)-(%d,%d)  图 %dx%d"
          % (tuple(int(v) for v in bg), input_top, x1, y1, x2, _y2, W, H))

    from wechat_assistant.sender import _cv2
    has_cv2 = _cv2() is not None
    print("   cv2 = %s" % has_cv2)

    for bx1 in rd._geometry_candidates(a, bg, x1):
        cl = rd._crop_left(bx1)
        crop_box = (cl, y1, x2, input_top)
        area = img.crop(crop_box)
        # ⚠️ **和产品一样**：分析数组 = 裁剪图那一块（`a[y1:input_top, cl:x2]`），
        #    这样盒坐标和 OCR 块坐标天生对齐，不需要任何平移。
        ca = a[crop_box[1]:crop_box[3], crop_box[0]:crop_box[2]]
        blocks = rd._recognize(area)
        raw = R.bubble_boxes(ca, bg) if has_cv2 else []
        boxes = list(raw)
        print("\n== 候选 bx1=%d  裁剪左=%d  裁剪框=%s  裁剪图 %dx%d  分析数组 %s =="
              % (bx1, cl, crop_box, area.width, area.height, ca.shape))
        print("   气泡盒（%d 个）：" % len(raw))
        for i, b in enumerate(raw):
            ins = [x for x in blocks if b[0] <= x.cx <= b[2] and b[1] <= x.cy <= b[3]]
            print("      #%-2d %4d×%-4d @(%4d,%4d) 颜色数=%3d 假盒=%-5s 装了 %d 块 %s"
                  % (i, b[2] - b[0], b[3] - b[1], b[0], b[1],
                     R._color_count(ca, b), R._box_is_phantom(b, blocks),
                     len(ins), [(x.text or '')[:8] for x in ins][:4]))
        real = [b for b in boxes if not R._box_is_phantom(b, blocks)]
        print("   _image_regions(real, mid_x=%d) = %s"
              % (area.width // 2, R._image_regions(real, ca, area.width / 2.0)))
        print("   OCR %d 块：" % len(blocks))
        for b in blocks:
            print("      %-24s x=%4d..%-5d y=%4d..%-5d"
                  % ((b.text or "")[:22], b.x1, b.x2, b.y1, b.y2))
        if blocks or raw:
            break

    print("\n== 产品 read_chat() 的最终结果 ==")
    snap = rd.read_chat(0)
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
