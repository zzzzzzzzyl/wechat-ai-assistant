# -*- coding: utf-8 -*-
"""把**当前屏幕上的微信窗口**截下来存成一张图（给排查用）。

    python probes\shot_wechat_now.py [输出路径]

为什么需要它（2026-10-12 实测）：这台机器上**微信窗口的 `PrintWindow` 给全黑**
（`check_capture_selftest.py` 自检正常、UIA 也是 0 个控件），所以要看"微信现在
长什么样"只能**屏幕截图**。

⚠️ 纯只读：只截屏，不置顶、不激活、不点任何东西。
⚠️ 窗口矩形可能**超出屏幕**（本机 2240x1400，实测微信矩形下边到 2040）——
   超出部分会被补成纯黑，所以这里先和屏幕取交集再截。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import ctypes  # noqa: E402

from wechat_assistant import dpi as _dpi  # noqa: E402

print("DPI 感知: %s" % _dpi.enable())

import win32gui  # noqa: E402
from PIL import ImageGrab  # noqa: E402

from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.reader import list_windows, pick_window  # noqa: E402


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("TEMP", "."), "wechat_now.png")
    cfg = load_config()
    cands = list_windows(cfg)
    for c in cands:
        r = c["rect"]
        print("   候选 hwnd=0x%X title=%r %dx%d"
              % (c["hwnd"], c["title"], r[2] - r[0], r[3] - r[1]))
    win = pick_window(cands, cfg)
    if win is None:
        print("挑不出主窗口")
        return 1
    u = ctypes.windll.user32
    u.SetProcessDPIAware()
    SW, SH = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    x1, y1, x2, y2 = win["rect"]
    vx1, vy1 = max(0, x1), max(0, y1)
    vx2, vy2 = min(SW, x2), min(SH, y2)
    print("   窗口 rect=%s  屏幕 %dx%d  取交集=(%d,%d)-(%d,%d)"
          % ((x1, y1, x2, y2), SW, SH, vx1, vy1, vx2, vy2))
    if vx2 - vx1 < 50 or vy2 - vy1 < 50:
        print("   **可见部分太小** —— 微信是不是最小化了？")
        return 1
    img = ImageGrab.grab(bbox=(vx1, vy1, vx2, vy2), all_screens=True).convert("RGB")
    img.save(out)
    print("   已存 %s  (%dx%d)" % (out, img.width, img.height))
    return 0


if __name__ == "__main__":
    sys.exit(main())
