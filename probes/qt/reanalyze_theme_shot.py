# -*- coding: utf-8 -*-
"""重新分析**已经存下来的**微信抓图，定位每个色号到底在哪个区域。

    python -X utf8 probes/qt/reanalyze_theme_shot.py [图片路径]

⚠️ 为什么要有这个：
    上一次实测把结论写成了"标题栏 #DCDCE1（98.5%）"，据此把 `surface_alt`
    定成了 `#DCDCE1`。但用户看浅色模式时说**灰色太深了，不是微信配色** ——
    所以那份"实测"要么量错了区域，要么量错了窗口状态。
    原图还在（`out/theme_probe.png`），**不要重开一次采样**，
    先把原图按网格摊开看每个色号出现在哪儿，再决定改什么。
    （重采样会引入新的变量：微信当前是深色、窗口位置/大小都可能变了。）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from collections import Counter                                   # noqa: E402

from PIL import Image                                             # noqa: E402


def hexs(rgb):
    return "#%02X%02X%02X" % rgb[:3]


def lum(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "out", "theme_probe.png")
    if not os.path.exists(path):
        print("找不到图：%s" % path)
        return 1

    im = Image.open(path).convert("RGB")
    W, H = im.size
    px = im.load()
    print("图：%s" % path)
    print("尺寸：%d x %d" % (W, H))

    # 整窗亮度 —— 用来判断这张是深色还是浅色那一次
    step = 4
    vals = [lum(px[x, y]) for y in range(0, H, step) for x in range(0, W, step)]
    avg = sum(vals) / max(1, len(vals))
    print("整窗平均亮度：%.1f  -> 这张是【%s】那一次"
          % (avg, "浅色" if avg > 128 else "深色"))

    # ---- 全图高频色 ----
    cnt = Counter()
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            cnt[hexs(px[x, y])] += 1
    total = sum(cnt.values())
    print("\n== 全图最高频的 12 个色 ==")
    for c, n in cnt.most_common(12):
        print("   %s  %5.1f%%  (亮度 %.0f)" % (c, 100.0 * n / total, lum(
            tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)))))

    # ---- 按网格摊开，定位每个色号 ----
    # ⚠️ 网格是**目的**不是装饰：上一版只报"标题栏 98.5% 是某个色"，
    #    却没有说那个"标题栏区域"框到的是不是真的标题栏。
    #    摊成 6x8 格，就能一眼看出某个色是"整条横带"还是"某一块"。
    print("\n== 6 x 8 网格：每格的主色 ==")
    print("      " + "".join("%-11d" % (c * W // 8) for c in range(8)))
    for ry in range(6):
        row = []
        for rx in range(8):
            x0, x1 = rx * W // 8, (rx + 1) * W // 8
            y0, y1 = ry * H // 6, (ry + 1) * H // 6
            c = Counter()
            for y in range(y0, y1, 2):
                for x in range(x0, x1, 2):
                    c[hexs(px[x, y])] += 1
            top, n = c.most_common(1)[0]
            tt = sum(c.values())
            row.append("%s%3.0f%%" % (top, 100.0 * n / max(1, tt)))
        print("  y%-4d" % (ry * H // 6) + "".join("%-11s" % v for v in row))

    # ---- 专门找 #DCDCE1 出现的行/列 ----
    target = "#DCDCE1"
    rows = Counter()
    for y in range(H):
        for x in range(0, W, 3):
            if hexs(px[x, y]) == target:
                rows[y] += 1
    if rows:
        ys = sorted(rows)
        print("\n== %s 出现在哪些行 ==" % target)
        print("   行范围 %d ~ %d（共 %d 行命中，图高 %d）"
              % (ys[0], ys[-1], len(ys), H))
        # 按 10% 分桶，看是集中在某一条横带还是散落
        buckets = Counter()
        for y in ys:
            buckets[y * 10 // H] += 1
        for b in sorted(buckets):
            print("   %3d%%~%3d%% 高：%d 行" % (b * 10, (b + 1) * 10,
                                               buckets[b]))
    else:
        print("\n== %s 在这张图里**一次都没出现** ==" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
