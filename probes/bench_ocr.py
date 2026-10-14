# -*- coding: utf-8 -*-
"""OCR 性能基准：定位「每次 OCR 电脑卡一下」的瓶颈在哪。

怀疑的几个原因：
    1. 引擎初始化（加载 ONNX 模型）每次都要 1~3 秒
    2. 首次推理比后续慢（onnxruntime 要做图优化）
    3. 图上放大 3 倍反而浪费 —— RapidOCR 内部会把长边缩到 det_limit_side_len，
       放大等于白做，还可能让小字更糊
    4. 对整行做 OCR，其实只需要行上部那一小条（昵称区）

做法：对同一张真实消息行截图，分别测
    - 引擎初始化耗时
    - 首次 vs 后续调用耗时
    - 不同放大倍数
    - 只裁昵称区（行上部）
并把 RapidOCR 自己给出的 det/cls/rec 分段时间打出来。

用法：python probes/bench_ocr.py
"""

import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
from PIL import Image

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")


def pick_sample():
    """挑一张真实的消息行截图。"""
    for pat in ("merge_shots/*.png", "compare_uia_ocr.png", "shots/*.png", "wechat_shot_*.png"):
        files = sorted(glob.glob(os.path.join(OUT, pat)))
        if files:
            return files[0]
    return None


def resize_to(img, scale):
    if scale == 1.0:
        return img
    return img.resize((int(img.width * scale), int(img.height * scale)), Image.BICUBIC)


def timed_call(engine, arr, n=5):
    """返回 (后续调用耗时列表ms, 首次耗时ms, elapse 明细)"""
    t0 = time.perf_counter()
    res, elapse = engine(arr)
    first = (time.perf_counter() - t0) * 1000
    rest = []
    for _ in range(n - 1):
        t0 = time.perf_counter()
        engine(arr)
        rest.append((time.perf_counter() - t0) * 1000)
    return first, rest, elapse, res


def main():
    sample = pick_sample()
    print("=" * 84)
    print("OCR 性能基准")
    print("=" * 84)
    if not sample:
        print("!! 没找到可用的截图样例（先跑一次 merge_chat_ocr.py 或 probe_ocr.py）")
        return
    print("样例图片: %s" % sample)
    img0 = Image.open(sample).convert("RGB")
    print("原始尺寸: %s" % (img0.size,))

    # ---------- 1. 引擎初始化
    print("\n【1】引擎初始化（加载 ONNX 模型）")
    from rapidocr_onnxruntime import RapidOCR

    t0 = time.perf_counter()
    engine = RapidOCR()
    init_ms = (time.perf_counter() - t0) * 1000
    print("    RapidOCR() 耗时: %.0f ms   <-- 每新建一个引擎就要付一次" % init_ms)

    # 看看内部配置里能拿到什么
    try:
        cfg = engine.text_detector.session.get_session_options() if hasattr(engine, "text_detector") else None
        print("    (内部 session 可访问: %s)" % (cfg is not None))
    except Exception:
        pass

    # ---------- 2. 首次 vs 后续
    print("\n【2】首次调用 vs 后续调用（同一张图，整行不放大）")
    arr1 = np.asarray(img0)
    first, rest, elapse, res = timed_call(engine, arr1, n=5)
    print("    首次 : %.0f ms   <-- 含 onnxruntime 图优化" % first)
    if rest:
        print("    后续 : 最小 %.0f / 平均 %.0f / 最大 %.0f ms"
              % (min(rest), sum(rest) / len(rest), max(rest)))
    try:
        parts = np.array(elapse[0] if isinstance(elapse, list) and elapse else elapse).flatten()
        print("    引擎自报分段(ms): %s" % [round(float(x), 1) for x in parts])
    except Exception:
        pass
    print("    识别到 %d 个文本块" % (len(res) if res else 0))

    # ---------- 3. 不同放大倍数
    print("\n【3】不同放大倍数（同一张行图）")
    print("    %-8s %-14s %-12s %s" % ("scale", "图片尺寸", "平均耗时", "识别块数"))
    for scale in (1.0, 1.5, 2.0, 3.0):
        im = resize_to(img0, scale)
        arr = np.asarray(im)
        try:
            _, rest2, _, res2 = timed_call(engine, arr, n=4)
            avg = sum(rest2) / len(rest2) if rest2 else 0
            print("    %-8s %-14s %-12s %s"
                  % (scale, "%dx%d" % im.size, "%.0f ms" % avg, len(res2) if res2 else 0))
        except Exception as e:
            print("    %-8s 失败: %s" % (scale, e))

    # ---------- 4. 只裁昵称区（行上半部分）
    print("\n【4】只 OCR「昵称区」= 行图的上半部分（群聊昵称所在位置）")
    h = img0.height
    print("    %-26s %-14s %-12s %s" % ("裁剪区域", "图片尺寸", "平均耗时", "识别块数"))
    for frac in (0.35, 0.5, 1.0):
        crop = img0.crop((0, 0, img0.width, max(8, int(h * frac))))
        arr = np.asarray(crop)
        try:
            _, rest3, _, res3 = timed_call(engine, arr, n=4)
            avg = sum(rest3) / len(rest3) if rest3 else 0
            label = "整行" if frac >= 1.0 else ("上部 %.0f%%" % (frac * 100))
            print("    %-26s %-14s %-12s %s"
                  % (label, "%dx%d" % crop.size, "%.0f ms" % avg, len(res3) if res3 else 0))
        except Exception as e:
            print("    %-26s 失败: %s" % (label if frac >= 1 else "裁剪", e))

    # ---------- 5. 结论
    print("\n" + "=" * 84)
    print("判读")
    print("=" * 84)
    print("  - 若『引擎初始化』耗时很大：主程序必须复用同一个引擎，不能每次 OCR 都新建")
    print("  - 若『首次 vs 后续』差距大：启动时做一次预热 (warmup) 即可消除首卡")
    print("  - 若放大反而更慢/更差：说明放大是白费，RapidOCR 内部会自行缩放")
    print("  - 若「上部 35%%」明显更快且仍能识别出昵称：只裁昵称区做 OCR")


if __name__ == "__main__":
    main()
