# -*- coding: utf-8 -*-
"""把一张图降采样成**字符画**打印出来。

为什么需要：本模型不支持图片输入（`read_image` 直接报
"model does not declare image input"），验不了「裁出来的到底是不是那张图」。
但把亮度映射成字符之后，**用眼睛看文本**就够了 —— 照片、二维码、纯色块、
文字行，各自的字符纹理一眼可分。

用法：
    python probes/ascii_shot.py <png> [列数，默认 96]
    python probes/ascii_shot.py <png> --region x1,y1,x2,y2
"""

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402

RAMP = " .:-=+*#%@"          # 从暗到亮


def render(im, cols=96, x1=None, y1=None, x2=None, y2=None):
    if x1 is not None:
        im = im.crop((x1, y1, x2, y2))
    w, h = im.size
    # 字符是高的，纵向要压缩一半左右
    rows = max(1, int(cols * h / w * 0.5))
    g = im.convert("L").resize((cols, rows), Image.BILINEAR)
    px = list(g.getdata())
    print("原图 %dx%d -> 字符 %dx%d" % (w, h, cols, rows))
    print("+" + "-" * cols + "+")
    for y in range(rows):
        line = "".join(RAMP[min(9, px[y * cols + x] * 10 // 256)] for x in range(cols))
        print("|" + line + "|")
    print("+" + "-" * cols + "+")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    path = sys.argv[1]
    cols = 96
    box = None
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--region":
            box = [int(v) for v in args[i + 1].split(",")]
            i += 2
        else:
            cols = int(args[i])
            i += 1

    im = Image.open(path).convert("RGB")
    if box:
        render(im, cols, *box)
    else:
        render(im, cols)
    return 0


if __name__ == "__main__":
    sys.exit(main())
