# -*- coding: utf-8 -*-
"""对裁下来的图片区域做数值取证 —— 确认它是「照片/表情包」而不是「界面装饰」。

读不了图（本模型不支持图片输入），所以只能靠数字判断。三个判据：

  ① 颜色直方图形状：照片是**又宽又平**的分布；纯 UI（气泡+文字）只有几个尖峰。
  ② 逐行方差轮廓：照片**每一行**都在变；文字气泡只在有字的那几行有方差。
  ③ 与窗口底色的距离：UI 装饰会大面积贴着主题底色，照片不会。

用法：python probes/analyze_image_crop.py out/image_capture/01_图片.png
"""

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("用法: python analyze_image_crop.py <png>")
        return 1
    path = sys.argv[1]
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = list(im.getdata())
    n = len(px)
    print("图: %s  %dx%d  %d 像素" % (path, w, h, n))

    # ① 颜色直方图
    from collections import Counter
    c = Counter(px)
    top = c.most_common(5)
    print()
    print("① 颜色直方图：不同颜色 %d 种（%.1f%% 的像素是唯一的）"
          % (len(c), 100.0 * len(c) / n))
    print("   最常见的前 5 色：")
    for col, k in top:
        print("     %-18s %6d  %5.1f%%" % (str(col), k, 100.0 * k / n))
    dominant = top[0][1] / n
    print("   最主色占比 %.1f%% —— %s"
          % (dominant * 100,
             "像 UI（单一底色主导）" if dominant > 0.5 else "不像单一底色，像照片"))

    # ② 逐行方差轮廓
    g = im.convert("L")
    gp = list(g.getdata())
    rows = []
    for y in range(h):
        row = gp[y * w:(y + 1) * w]
        m = sum(row) / w
        v = (sum((p - m) ** 2 for p in row) / w) ** 0.5
        rows.append(v)
    active = sum(1 for v in rows if v > 10)
    print()
    print("② 逐行灰度标准差：")
    print("   中位数 %.1f   最小 %.1f   最大 %.1f" % (sorted(rows)[h // 2], min(rows), max(rows)))
    print("   标准差 > 10 的行：%d / %d（%.1f%%）"
          % (active, h, 100.0 * active / h))
    print("   —— %s" % ("每一行都有内容，像照片"
                        if active > 0.9 * h else
                        "只有少数行有内容，像文字气泡"))

    # ③ 亮度分布宽度
    hist = g.histogram()
    lo = next(i for i, k in enumerate(hist) if sum(hist[:i + 1]) > n * 0.05)
    hi = next(i for i in range(255, -1, -1) if sum(hist[i:]) > n * 0.05)
    print()
    print("③ 5%%~95%% 亮度跨度：%d ~ %d（跨度 %d）" % (lo, hi, hi - lo))
    print("   —— %s" % ("跨度大，明暗层次丰富" if hi - lo > 120 else "跨度小，偏平"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
