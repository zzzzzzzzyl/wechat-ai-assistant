# -*- coding: utf-8 -*-
"""精量微信设置窗口**左侧导航栏的选中项**：底色、文字色、圆角、尺寸。

    python -X utf8 probes/qt/analyze_settings_nav.py <抓图路径>

⚠️ 为什么要单独精量：
    `measure_wechat_settings.py` 的区域统计只能告诉我们"选中项那块是绿的"，
    但**改代码要的是**：绿是哪个色号、上面的字是什么色、有没有圆角、多高、
    铺满导航栏还是留了边距。这些必须逐像素找出来。

⚠️ 找法：选中项是**一整块连通的绿色**，跟周围底色差很大。
    先用"彩度非零"筛出候选像素（灰阶底色的彩度接近 0），
    再取它的包围盒，然后在盒内统计"最亮的那个色"当文字色
    （文字必然比它自己的底色亮或暗，取对比大的那头）。
"""
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PIL import Image                                             # noqa: E402


def hexs(rgb):
    return "#%02X%02X%02X" % tuple(rgb[:3])


def lum(rgb):
    r, g, b = rgb[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def chroma(rgb):
    mx, mn = max(rgb[:3]), min(rgb[:3])
    return 0.0 if mx == 0 else (mx - mn) / float(mx)


def is_green(rgb):
    """⚠️ 必须按**绿**筛，不能只按"彩度非零"筛。

    第一版就是按彩度筛的，结果包围盒被**蓝**像素带跑了
    （左栏里有个偏蓝的图标 `#C6D8E6`，彩度 0.139 也过了 0.25 的线），
    算出来 316x120，而真正的绿色条只有 240x60 ——
    于是"四个角是不是圆角"量的是那块蓝图标的位置，结论全是错的。
    **筛条件写松了，后面所有几何结论都不可信。**
    """
    r, g, b = rgb[:3]
    return g > r + 20 and g > b + 20


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        # 找最近一张设置窗口的抓图
        d = os.path.join(ROOT, "out")
        cands = sorted([os.path.join(d, f) for f in os.listdir(d)
                        if f.startswith("wechat_settings_")])
        if not cands:
            print("找不到 wechat_settings_*.png，先跑 measure_wechat_settings.py")
            return 1
        path = cands[-1]
    im = Image.open(path).convert("RGB")
    W, H = im.size
    px = im.load()
    print("图：%s\n尺寸 %dx%d\n" % (path, W, H))

    # ---- 1. 找绿色的连通块（只在左半边找）----
    xs, ys = [], []
    for y in range(H):
        for x in range(0, W // 2):
            if is_green(px[x, y]):
                xs.append(x)
                ys.append(y)
    if not xs:
        print("左半边没找到绿色块 —— 左栏里没有绿色选中项？")
        return 1
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    print("== 1. 左栏绿色块 ==")
    print("   包围盒 x %d~%d  y %d~%d   尺寸 %dx%d"
          % (x0, x1, y0, y1, x1 - x0 + 1, y1 - y0 + 1))

    cnt = Counter()
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            cnt[px[x, y]] += 1
    total = sum(cnt.values()) or 1
    print("   块内主色：")
    for c, n in cnt.most_common(6):
        print("      %s  %5.1f%%   亮度 %5.1f  彩度 %.3f"
              % (hexs(c), 100.0 * n / total, lum(c), chroma(c)))

    # ---- 2. 底色 = 块内最高频的那个绿色 ----
    bg = None
    for c, n in cnt.most_common(40):
        if is_green(c):
            bg = c
            break
    bg = bg or cnt.most_common(1)[0][0]
    print("\n   -> 选中项**底色** = %s（亮度 %.1f）" % (hexs(bg), lum(bg)))

    # ---- 3. 文字色 = 块内跟底色亮度差最大的那批像素里最高频的 ----
    cand = Counter()
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            c = px[x, y]
            if abs(lum(c) - lum(bg)) > 40:
                cand[c] += 1
    print("\n== 2. 选中项上的**文字**色 ==")
    if cand:
        for c, n in cand.most_common(5):
            print("      %s  %d px   亮度 %5.1f  跟底色亮度差 %5.1f"
                  % (hexs(c), n, lum(c), abs(lum(c) - lum(bg))))
        fg = cand.most_common(1)[0][0]
        print("   -> 文字色取 %s" % hexs(fg))
    else:
        print("      没找到跟底色反差够大的像素 —— 这一项可能没有文字？")

    # ---- 4. 圆角？量四个角是不是底色 ----
    print("\n== 3. 选中块是不是圆角（量四个角）==")
    for label, (cx, cy) in (("左上", (x0, y0)), ("右上", (x1, y0)),
                            ("左下", (x0, y1)), ("右下", (x1, y1))):
        c = px[cx, cy]
        same = abs(lum(c) - lum(bg)) < 25
        print("   %s角 (%d,%d) = %s   %s"
              % (label, cx, cy, hexs(c), "还是绿色（直角）" if same else "不是绿色（圆角）"))

    # ---- 5. 导航栏底色、左/右栏分界 ----
    print("\n== 4. 导航栏整体（取选中块下方一行扫）==")
    yscan = min(H - 2, y1 + 40)
    prev = None
    for x in range(0, W):
        c = px[x, yscan]
        if prev is not None and abs(lum(c) - lum(prev)) >= 6:
            print("   x=%4d (%.0f%%)  %s -> %s"
                  % (x, 100.0 * x / W, hexs(prev), hexs(c)))
        prev = c
    return 0


if __name__ == "__main__":
    sys.exit(main())
