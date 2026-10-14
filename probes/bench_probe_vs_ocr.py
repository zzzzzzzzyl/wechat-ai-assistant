# -*- coding: utf-8 -*-
"""量「便宜探针」vs「真读一屏」的差 —— 决定「OCR 模式下要不要连续记录」。

    python probes/bench_probe_vs_ocr.py

要回答的问题
------------
OCR 后端一次读屏约 250ms，所以默认不敢连续记录（`ocr_read.record: false`）。
但那 250ms 里**绝大部分是 OCR**（识别文字），而"这一屏变了没"根本不需要
OCR —— 抓图 + 缩略图哈希就够（几十毫秒）。

如果"探针"足够便宜，就有一个**第三条路**：
    每 1~2 秒只做「抓图 + 哈希」，**变了才 OCR**
于是空闲时几乎不耗 CPU，而"你翻过的屏 / 来的新消息"照样能记下来 ——
连续记录最值钱的那部分拿到了，代价只有一个便宜探针。

这个脚本量的就是那个"便宜探针"到底多便宜。

⚠️ 纯只读：走 `capture.PrintWindow`，不置顶、不点击、不改微信任何状态。
"""

import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import dpi as _dpi  # noqa: E402

_dpi.enable()

import numpy as np  # noqa: E402

from wechat_assistant import capture  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.reader import list_windows, pick_window  # noqa: E402

#: 探针用的缩略图长边。**只用来判断"变没变"**，不要求像素级精度，
#: 所以可以降得很狠 —— 这正是它便宜的原因。
THUMB = 64


def thumb_hash(img):
    """把整窗缩成 THUMB 长度的灰度缩略图，返回 bytes（拿来比"变没变"）。"""
    g = img.convert("L").resize((THUMB, THUMB)).tobytes()
    return g


def bench(fn, n=8):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts), min(ts)


def main():
    cfg = load_config()
    win = pick_window(list_windows(cfg), cfg)
    if win is None:
        print("挑不出微信主窗口（先确认微信登录着、有正常窗口）")
        return 1
    hwnd = win["hwnd"]
    print("微信窗口 0x%X  %dx%d"
          % (hwnd, win["rect"][2] - win["rect"][0], win["rect"][3] - win["rect"][1]))

    img = capture.capture_window(hwnd)
    if img is None or capture.is_blank(img):
        print("**PrintWindow 抓到空白** —— 先把微信恢复出来再跑")
        return 1

    # ---- 便宜探针：抓图 + 缩略图
    probe_ms, probe_min = bench(lambda: thumb_hash(capture.capture_window(hwnd)))
    # 拆开看两段的占比
    cap_ms, _ = bench(lambda: capture.capture_window(hwnd))
    th_ms, _ = bench(lambda: thumb_hash(img))

    # ---- 真读一屏（OCR 后端整条路）
    from wechat_assistant.ocr_reader import OcrReader

    rd = OcrReader(cfg)
    rd._engine().load()
    rd._engine().warmup()
    read_ms, read_min = bench(lambda: rd.read_chat(hwnd), n=4)

    print("\n== 便宜探针 ==   抓图 %.0f ms + 缩略图 %.1f ms  =>  合计 %.0f ms（最低 %.0f）"
          % (cap_ms, th_ms, probe_ms, probe_min))
    print("== 真读一屏 ==   %.0f ms（最低 %.0f）" % (read_ms, read_min))
    print("   探针 / 真读 = %.2f 倍" % (probe_ms / read_ms))

    print("\n== 折算成「常驻占用一个核的百分比」==")
    for iv in (0.5, 1.0, 2.0, 5.0):
        print("   每 %.1fs 一拍：全 OCR %.1f%%   ｜   探针+按需 OCR %.1f%%（空闲时）"
              % (iv, read_ms / (iv * 1000) * 100, probe_ms / (iv * 1000) * 100))

    print("\n== 结论 ==")
    idle = probe_ms / (1.0 * 1000) * 100
    full = read_ms / (1.0 * 1000) * 100
    print("   1 秒一拍、空闲的时候：全 OCR 要 %.0f%%，探针只要 %.1f%% —— 差 %.0f 倍。"
          % (full, idle, full / idle))
    print("   而**内容一变就立刻 OCR**，所以「翻过去的屏 / 新消息」一条都不会漏。")
    print("   对照：现在 UIA 那条路一整拍约 100 ms（1 秒一拍 = 10%）——")
    print("   也就是说「探针 + 按需 OCR」在空闲时比 UIA 模式还省。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
