# -*- coding: utf-8 -*-
"""量「记录器一拍」的真实成本 —— 不是只量探针。

    python probes/bench_tick_cost.py

⚠️ 为什么必须单独来一个：`Recorder.tick()` 的顺序是

    ① `read_chat_title(hwnd)`   ← **每拍都做**
    ② 探针（抓图 + 哈希）
    ③ 探针命中才 `read_chat()`

而日志里那句「约 49 ms，空闲时只占 4.9% 一个核」**只量了第 ②步**。
第 ①步在 OCR 后端是**抓一次全窗 + OCR 一条标题栏**，很可能比探针还贵 ——
那样"探针让空闲成本降到 5%"这个结论就是**错的**（而且错在我自己的量法上）。

这个脚本把三段拆开量，并把它们加起来换算成"一个核的百分比"。
"""

import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import dpi as _dpi  # noqa: E402

_dpi.enable()

from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.ocr_reader import OcrReader  # noqa: E402
from wechat_assistant.reader import list_windows, pick_window  # noqa: E402


def med(fn, n=8):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts), min(ts), max(ts)


def main():
    cfg = load_config()
    win = pick_window(list_windows(cfg), cfg)
    if win is None:
        print("挑不出微信主窗口")
        return 1
    hwnd = win["hwnd"]

    rd = OcrReader(cfg)
    rd._engine().load()
    rd._engine().warmup()

    t_title = med(lambda: rd.read_chat_title(hwnd))
    t_probe = med(lambda: rd.probe(hwnd))
    t_full = med(lambda: rd.read_chat(hwnd), n=4)

    def fmt(x):
        return "中位 %6.0f ms ｜ 最低 %6.0f ｜ 最高 %6.0f" % x

    print("\n== 记录器一拍的三段（各自独立测量）==")
    print("   ① read_chat_title  " + fmt(t_title) + "   ← ⚠️ **每拍都做**")
    print("   ② probe（探针）     " + fmt(t_probe) + "   ← 日志里只报了这一段")
    print("   ③ read_chat（真读） " + fmt(t_full) + "   ← 只有探针命中才做")

    idle = t_title[0] + t_probe[0]
    hit = idle + t_full[0]
    print("\n== 换算成「一个核的百分比」==")
    # ⚠️ **探针模式下，没命中的那些拍不读标题**（2026-10-11 修的）：
    #    标题 OCR 有 65ms，比探针还贵，而探针命中的那次真读本来就会填标题。
    #    所以下面把"修好之后"和"修之前"两种情况都列出来 ——
    #    只报一个数的话，就看不出这次改了多少。
    probe_only = t_probe[0]
    probe_plus_title = t_title[0] + t_probe[0]
    hit = t_title[0] + t_probe[0] + t_full[0]      # 命中那一拍：标题 + 探针 + 真读
    full_only = t_title[0] + t_full[0]             # 每拍都真读

    rows = [
        ("空闲（探针模式，**修好之后**）", probe_only),
        ("空闲（每拍还读标题，修之前）", probe_plus_title),
        ("命中（标题 + 探针 + 真读一屏）", hit),
        ("每拍都真读 record=true", full_only),
    ]
    print("   %-36s %8s %8s %8s" % ("场景", "0.5s 一拍", "1.0s 一拍", "5.0s 一拍"))
    for label, ms in rows:
        print("   %-36s %7.1f%% %7.1f%% %7.1f%%"
              % (label, ms / 500 * 100, ms / 1000 * 100, ms / 5000 * 100))

    print("\n   对照：UIA 那条路一整拍约 100 ms -> 1 秒一拍 10%")

    print("\n== 结论 ==")
    print("   探针模式下**没命中的拍不读标题**，所以空闲成本 = 只探针 = %.0f ms"
          "（1 秒一拍 %.1f%%）" % (probe_only, probe_only / 1000 * 100))
    print("   ⚠️ 而修之前那个 65ms 的标题 OCR 是每拍都做的：")
    print("      %.0f + %.0f = %.0f ms（1 秒一拍 **%.1f%%**）——"
          % (t_title[0], t_probe[0], probe_plus_title, probe_plus_title / 1000 * 100))
    print("      和 UIA（10%）**一样贵**，而我当时只量了探针那一段，")
    print("      就说过「比 UIA 还省」—— 那是错的。")
    print("\n   命中那一拍是 %.0f ms（含 380ms 左右的真读），" % hit)
    print("   所以滑动档 `probe_fast` 的下限必须 ≥ 0.5s，否则拍比拍还长。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
