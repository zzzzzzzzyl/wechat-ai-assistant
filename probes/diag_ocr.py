# -*- coding: utf-8 -*-
"""OCR 慢到 10 秒/张，深挖根因。

已观测到的反常：
    - 一张 978x84 的小图，后续调用要 7.9~13 秒（首次 3.3 秒，后续反而更慢）
    - RapidOCR 自报分段只有 1.2ms  <- 与实测差 4 个数量级，说明它只统计了某一小段
    - 放大 3 倍、裁剪到 1/3 高度，耗时几乎不变（都 7~10 秒）<- 与计算量无关，可疑

如果耗时与图片大小基本无关，那瓶颈就**不是**推理计算，而是别的东西
（线程池调度 / 每次调用重建 session / 杀软扫描 / 输入格式转换）。

本脚本依次排查：
    1. 机器基线算力（numpy 矩阵乘，排除机器本身太慢）
    2. onnxruntime 版本、可用 provider、线程设置
    3. RapidOCR 可配置参数
    4. 手动分段计时（det / cls / rec 各自多久）
    5. 限制线程数后是否变快

用法：python probes/diag_ocr.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np


def sec(fn, n=1):
    t0 = time.perf_counter()
    for _ in range(n):
        r = fn()
    return (time.perf_counter() - t0) / n, r


def main():
    print("=" * 84)
    print("OCR 慢因诊断")
    print("=" * 84)

    # ---------- 1. 机器基线算力
    print("\n【1】机器基线算力（numpy）")
    a = np.random.rand(800, 800).astype(np.float32)
    b = np.random.rand(800, 800).astype(np.float32)
    t, _ = sec(lambda: a @ b, n=3)
    print("    800x800 float32 矩阵乘: %.1f ms  (正常机器应 < 80ms)" % (t * 1000))
    print("    CPU 核数: %s" % os.cpu_count())

    # ---------- 2. onnxruntime 环境
    print("\n【2】onnxruntime 环境")
    try:
        import onnxruntime as ort

        print("    onnxruntime 版本: %s" % ort.__version__)
        print("    可用 providers  : %s" % ort.get_available_providers())
        so = ort.SessionOptions()
        print("    默认 intra_op_num_threads: %s" % so.intra_op_num_threads)
        print("    默认 execution_mode      : %s" % so.execution_mode)
        print("    默认 graph_optimization  : %s" % so.graph_optimization_level)
    except Exception as e:
        print("    读取失败: %s" % e)

    # ---------- 3. RapidOCR 可配置参数
    print("\n【3】RapidOCR 支持的构造参数")
    try:
        import inspect

        from rapidocr_onnxruntime import RapidOCR

        sig = inspect.signature(RapidOCR.__init__)
        for name, p in sig.parameters.items():
            if name == "self":
                continue
            print("    %-28s 默认=%s" % (name, p.default))
    except Exception as e:
        print("    读取失败: %s" % e)

    # ---------- 4. 手动分段计时
    print("\n【4】手动分段计时（拿一张真实消息行图）")
    import glob

    from PIL import Image

    sample = None
    for pat in ("merge_shots/*.png", "compare_uia_ocr.png", "shots/*.png"):
        fs = sorted(glob.glob(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "out", pat)))
        if fs:
            sample = fs[0]
            break
    if not sample:
        print("    !! 没有样例图")
        return
    img = Image.open(sample).convert("RGB")
    arr = np.asarray(img)
    print("    样例: %s  尺寸 %s" % (os.path.basename(sample), img.size))

    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()

    # 4a 只做预处理
    try:
        t, _ = sec(lambda: engine.text_detector.preprocess(arr), n=3)
        print("    det 预处理         : %.1f ms" % (t * 1000))
    except Exception as e:
        print("    det 预处理失败: %s" % e)

    # 4b 只做检测
    det_out = None
    try:
        t, det_out = sec(lambda: engine.text_detector(arr), n=3)
        print("    det 完整           : %.1f ms" % (t * 1000))
    except Exception as e:
        print("    det 失败: %s" % e)

    # 4c 识别（若有检测框）
    try:
        if det_out and len(det_out) >= 2 and det_out[0] is not None:
            boxes = det_out[0]
            print("    检测到 %d 个框" % len(boxes))
            if len(boxes):
                t, _ = sec(lambda: engine.text_recognizer([arr]), n=3)
                print("    rec 完整(整图)     : %.1f ms" % (t * 1000))
        else:
            print("    没检测到框")
    except Exception as e:
        print("    rec 失败: %s" % e)

    # ---------- 5. 端到端重复测，看波动
    print("\n【5】端到端重复 5 次（看是否稳定，还是波动剧烈）")
    for i in range(5):
        t0 = time.perf_counter()
        engine(arr)
        print("    第 %d 次: %.0f ms" % (i + 1, (time.perf_counter() - t0) * 1000))

    print("\n" + "=" * 84)
    print("判读")
    print("=" * 84)
    print("  - 若矩阵乘很慢 -> 机器本身算力/降频问题")
    print("  - 若 det/rec 各自很快、但端到端很慢 -> 中间有额外开销（前后处理/线程调度）")
    print("  - 若每次都在 8~13 秒且波动大 -> 更像被外部因素干扰（杀软实时扫描/CPU 抢占）")


if __name__ == "__main__":
    main()
