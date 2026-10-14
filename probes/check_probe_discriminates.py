# -*- coding: utf-8 -*-
"""验证「便宜探针」的哈希**分得开不同的屏、又扛得住微小抖动**。

    python probes/check_probe_discriminates.py

为什么需要这个
--------------
`ocr_read.probe` 的整套逻辑建立在「抓图 + 缩略图哈希能判断屏幕变没变」之上。
单测里 `probe()` 是**假后端**（返回 b"a" / b"b"），验的是**调用逻辑**，
验不了"这个哈希在真实像素上到底判不判得准"。这个脚本补那一半。

四个判据
--------
1. **稳定性**：同一屏连抓 N 次，哈希必须**完全一致**
   （否则会每拍都"变了"→ 每拍都真读 250ms，等于探针白做）
2. **区分度**：两张不同的真实截图，哈希必须**不同**
   （否则翻页不被发现，整个探针没意义）
3. **灵敏度**：往真实截图上**贴一条合成消息**（模拟"来新消息/翻了一屏"），
   逐档减小高度，看**最小能测出多高的一条** —— 这个数决定它抓不抓得住
   快速滑动里滑过去的那一屏
4. **抗抖动**：整体加一点噪声（模拟 OCR 前的抗锯齿/缩放抖动），
   看会不会被误判成"变了"

⚠️ 全部在内存里对位图做操作，**不碰微信窗口、不改它任何状态**。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import dpi as _dpi  # noqa: E402

_dpi.enable()

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from wechat_assistant import capture  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.ocr_reader import OcrReader, probe_changed  # noqa: E402
from wechat_assistant.reader import list_windows, pick_window  # noqa: E402


def cfg_bits(cfg):
    """把配置里的容差参数取出来给 `probe_changed` 用。"""
    return int(cfg.get("ocr_read.probe_tol", 8)), float(
        cfg.get("ocr_read.probe_changed", 0.01) or 0.01)


def _pc(cfg, a, b):
    tol, frac = cfg_bits(cfg)
    return probe_changed(a, b, tol, frac)


def make_reader(cfg, img):
    """把抓图换成给定的位图 —— 这样 probe() 里那套**真实几何 + 真实哈希**照跑。"""
    rd = OcrReader(cfg)
    rd.grab = lambda _h: img
    return rd


def main():
    cfg = load_config()
    win = pick_window(list_windows(cfg), cfg)
    if win is None:
        print("挑不出微信主窗口（先确认微信登录着、有正常窗口）")
        return 1
    live = capture.capture_window(win["hwnd"])
    if live is None or capture.is_blank(live):
        print("**PrintWindow 抓到空白** —— 先把微信恢复出来再跑")
        return 1
    print("实时窗口 %dx%d  thumb=%s" % (live.width, live.height,
                                     cfg.get("ocr_read.probe_thumb", 48)))

    rd = make_reader(cfg, live)
    ok = True

    # ---- 1. 稳定性
    hs = [rd.probe(0) for _ in range(6)]
    same = all(h == hs[0] for h in hs) and hs[0] is not None
    print("\n1. 稳定性：同一屏连探 6 次 -> %s（%d 字节）"
          % ("完全一致 ✅" if same else "**不一致 ✗**", len(hs[0] or b"")))
    ok = ok and same

    # ---- 2. 区分度：一张真实老截图
    other = None
    for p in ("out/compare_uia_ocr.png", "out/v2_pw_20260923_010346.png"):
        f = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), p)
        if os.path.exists(f):
            other = Image.open(f).convert("RGB")
            print("   （拿 %s 当「另一屏」，%dx%d）" % (p, other.width, other.height))
            break
    if other is not None:
        h2 = make_reader(cfg, other).probe(0)
        diff = h2 != hs[0]
        print("2. 区分度：另一张截图的哈希%s ✅" % ("不同" if diff else "**居然相同 ✗**"))
        ok = ok and diff
    else:
        print("2. 区分度：**跳过**（out/ 里没有可用的老截图）")

    # ---- 3. 灵敏度：贴一条合成消息，逐档减小
    #
    # 模拟的是「来了一条新消息」：在聊天区底部往上贴一条横条。
    # ⚠️ 颜色取聊天区底色的补色方向，保证和底色有足够对比。
    print("\n3. 灵敏度：往聊天区里贴一条合成消息，看最小能测出多高")
    scale = rd._scale()
    from wechat_assistant.ocr_reader import chat_rect
    x1, y1, x2, y2 = chat_rect((0, 0, live.width, live.height), scale, cfg,
                               (live.width, live.height))
    a0 = np.asarray(live.convert("RGB")).astype(np.uint8).copy()
    # ⚠️ **必须贴在探针的裁剪区里面**。探针只覆盖「标题栏 + 聊天区」，
    #    下界是 `_input_top`（不含输入框）。第一版拿 `y2`（= 窗口底边）
    #    当底边往上贴，结果整条消息落在裁剪区**外面**，于是"连 80px 都测不出来"
    #    —— 那是**脚本的错**，不是探针的错。
    bg = rd._bg(np.asarray(live.convert("RGB")).astype(np.int16), x1, x2, y1)
    crop_bottom = rd._input_top(np.asarray(live.convert("RGB")).astype(np.int16),
                                bg, live.height, x1, x2, y1)
    print("   聊天区 %s -> 探针裁剪下界 y=%d（%dx%d）"
          % ((x1, y1, x2, y2), crop_bottom, x2 - x1, crop_bottom))
    smallest = None
    for h in (4, 8, 12, 16, 24, 32, 48, 80):
        img = Image.fromarray(a0.copy())
        d = ImageDraw.Draw(img)
        # 贴一条贴近裁剪区底部、但**留在裁剪区内**的消息
        top = max(y1 + 1, crop_bottom - h - 6)
        d.rectangle([x1 + 20, top, x2 - 20, top + h], fill=(200, 200, 200))
        hh = make_reader(cfg, img).probe(0)
        hit = _pc(cfg, hs[0], hh) if hh else False
        print("      高 %3d px -> %s" % (h, "测出来了" if hit else "**没测出来**"))
        if hit and smallest is None:
            smallest = h
            break
    if smallest is None:
        print("   ⚠️ 连 80px 高的消息都测不出来 —— 探针参数有问题")
        ok = False
    else:
        print("   => 最小可测高度 = %d px（一条消息气泡通常 40~90px，够用）" % smallest)

    # ---- 4. 抗抖动：整体加噪声
    print("\n4. 抗抖动：在位图上加随机噪声，看会不会被误判成「变了」")
    # ⚠️ **每种幅度要试多个种子**。第一版每种幅度只试一次，结果是
    #    「±10 触发、±20 不触发」—— **非单调**，那是"一次随机实现"的噪音，
    #    不是结论（降采样会把噪声平均掉，所以幅度越大通常越不容易触发，
    #    但单次仍可能翻转）。改成每种幅度试 5 次、报"触发几次"。
    rng = np.random.default_rng(20261010)
    bad = []
    for amp in (2, 5, 10, 20):
        hits = 0
        for _ in range(5):
            noisy = np.clip(
                a0.astype(np.int16)
                + rng.integers(-amp, amp + 1, a0.shape[:2])[:, :, None],
                0, 255).astype(np.uint8)
            hh = make_reader(cfg, Image.fromarray(noisy)).probe(0)
            if hh and _pc(cfg, hs[0], hh):
                hits += 1
        print("      噪声幅度 ±%-3d -> 5 次里触发 %d 次%s"
              % (amp, hits, "  ✗ 太敏感" if amp <= 2 and hits else ""))
        if amp <= 2 and hits:
            bad.append(amp)
    if bad:
        print("   ✗ 连 ±%s 的抖动都会触发 —— 微信里一点抗锯齿就会让探针每拍命中"
              % bad[0])
        ok = False
    else:
        print("   => ±2 的抖动 5 次都不触发（真实抗锯齿/缩放抖动就在这个量级）")

    print("\n结论: %s" % ("探针的哈希可用" if ok else "**探针有问题，别开**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
