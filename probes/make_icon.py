# -*- coding: utf-8 -*-
"""把一张大图做成程序图标（PNG + 多尺寸 ICO）。

为什么要这么一步
----------------
Windows 的任务栏/标题栏/Alt-Tab 会按**不同尺寸**取图标（16/24/32/48/256…）。
只给一个 1298×1298 的 PNG 的话：
  · Qt 自己缩放质量一般（尤其 16×16 那种小尺寸会糊）；
  · 而且**任务栏按钮的图标**走的是另一条路（`SetCurrentProcessExplicitAppUserModelID`
    + 窗口图标），需要能按尺寸取到合适的位图。
所以一次性把常用的几档都烘进一个 `.ico` 里，运行时只加载一个文件。

用法：
    python probes/make_icon.py <源图> [输出目录]
默认输出到 `wechat_assistant/assets/`（`paths.pkg_dir()` 认这个位置，
打包后会跟着进包）。
"""

import os
import sys

#: 烘进 .ico 的尺寸。Windows 实际会取的大致就是这几档。
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
#: 单独存一份 PNG 用的尺寸（Qt 的 `QIcon` 也认 PNG，给个大一点的）。
PNG_SIZE = 256


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    src = argv[1]
    if not os.path.exists(src):
        print("找不到源图: %s" % src)
        return 1
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = argv[2] if len(argv) > 2 else os.path.join(
        root, "wechat_assistant", "assets")
    os.makedirs(out_dir, exist_ok=True)

    from PIL import Image

    im = Image.open(src).convert("RGBA")
    print("源图: %s  %dx%d  %s" % (src, im.width, im.height, im.mode))
    # ⚠️ **正方形化**：任务栏图标是方的，非方图会被拉伸得很难看。
    #    取短边居中裁一个正方形再缩放 —— 而不是直接 resize（那会变形）。
    s = min(im.width, im.height)
    if im.width != im.height:
        left = (im.width - s) // 2
        top = (im.height - s) // 2
        im = im.crop((left, top, left + s, top + s))
        print("  -> 居中裁成 %dx%d（原图不是正方形）" % (s, s))

    png = os.path.join(out_dir, "icon.png")
    im.resize((PNG_SIZE, PNG_SIZE), Image.LANCZOS).save(png)
    print("  -> %s  (%dx%d)" % (png, PNG_SIZE, PNG_SIZE))

    ico = os.path.join(out_dir, "icon.ico")
    # ⚠️ 每一步都用 LANCZOS 单独缩，不要拿上一步的结果再缩（会累积模糊）。
    im.resize((256, 256), Image.LANCZOS).save(
        ico, format="ICO", sizes=[(n, n) for n in ICO_SIZES])
    print("  -> %s  (%s)" % (ico, "/".join(str(n) for n in ICO_SIZES)))

    # 自检：把 .ico 读回来，确认里面真的有那么多档
    with Image.open(ico) as back:
        got = sorted(back.info.get("sizes") or [])
    print("  自检：.ico 里实际有 %d 档 %s" % (len(got), got))
    if len(got) != len(ICO_SIZES):
        print("  ⚠️ 档数对不上（期望 %d）" % len(ICO_SIZES))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
