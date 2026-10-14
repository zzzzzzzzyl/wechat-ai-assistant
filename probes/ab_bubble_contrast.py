# -*- coding: utf-8 -*-
"""对照实验：**气泡填充色和聊天底色的色差** 决定 `bubble_boxes()` 成不成。

    python probes\ab_bubble_contrast.py

⚠️ 纯离线、纯只读、不碰微信：自己造两张图，颜色**全部取自项目实测值**
   （`wechat_assistant/theme.py` 的 `DARK_PALETTE` / `LIGHT_PALETTE`）：

     深色：聊天底 #1E1E1F，对方气泡 #2F2F30   -> 曼哈顿色差 51（量化后 58）
     浅色：聊天底 #FAFAFA，对方气泡 #FAFAFA   -> 曼哈顿色差 **0**（量化后 6）

   两张图除了底色/气泡色**完全一样**（同样的文字块、同样的几何），
   所以差异只可能来自配色。

判据是产品代码 `ocr_reader.bubble_boxes(tol=45)`，不是这里另写一份。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from wechat_assistant import ocr_reader as R  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402

W, H = 900, 700
BG_DARK, BUB_DARK = (0x1E, 0x1E, 0x1F), (0x2F, 0x2F, 0x30)
BG_LIGHT, BUB_LIGHT = (0xFA, 0xFA, 0xFA), (0xFA, 0xFA, 0xFA)
TEXT_DARK, TEXT_LIGHT = (0xE1, 0xE1, 0xE5), (0x14, 0x14, 0x14)


def build(bg, bubble, text_col):
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    # 两条"对方的"气泡（贴左）+ 一条"我的"（贴右，微信自己的气泡是绿的）
    boxes = [(60, 80, 420, 140), (60, 200, 360, 260), (480, 380, 840, 440)]
    for i, (x1, y1, x2, y2) in enumerate(boxes):
        col = bubble if i < 2 else (0x95, 0xEC, 0x69)   # 我的绿气泡
        d.rounded_rectangle([x1, y1, x2, y2], radius=8, fill=col)
        # 假装是文字笔画：几条深色/浅色短线
        for k in range(3):
            yy = y1 + 14 + k * 12
            d.rectangle([x1 + 16, yy, x2 - 40 - k * 30, yy + 7], fill=text_col)
    return img


def run(name, img):
    a = np.asarray(img).astype(np.int16)
    cfg = load_config()
    bgv = R.OcrReader(cfg)._bg(a, 130, W, 0)   # 从 x=130 起（躲开左侧头像）
    boxes = R.bubble_boxes(a, bgv, tol=45)
    left = [b for b in boxes if not R.side_of_box(b, W)]
    right = [b for b in boxes if R.side_of_box(b, W)]
    print("== %s ==" % name)
    print("   底色 %s   气泡色 %s   色差 = %d   （tol = 45）"
          % (tuple(int(v) for v in bgv), name == "深色" and BUB_DARK or BUB_LIGHT,
             sum(abs(int(x) - int(y))
                 for x, y in zip(bgv, BUB_DARK if name == "深色" else BUB_LIGHT))))
    print("   真实气泡：对方 2 个 + 我 1 个 = 3 个")
    print("   bubble_boxes 认出 %d 个（对方侧 %d，我的侧 %d）"
          % (len(boxes), len(left), len(right)))
    for b in boxes:
        print("      %4d×%-4d @(%3d,%3d) -> %s"
              % (b[2] - b[0], b[3] - b[1], b[0], b[1],
                 "我的" if R.side_of_box(b, W) else "对方"))
    return len(boxes)


def main():
    dark = build(BG_DARK, BUB_DARK, TEXT_DARK)
    light = build(BG_LIGHT, BUB_LIGHT, TEXT_LIGHT)
    p = os.path.join(os.environ.get("TEMP", "."), "ab_bubble_dark.png")
    dark.save(p)
    p2 = os.path.join(os.environ.get("TEMP", "."), "ab_bubble_light.png")
    light.save(p2)

    nd = run("深色", dark)
    nl = run("浅色", light)

    print("""
结论：
  · 深色下对方气泡和底色的色差是 **58**（量化后）—— 过了 tol=45 这条线，
    能认出；余量只有 13（真机实测是 56，余量 11）。
  · 浅色下两者**基本是同一个色**（实测结论见 theme.py：都是 #FAFAFA），
    量化后色差 **6**，**连通域判据零信号** ——
    对方的气泡永远认不出来；此时只有"我的绿气泡"（色差几百）还能被认出来。
  · `read_chat` 的几何那一轮要 "boxes 非空" 才通过 —— 一屏里只要有人发过绿气泡
    它就"侥幸通过"，然后按气泡盒切成 0 条消息（对方的全在盒外/无盒）→ 症状正是
    "标题正常、消息 0 条"，和用户实测一模一样。
  · 100%% 可复现的合成图：%s / %s
""" % (p, p2))
    print("深色认出 %d 个、浅色认出 %d 个（同几何、同文字，只有配色不同）" % (nd, nl))
    return 0


if __name__ == "__main__":
    sys.exit(main())
