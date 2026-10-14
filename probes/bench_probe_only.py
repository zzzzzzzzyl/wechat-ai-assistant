# -*- coding: utf-8 -*-
"""量 `OcrReader.probe()` 的真实耗时 —— 探针方案的经济性全靠这个数。

    python probes/bench_probe_only.py

⚠️ 为什么单独来一个：探针第一版为了"和 read_chat 用同一套几何规则"，
   在里面调了 `_bg()`（众数）+ `find_input_top()`（逐列扫描），还把整张
   1402x1011 的位图转了 numpy int16 —— **实测 132ms**（日志原文
   「约 132 ms，空闲时只占 13.2% 一个核」），**比 UIA 一整拍（100ms）还贵**，
   整个"探针 + 按需 OCR"的方案等于白做（甚至更糟）。

   改成「几何缓存上次 read_chat 用的裁剪框、没有缓存就只用配置算术」之后，
   探针里只剩 `grab + crop + resize + tobytes`，**没有 numpy**。

判据（两条都要）：
    ① 中位数 **< 60ms**（这样 1 秒一拍 ≤ 6% 一个核，明显优于 UIA 的 10%）
    ② 稳态（跑满几拍之后）不能比第一拍慢 —— 说明没有"每次重算几何"的隐藏成本

⚠️ 取**中位数**，别取单次：实测抓图会偶发窜到 179ms（别的进程抢核）。
"""

import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import dpi as _dpi  # noqa: E402

_dpi.enable()

from wechat_assistant import capture  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.ocr_reader import OcrReader  # noqa: E402
from wechat_assistant.reader import list_windows, pick_window  # noqa: E402

BUDGET_MS = 60.0


def main():
    cfg = load_config()
    win = pick_window(list_windows(cfg), cfg)
    if win is None:
        print("挑不出微信主窗口")
        return 1
    live = capture.capture_window(win["hwnd"])
    if live is None or capture.is_blank(live):
        print("**PrintWindow 抓到空白** —— 先把微信恢复出来再跑")
        return 1
    print("微信窗口 %dx%d" % (live.width, live.height))

    rd = OcrReader(cfg)
    # ⚠️⚠️ **不要覆写 `rd.grab`！** 第一版为了"排除别的变量"把它换成了
    #     一张缓存好的位图，结果量出来 **4ms**，还打出一句
    #     「探针自己只花 -35 ms」—— **负数，物理上不可能**。
    #     原因：那样量的是「crop + resize」，**根本没抓图**，而抓图正是探针
    #     成本的大头。探针的真实成本必须**包含抓图**，所以这里让它真抓。
    hwnd = win["hwnd"]

    # 冷启动：第一拍（没有几何缓存，走配置算术那条路）
    t0 = time.perf_counter()
    rd.probe(hwnd)
    cold = (time.perf_counter() - t0) * 1000

    # 稳态：连打 12 拍
    ts = []
    for _ in range(12):
        t0 = time.perf_counter()
        rd.probe(hwnd)
        ts.append((time.perf_counter() - t0) * 1000)
    med = statistics.median(ts)
    lo, hi = min(ts), max(ts)

    # 对照：抓图本身占多少（探针 = 抓图 + 一点点 PIL）
    caps = []
    for _ in range(8):
        t0 = time.perf_counter()
        capture.capture_window(win["hwnd"])
        caps.append((time.perf_counter() - t0) * 1000)
    cap_med = statistics.median(caps)

    print("\n冷启动第一拍 = %.0f ms（含首次 crop/resize 的分配）" % cold)
    print("稳态 12 拍    = 中位 %.0f ms ｜ 最低 %.0f ｜ 最高 %.0f" % (med, lo, hi))
    print("  其中抓图本身 = 中位 %.0f ms  -> 探针自己只花 %.0f ms"
          % (cap_med, med - cap_med))
    print("  全部 12 拍: %s" % " ".join("%.0f" % v for v in ts))

    print("\n折算（1 秒一拍）：常驻 %.1f%% 一个核" % (med / 1000 * 100))
    print("对照：UIA 那条路一整拍 ~100 ms -> 1 秒一拍 = 10%")
    print("      完整读一屏（抓图 + OCR）~250 ms -> 1 秒一拍 = 25%")

    ok = med < BUDGET_MS
    print("\n结论: %s（中位 %.0f ms，预算 %.0f ms）"
          % ("合格 —— 探针方案成立" if ok else "**太慢，方案不成立**", med, BUDGET_MS))
    if not ok:
        print("   先看探针里是不是又混进了像素分析（_bg / find_input_top / numpy）。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
