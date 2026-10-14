# -*- coding: utf-8 -*-
"""量「抓图 + 缩略图哈希」的成本 —— **不依赖微信**，用任意大窗口量。

    python probes/bench_capture_hash.py

为什么单独写一个：`bench_probe_vs_ocr.py` 要微信窗口处于正常状态，
而微信经常是缩小的 / 停在扫码登录窗 / 被最小化 —— 那时它跑不了，
但"抓图 + 哈希有多便宜"这个**结论本身和微信无关**：
    抓图成本只和**窗口面积**成正比（PrintWindow 是一次 blit + 位图拷贝），
    缩略图哈希成本只和缩略图尺寸有关（THUMB=64 -> 4096 字节）。

⚠️ 抓不到内容的窗口（Chrome 这类走 GPU 合成的）**耗时是一样的** ——
   这里只量时间，不看内容，所以 blank 不影响结论（脚本会注明）。
"""

import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import capture  # noqa: E402
import win32gui  # noqa: E402

THUMB = 64


def thumb_hash(img):
    return img.convert("L").resize((THUMB, THUMB)).tobytes()


def bench(fn, n=10):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts), min(ts)


def main():
    cands = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h):
            r = win32gui.GetWindowRect(h)
            w, hh = r[2] - r[0], r[3] - r[1]
            if w > 300 and hh > 200 and r[0] > -10000:
                cands.append((w * hh, h, w, hh, win32gui.GetClassName(h)))
        return True

    win32gui.EnumWindows(cb, None)
    if not cands:
        print("没有够大的可见窗口可量")
        return 1
    cands.sort(reverse=True)

    print("== 抓图 + 缩略图哈希（THUMB=%d）==" % THUMB)
    best = None
    for area, h, w, hh, cls in cands[:5]:
        img = capture.capture_window(h)
        if img is None:
            continue
        total, _ = bench(lambda: thumb_hash(capture.capture_window(h)))
        cap, _ = bench(lambda: capture.capture_window(h))
        th, _ = bench(lambda: thumb_hash(img))
        print("   %-34s %4dx%-4d (%5.2f MP)  抓图 %5.1f ms + 哈希 %4.1f ms = %5.1f ms  %s"
              % (cls[:34], w, hh, area / 1e6, cap, th, total,
                 "(抓不到内容，但耗时一样)" if capture.is_blank(img) else ""))
        if best is None or area > best[0]:
            best = (area, total, cap, th)

    if best is None:
        return 1
    area, total, cap, th = best

    # ---- **结论从量出来的数推，不写死。**
    #
    # ⚠️ 第一版这里硬编码了"探针 6~10 ms、比真读便宜 30 倍" —— 而同一段代码
    #    量出来是 **35 ms**（1.39 MP 的窗口）。**脚本的结论和它自己的数矛盾**，
    #    比没有结论更糟（会被当成"量过了"）。所以现在全部按面积折算着算。
    TARGET_MP = 1206 * 736 / 1e6        # 实测那台机器的微信窗口（1206x736）
    probe_ms = total * (TARGET_MP / (area / 1e6))
    read_ms = 250.0                     # 真读一屏（抓图 + OCR + 切行）的实测下限
    print("\n   折算到微信窗口 %.2f MP（按面积等比）：探针约 %.0f ms"
          % (TARGET_MP, probe_ms))
    print("   （抓图 %.0f ms 里绝大部分是 PrintWindow 的 blit + 位图拷贝，"
          "缩略图 %.1f ms）" % (cap, th))
    print("   ⚠️ 有一档窜到 179 ms —— 那是别的进程抢核，**取中位数别取单次**。")

    print("\n== 这条链的意义 ==")
    print("   探针（抓图 + 哈希）≈ %.0f ms" % probe_ms)
    print("   真读一屏（抓图 + OCR + 切行）≈ %.0f ms（下限，见 bench_probe_vs_ocr）"
          % read_ms)
    print("   => 探针比真读便宜 **约 %.0f 倍**。" % (read_ms / probe_ms))
    print("   1 秒一拍只做探针 = 常驻 %.1f%% 一个核"
          % (probe_ms / 1000 * 100))
    print("   对照 UIA 那条路一整拍约 100 ms（1 秒一拍 = 10%）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
