# -*- coding: utf-8 -*-
"""真机截图 + **可调 tol** 跑完整产品链路（含 OCR），证明"丢在阈值上"。

    python probes\measure_live_read.py <截图.png> --tol 45   # 现状：对方的话全丢
    python probes\measure_live_read.py <截图.png> --tol 40   # 只把阈值降 5 就能读出来

做法：把 `OcrReader.grab` 换成「返回这张已经抓好的图」，然后调**真的
`rd.read_chat()`** —— 跑的是产品代码本身，不是我另写的一份。

⚠️ 为什么只能这样验：这台机器上微信窗口的 **PrintWindow 给全黑**
   （`check_capture_selftest.py` 自检正常、UIA 也是 0 个控件），
   所以离线用**屏幕截图**喂进去是唯一能在真机上量到"消息 N 条"的路。
⚠️ 纯只读：只读一张 PNG、不碰微信。
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402

from wechat_assistant import ocr_reader as R  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--scale", type=float, default=1.54)
    ap.add_argument("--tol", type=int, default=45)
    args = ap.parse_args()

    cfg = load_config()
    img = Image.open(args.image).convert("RGB")
    # ⚠️ 把日志接出来 —— 产品里的 `_log` 默认是空的，而
    #    `DSH_OCR_TRACE=1` 时它会把"每一条行为什么被丢"打出来，
    #    排查时这行是**唯一**能看到取舍的地方。
    rd = R.OcrReader(cfg, logger=lambda m: print("   [log] %s" % m))
    rd.grab = lambda _hwnd: img          # ⚠️ 只有取图被替换，其余全走产品代码
    rd._scale = lambda: args.scale

    # ⚠️ 阈值是硬编码在 `read_chat()` 里的（`bubble_boxes(...)` 用默认 45），
    #    这里用**默认参数替换**来改它 —— 只改这一个数，别的一律不动。
    #    ⚠️ 签名必须跟着产品走（`bubble_boxes(a, bg, tol, min_x)`），
    #       漏一个参数会在真机上报 `unexpected keyword argument`。
    orig = R.bubble_boxes
    try:
        R.bubble_boxes = (lambda a, bg, tol=args.tol, min_x=0:
                          orig(a, bg, tol=tol, min_x=min_x))
        print("tol = %d（其他一律走产品代码）" % args.tol)
        t0 = time.perf_counter()
        snap = rd.read_chat(0)
        ms = (time.perf_counter() - t0) * 1000
    finally:
        R.bubble_boxes = orig

    if snap is None:
        print("**read_chat 返回 None**（_geom_bad=%s）" % rd._geom_bad)
        return 1
    print("标题 = %r" % snap.title)
    print("**消息 %d 条**   时间分隔 %d 个   （%.0f ms）"
          % (len(snap.messages), len(snap.markers), ms))
    for m in snap.messages:
        r = m.rect or (0, 0, 0, 0)
        print("   [%s] y=%-4s x=%s..%-5s %s"
              % ("我" if m.is_self else "对方", r[1], r[0], r[2],
                 (m.text or "")[:70]))
    for k in snap.markers:
        print("   [时间] %s" % k.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
