# -*- coding: utf-8 -*-
"""扫一批截图，列出「聊天区底色 / 对方气泡填充色 / 两者色差」。

用来找**浅色的真聊天截图**，并且量出「气泡填充色和底色的色差」到底是多少 ——
`bubble_boxes(tol=45)` 的成败全看这个数。

    python probes\scan_theme_colors.py out\shots out\sender_shots out

⚠️ 纯只读：只读已有 PNG。
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from wechat_assistant import ocr_reader as R  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402


def modal(a, q=4):
    sub = a.astype(np.int32)
    key = ((sub[:, :, 0] // q) * 1024 + (sub[:, :, 1] // q) * 32
           + (sub[:, :, 2] // q))
    vals, counts = np.unique(key.ravel(), return_counts=True)
    order = np.argsort(-counts)
    out = []
    for i in order[:6]:
        k = int(vals[i])
        col = ((k // 1024) * q + q // 2, ((k // 32) % 32) * q + q // 2,
               (k % 32) * q + q // 2)
        out.append((col, float(counts[i]) / float(key.size)))
    return out


def dist(a, b):
    return sum(abs(int(x) - int(y)) for x, y in zip(a, b))


def one(cfg, path):
    img = Image.open(path).convert("RGB")
    W, H = img.width, img.height
    scale = 1.5
    rd = R.OcrReader(cfg)
    x1, y1, x2, y2 = R.chat_rect((0, 0, W, H), scale, cfg, (W, H))
    a = np.asarray(img).astype(np.int16)
    if x2 - x1 < 60 or y2 - y1 < 60:
        return None
    bg = rd._bg(a, x1, x2, y1)
    if bg is None:
        return None
    ca = a[y1:y2, x1:x2]
    tops = modal(ca)
    boxes = R.bubble_boxes(ca, bg)
    lum = float(np.asarray(bg).mean())
    return dict(path=path, size=(W, H), bg=tuple(int(v) for v in bg), lum=lum,
                tops=tops, boxes=boxes, ca=ca, bgarr=bg)


def main():
    cfg = load_config()
    dirs = sys.argv[1:] or ["."]
    files = []
    for d in dirs:
        if os.path.isfile(d):
            files.append(d)
            continue
        for root, _dd, ff in os.walk(d):
            for f in ff:
                if f.lower().endswith(".png"):
                    files.append(os.path.join(root, f))
    print("扫描 %d 个 PNG\n" % len(files))
    print("%-46s %-11s %-16s %-16s %-6s %s"
          % ("文件", "尺寸", "聊天区底色", "次众数(候选气泡色)", "色差", "气泡盒数"))
    print("-" * 118)
    for p in files:
        try:
            r = one(cfg, p)
        except Exception as e:
            print("%-46s 跳过（%s: %s）" % (os.path.basename(p), type(e).__name__, e))
            continue
        if r is None:
            continue
        tops = [t for t in r["tops"] if dist(t[0], r["bg"]) > 4]
        second = tops[0] if tops else (r["bg"], 1.0)
        d = dist(second[0], r["bg"])
        tag = "深色" if r["lum"] < 128 else "浅色"
        print("%-46s %-11s %-16s %-16s %-6d %d  [%s]"
              % (os.path.basename(p), "%dx%d" % r["size"], str(r["bg"]),
                 "%s %.0f%%" % (str(second[0]), second[1] * 100), d,
                 len(r["boxes"]), tag))


if __name__ == "__main__":
    main()
