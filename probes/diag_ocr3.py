# -*- coding: utf-8 -*-
"""验证：限制 onnxruntime 线程数能否同时解决「CPU 100%」和「卡顿」。

现象：
    - OCR 时任务管理器 CPU 冲到 100%
    - 系统卡顿约 1 秒，有时更长
    - 实测 det 一段就要 9~18 秒且波动剧烈（正常应该百毫秒级）

怀疑：
    onnxruntime 默认 intra_op_num_threads=0，即「用满所有核心」。
    本机 20 个逻辑核心，对一张小图开 20 个线程，
    线程创建/同步/伪共享的开销可能远超计算量 —— 既慢，又把 CPU 吃满导致整机卡顿。

做法：
    直接对 RapidOCR 用的两个模型（det / rec），
    在 intra_op_num_threads = 1/2/4/8/20/0 下各跑几次，对比推理耗时。

用法：python probes/diag_ocr3.py
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


def find_models():
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    import rapidocr_onnxruntime as r

    pkg = os.path.dirname(r.__file__)
    det = glob.glob(os.path.join(pkg, "models", "*det*infer.onnx"))
    rec = glob.glob(os.path.join(pkg, "models", "*rec*infer.onnx"))
    return (det[0] if det else None), (rec[0] if rec else None)


def bench(model_path, shape, thread_list, repeats=4):
    import onnxruntime as ort

    rows = []
    for nt in thread_list:
        so = ort.SessionOptions()
        if nt > 0:
            so.intra_op_num_threads = nt
            so.inter_op_num_threads = 1
        t0 = time.perf_counter()
        try:
            sess = ort.InferenceSession(model_path, so, providers=["CPUExecutionProvider"])
        except Exception as e:
            rows.append((nt, None, "建 session 失败: %s" % e))
            continue
        init_ms = (time.perf_counter() - t0) * 1000

        name = sess.get_inputs()[0].name
        inp = {name: np.random.rand(*shape).astype(np.float32)}

        try:
            sess.run(None, inp)          # warmup
            ts = []
            for _ in range(repeats):
                t0 = time.perf_counter()
                sess.run(None, inp)
                ts.append((time.perf_counter() - t0) * 1000)
            rows.append((nt, (init_ms, min(ts), sum(ts) / len(ts), max(ts)), None))
        except Exception as e:
            rows.append((nt, None, "推理失败: %s" % e))
    return rows


def main():
    print("=" * 88)
    print("onnxruntime 线程数对推理耗时的影响")
    print("=" * 88)
    print("逻辑核心数: %s" % os.cpu_count())

    det, rec = find_models()
    print("det 模型: %s" % (os.path.basename(det) if det else "未找到"))
    print("rec 模型: %s" % (os.path.basename(rec) if rec else "未找到"))

    threads = [1, 2, 4, 8, 20, 0]

    for label, model, shape in (
        ("det (检测)", det, (1, 3, 64, 320)),
        ("rec (识别)", rec, (1, 3, 48, 320)),
    ):
        if not model:
            continue
        print("\n" + "-" * 88)
        print("【%s】输入形状 %s" % (label, shape))
        print("-" * 88)
        print("  %-10s %-12s %-10s %-10s %-10s" % ("线程数", "session初始化", "最快", "平均", "最慢"))
        for nt, r, err in bench(model, shape, threads):
            tag = "自动(0)" if nt == 0 else str(nt)
            if err:
                print("  %-10s %s" % (tag, err))
            else:
                init_ms, mn, avg, mx = r
                print("  %-10s %-12s %-10s %-10s %-10s"
                      % (tag, "%.0f ms" % init_ms, "%.1f ms" % mn, "%.1f ms" % avg, "%.1f ms" % mx))

    print("\n" + "=" * 88)
    print("判读")
    print("=" * 88)
    print("  若「线程数=1 或 2」比「自动(0)」明显更快 -> 多线程在小输入上纯属负优化，")
    print("  限线程既能提速、又能把 CPU 占用降下来（解决整机卡顿）。")
    print("  若差异不大 -> 瓶颈不在这里，需要另找原因。")


if __name__ == "__main__":
    main()
