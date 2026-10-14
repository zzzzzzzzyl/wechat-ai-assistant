# -*- coding: utf-8 -*-
"""第二个性能坑：Det 的 limit_type=min 会把扁图放大到离谱尺寸。

怀疑：
    默认配置 Det.limit_side_len=736 / limit_type=min 表示「保证短边 >= 736」。
    聊天消息行是极扁的图（978x84），短边只有 84，
    于是会被放大 8.76 倍 -> 8571x736 送进检测模型，计算量爆炸。

本脚本：
    1. 用 patch 的方式把实际送进 det 的张量形状打出来（眼见为实）
    2. 生成若干自定义 config（绝对路径 + 不同 limit 设置），对比耗时
    3. 顺便对比线程数设置

用法：python probes/test_ocr_det_limit.py
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")


def sample_image():
    for pat in ("merge_shots/*.png", "compare_uia_ocr.png", "shots/*.png"):
        fs = sorted(glob.glob(os.path.join(OUT, pat)))
        if fs:
            return fs[0]
    return None


def make_config(name, intra=4, limit_side_len=736, limit_type="min", use_cls=True):
    """基于包内默认 config 生成一份自定义配置（模型路径改成绝对路径）。"""
    import rapidocr_onnxruntime as rr

    pkg = os.path.dirname(rr.__file__)
    with open(os.path.join(pkg, "config.yaml"), "r", encoding="utf-8") as f:
        txt = f.read()

    txt = txt.replace("&intra_nums -1", "&intra_nums %d" % intra)
    txt = txt.replace("&inter_nums -1", "&inter_nums 1")
    txt = txt.replace("limit_side_len: 736", "limit_side_len: %d" % limit_side_len)
    txt = txt.replace("limit_type: min", "limit_type: %s" % limit_type)
    if not use_cls:
        txt = txt.replace("use_cls: true", "use_cls: false")

    # 模型路径 -> 绝对路径（用 config_path 指定外部文件时，相对路径会解析错）
    models_dir = os.path.join(pkg, "models")
    for m in ("ch_PP-OCRv4_det_infer.onnx",
              "ch_ppocr_mobile_v2.0_cls_infer.onnx",
              "ch_PP-OCRv4_rec_infer.onnx"):
        txt = txt.replace("models/%s" % m, os.path.join(models_dir, m).replace("\\", "/"))

    path = os.path.join(OUT, "cfg_%s.yaml" % name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    return path


def bench(engine, arr, warmup=1, repeats=3, spy_shape=False):
    shapes = []
    if spy_shape:
        # 正确的方法名是 preprocess_op（det 内部做缩放/padding 的地方）
        orig = engine.text_det.preprocess_op

        def patched(img, *a, **kw):
            out = orig(img, *a, **kw)
            try:
                cand = out if isinstance(out, (tuple, list)) else [out]
                for o in cand:
                    s = getattr(o, "shape", None)
                    if s is not None:
                        shapes.append(tuple(s))
                        break
            except Exception:
                pass
            return out

        engine.text_det.preprocess_op = patched

    for _ in range(warmup):
        engine(arr)
    ts = []
    res = None
    for _ in range(repeats):
        t0 = time.perf_counter()
        res, _ = engine(arr)
        ts.append((time.perf_counter() - t0) * 1000)
    return min(ts), sum(ts) / len(ts), max(ts), (len(res) if res else 0), shapes


def main():
    p = sample_image()
    if not p:
        print("!! 没有样例图")
        return
    img = Image.open(p).convert("RGB")
    arr = np.asarray(img)
    print("=" * 92)
    print("Det limit_type / limit_side_len 对性能的影响")
    print("=" * 92)
    print("样例: %s  尺寸 %s" % (os.path.basename(p), img.size))

    from rapidocr_onnxruntime import RapidOCR

    cases = [
        ("默认(limit=736,min,intra=-1)", None),
        ("intra=4, limit=736,min", make_config("a", 4, 736, "min")),
        ("intra=4, limit=736,max", make_config("b", 4, 736, "max")),
        ("intra=4, limit=320,max", make_config("c", 4, 320, "max")),
        ("intra=4, limit=160,max", make_config("d", 4, 160, "max")),
    ]

    print("\n" + "-" * 92)
    print("%-30s %-10s %-10s %-10s %-8s %s" % ("配置", "最快", "平均", "最慢", "识别块", "det实际输入"))
    print("-" * 92)
    for name, cfg_path in cases:
        try:
            if cfg_path is None:
                eng = RapidOCR()
            else:
                eng = RapidOCR(config_path=cfg_path)
            mn, avg, mx, nblk, shapes = bench(eng, arr, spy_shape=True)
            sh = shapes[0] if shapes else None
            print("%-30s %-10s %-10s %-10s %-8s %s"
                  % (name, "%.0f ms" % mn, "%.0f ms" % avg, "%.0f ms" % mx, nblk, sh))
        except Exception as e:
            print("%-30s 失败: %s: %s" % (name, type(e).__name__, e))

    print("\n" + "=" * 92)
    print("判读")
    print("=" * 92)
    print("  - 看 det 实际输入那一列：如果是 1x3x7xx x 8xxx 这种畸形尺寸，")
    print("    就证实了「短边放大」把扁图撑成了巨图，这才是主要耗时来源。")
    print("  - 换成 limit_type=max 后尺寸应回到 1x3x63x736 量级，耗时大幅下降。")
    print("  - 同时对比识别块数：如果识别结果变少/变差，说明限制过度，需要折中。")


if __name__ == "__main__":
    main()
