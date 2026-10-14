# -*- coding: utf-8 -*-
"""
对任意图片跑 RapidOCR，输出「文本 + 像素坐标」。
用于验证截图方式是否正确（能否截到真实微信内容），也用于后续气泡左右定位。

用法：
    python ocr_image.py <图片路径> [放大倍数，默认2.0]
"""

import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    if len(sys.argv) < 2:
        print("用法: python ocr_image.py <图片路径> [放大倍数]")
        return
    path = sys.argv[1]
    scale = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0

    from PIL import Image

    img = Image.open(path)
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), 3)
        tmp = os.path.join(tempfile.gettempdir(), "ocr_scaled_tmp.png")
        img.save(tmp)
        work_path = tmp
    else:
        work_path = path

    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    result, _ = engine(work_path)
    if not result:
        print("!! 未识别到任何文本")
        return

    rows = []
    for box, text, score in result:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        rows.append(
            (
                int(min(xs) / scale),
                int(min(ys) / scale),
                int(max(xs) / scale),
                int(max(ys) / scale),
                float(score),
                text,
            )
        )
    rows.sort(key=lambda r: (r[1] // 14, r[0]))

    print("图片: %s  原始尺寸 %s  放大 %.1fx" % (path, Image.open(path).size, scale))
    print("识别到 %d 个文本块（坐标已还原到原图尺度）" % len(rows))
    print("%-6s %-6s %-6s %-6s %-5s %s" % ("x1", "y1", "x2", "y2", "score", "text"))
    for x, y, x2, y2, s, t in rows:
        print("%-6d %-6d %-6d %-6d %-5.2f %s" % (x, y, x2, y2, s, t))


if __name__ == "__main__":
    main()
