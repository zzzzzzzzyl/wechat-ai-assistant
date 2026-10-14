# -*- coding: utf-8 -*-
"""OCR 慢因诊断（第二层）：分段计时 + session 配置来源。

已排除：
    - 机器算力不足（800x800 矩阵乘仅 4.3ms，20 核）
    - AzureExecutionProvider 被误用（实际 providers = ['CPUExecutionProvider']）
    - 图片尺寸（裁到 1/3 高度，耗时几乎不变）

剩下要查：
    1. 到底是 det / cls / rec 哪一段慢
    2. session 是怎么创建的（线程数、执行模式、是否有 per-call 的额外开销）

用法：python probes/diag_ocr2.py
"""

import glob
import inspect
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sample_image():
    for pat in ("merge_shots/*.png", "compare_uia_ocr.png", "shots/*.png"):
        fs = sorted(glob.glob(os.path.join(ROOT, "out", pat)))
        if fs:
            return fs[0]
    return None


def timed(fn, n=4, label=""):
    ts = []
    out = None
    for i in range(n):
        t0 = time.perf_counter()
        out = fn()
        ts.append((time.perf_counter() - t0) * 1000)
    print("    %-22s 各次: %s" % (label, " ".join("%.0f" % t for t in ts)))
    return out


def main():
    p = sample_image()
    if not p:
        print("!! 没有样例图")
        return
    img = Image.open(p).convert("RGB")
    arr = np.asarray(img)
    print("=" * 84)
    print("OCR 慢因诊断 第二层")
    print("=" * 84)
    print("样例: %s  尺寸 %s" % (os.path.basename(p), img.size))

    from rapidocr_onnxruntime import RapidOCR

    e = RapidOCR()
    print("\n引擎内部对象: text_det=%s, text_cls=%s, text_rec=%s"
          % (type(e.text_det).__name__, type(e.text_cls).__name__, type(e.text_rec).__name__))

    print("\n【1】分段计时（单位 ms，每段连测 4 次）")
    det_out = timed(lambda: e.text_det(arr), label="text_det(整图)")
    boxes = None
    if isinstance(det_out, tuple) and det_out and det_out[0] is not None:
        boxes = det_out[0]
    elif det_out is not None:
        boxes = det_out
    nbox = len(boxes) if boxes is not None and hasattr(boxes, "__len__") else 0
    print("      -> 检测到 %d 个文本框" % nbox)

    if nbox:
        try:
            crops = e.get_crop_img_list(arr, boxes)
            print("      -> 裁出 %d 个候选图" % len(crops))
            timed(lambda: e.text_cls(crops), label="text_cls(方向分类)")
            timed(lambda: e.text_rec(crops), label="text_rec(识别)")
        except Exception as ex:
            print("      cls/rec 分段失败: %s: %s" % (type(ex).__name__, ex))

    print("\n【2】端到端（RapidOCR.__call__）")
    timed(lambda: e(arr), label="完整调用")

    print("\n【3】OrtInferSession 是怎么建 session 的（看线程/执行模式）")
    try:
        cls = type(e.text_rec.session)
        src = inspect.getsource(cls.__init__)
        for line in src.splitlines():
            s = line.strip()
            if any(k in s for k in ("InferenceSession", "SessionOptions", "providers",
                                    "intra_op", "inter_op", "sess_options", "execution_mode")):
                print("    %s" % s)
    except Exception as ex:
        print("    读取失败: %s" % ex)

    print("\n【4】onnxruntime 层面直接建一个 session 看初始化耗时")
    try:
        import onnxruntime as ort

        model = None
        for attr in ("text_det", "text_rec"):
            obj = getattr(e, attr)
            sess = getattr(obj, "session", None)
            inner = getattr(sess, "session", None)
            if inner is not None:
                try:
                    model = inner._model_path
                except Exception:
                    pass
        print("    找到模型路径: %s" % model)
        if model:
            t0 = time.perf_counter()
            so = ort.SessionOptions()
            s2 = ort.InferenceSession(model, so, providers=["CPUExecutionProvider"])
            print("    InferenceSession 初始化: %.0f ms" % ((time.perf_counter() - t0) * 1000))
            print("    实际 providers: %s" % s2.get_providers())
            # 连续跑几次，看单次推理耗时
            names = [i.name for i in s2.get_inputs()]
            print("    输入: %s" % names)
    except Exception as ex:
        print("    失败: %s: %s" % (type(ex).__name__, ex))

    print("\n" + "=" * 84)
    print("判读")
    print("=" * 84)
    print("  - 若某一段独占了几秒 -> 瓶颈就是它（det 通常是大头）")
    print("  - 若三段都很快、端到端也快 -> 那第一次跑的 10 秒来自别处（可能是偶发）")
    print("  - 关注 OrtInferSession 是否每次都重新创建 session（那会很慢）")


if __name__ == "__main__":
    main()
