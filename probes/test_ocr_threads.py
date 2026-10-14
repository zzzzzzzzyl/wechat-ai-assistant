# -*- coding: utf-8 -*-
"""验证并确定 OCR 的线程数配置方式。

根因（已定位）：
    rapidocr 的默认 config.yaml 里 intra_op_num_threads = -1，
    表示「不设置」，于是 onnxruntime 用自动模式（intra_op_num_threads=0）。
    实测该模式在本机是性能灾难：
        rec 模型 显式设线程 = 10~28 ms  /  自动模式 = 233 ms（最慢 434ms）
        session 初始化       152 ms     /  自动模式 = 1528 ms
    而且自动模式会把全部 20 个核心拉起来 -> 任务管理器 CPU 100% -> 整机卡顿。

本脚本测试三种把线程数传进去的方式，挑出可行的那一种：
    A. RapidOCR(intra_op_num_threads=N)
    B. 自定义 config.yaml（复制默认配置改线程数，用 config_path 传入）
    C. 关闭 use_cls（方向分类，正常聊天截图用不上，可省时间）

用法：python probes/test_ocr_threads.py
"""

import glob
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sample_image():
    for pat in ("merge_shots/*.png", "compare_uia_ocr.png", "shots/*.png"):
        fs = sorted(glob.glob(os.path.join(ROOT, "out", pat)))
        if fs:
            return fs[0]
    return None


def bench(engine, arr, warmup=1, repeats=3):
    for _ in range(warmup):
        engine(arr)
    ts = []
    res = None
    for _ in range(repeats):
        t0 = time.perf_counter()
        res, _ = engine(arr)
        ts.append((time.perf_counter() - t0) * 1000)
    return min(ts), sum(ts) / len(ts), max(ts), (len(res) if res else 0)


def main():
    p = sample_image()
    if not p:
        print("!! 没有样例图")
        return
    arr = np.asarray(Image.open(p).convert("RGB"))
    print("=" * 90)
    print("OCR 线程数配置方式验证")
    print("=" * 90)
    print("样例: %s  尺寸 %s   逻辑核心 %s"
          % (os.path.basename(p), Image.open(p).size, os.cpu_count()))

    from rapidocr_onnxruntime import RapidOCR
    import rapidocr_onnxruntime as rr

    pkg_dir = os.path.dirname(rr.__file__)
    default_cfg = os.path.join(pkg_dir, "config.yaml")

    cases = []

    # A. 默认（对照组）
    cases.append(("A. 默认（intra=-1，自动）", lambda: RapidOCR()))

    # B. kwargs 传线程数
    for n in (2, 4):
        cases.append(("B. kwargs intra_op_num_threads=%d" % n,
                      (lambda nn: (lambda: RapidOCR(intra_op_num_threads=nn)))(n)))

    # C. 自定义 config.yaml
    custom = os.path.join(ROOT, "out", "rapidocr_custom.yaml")
    try:
        os.makedirs(os.path.dirname(custom), exist_ok=True)
        with open(default_cfg, "r", encoding="utf-8") as f:
            txt = f.read()
        txt2 = txt.replace("&intra_nums -1", "&intra_nums 4").replace("&inter_nums -1", "&inter_nums 1")
        with open(custom, "w", encoding="utf-8") as f:
            f.write(txt2)
        print("\n已生成自定义配置: %s" % custom)
        cases.append(("C. 自定义 config.yaml(intra=4)", lambda: RapidOCR(config_path=custom)))
    except Exception as e:
        print("生成自定义配置失败: %s" % e)

    # D. 关掉方向分类
    cases.append(("D. kwargs intra=4 + use_cls=False",
                  lambda: RapidOCR(intra_op_num_threads=4, use_cls=False)))

    print("\n" + "-" * 90)
    print("%-40s %-10s %-10s %-10s %-8s %s" % ("配置", "初始化", "最快", "平均", "识别块", "是否生效"))
    print("-" * 90)

    base_avg = None
    for name, factory in cases:
        try:
            t0 = time.perf_counter()
            eng = factory()
            init_ms = (time.perf_counter() - t0) * 1000
            mn, avg, mx, nblk = bench(eng, arr)
            if base_avg is None:
                base_avg = avg
            speed = ""
            if base_avg and avg:
                speed = "快 %.1fx" % (base_avg / avg) if avg < base_avg else "慢 %.1fx" % (avg / base_avg)
            print("%-40s %-10s %-10s %-10s %-8s %s"
                  % (name, "%.0f ms" % init_ms, "%.0f ms" % mn, "%.0f ms" % avg, nblk, speed))
        except Exception as e:
            print("%-40s 失败: %s: %s" % (name, type(e).__name__, e))

    print("\n" + "=" * 90)
    print("结论要点")
    print("=" * 90)
    print("  - 哪一行明显快，就说明那个传参方式生效了")
    print("  - 目标：单次 OCR 从数秒降到百毫秒级，且不再吃满 20 个核心")
    print("  - 最终参数会写进项目配置，主程序统一使用")


if __name__ == "__main__":
    main()
