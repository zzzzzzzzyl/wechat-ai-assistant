# -*- coding: utf-8 -*-
"""验证优化配置没有牺牲识别质量。

速度优化有两个手段，但都可能影响准确率：
    1. 限制 onnxruntime 线程数（intra_op_num_threads=4）—— 只影响速度，理论上不影响结果
    2. 改 det 的 limit_type=min -> max、并调小 limit_side_len —— 会改变喂给模型的图片尺寸，
       如果缩得太小，小字会糊掉、识别不出来

本脚本对所有真实消息行截图，用不同配置各跑一遍，把识别出的文本并排打出来对比。

用法：python probes/verify_ocr_quality.py
"""

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")


def make_config(name, intra=4, limit_side_len=736, limit_type="max"):
    import rapidocr_onnxruntime as rr

    pkg = os.path.dirname(rr.__file__)
    with open(os.path.join(pkg, "config.yaml"), "r", encoding="utf-8") as f:
        txt = f.read()
    txt = txt.replace("&intra_nums -1", "&intra_nums %d" % intra)
    txt = txt.replace("&inter_nums -1", "&inter_nums 1")
    txt = txt.replace("limit_side_len: 736", "limit_side_len: %d" % limit_side_len)
    txt = txt.replace("limit_type: min", "limit_type: %s" % limit_type)
    models = os.path.join(pkg, "models")
    for m in ("ch_PP-OCRv4_det_infer.onnx",
              "ch_ppocr_mobile_v2.0_cls_infer.onnx",
              "ch_PP-OCRv4_rec_infer.onnx"):
        txt = txt.replace("models/%s" % m, os.path.join(models, m).replace("\\", "/"))
    path = os.path.join(OUT, "qc_%s.yaml" % name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    return path


def texts_of(engine, arr):
    res, _ = engine(arr)
    if not res:
        return []
    return [t for _, t, _ in res]


def main():
    files = sorted(glob.glob(os.path.join(OUT, "merge_shots", "*.png")))
    if not files:
        print("!! 没有 merge_shots 样例（先跑一次 merge_chat_ocr.py）")
        return
    print("=" * 96)
    print("识别质量对比（%d 张真实消息行截图）" % len(files))
    print("=" * 96)

    from rapidocr_onnxruntime import RapidOCR

    configs = [
        ("上限736(max)", make_config("736max", 4, 736, "max")),
        ("上限320(max)", make_config("320max", 4, 320, "max")),
        ("上限512(max)", make_config("512max", 4, 512, "max")),
    ]
    engines = []
    for name, cfg in configs:
        engines.append((name, RapidOCR(config_path=cfg)))

    agree = {name: 0 for name, _ in engines}
    total = 0
    for f in files:
        img = Image.open(f).convert("RGB")
        arr = np.asarray(img)
        total += 1
        print("\n--- %s  尺寸 %s ---" % (os.path.basename(f), img.size))
        baseline = None
        for name, eng in engines:
            try:
                tx = texts_of(eng, arr)
            except Exception as e:
                print("  %-14s 失败: %s" % (name, e))
                continue
            if baseline is None:
                baseline = tx
            mark = "  " if tx == baseline else "≠ "
            print("  %-14s %s%r" % (name, mark, tx))
            if tx == baseline:
                agree[name] += 1

    print("\n" + "=" * 96)
    print("与「上限736(max)」结果一致的张数（共 %d 张）:" % total)
    for name, _ in engines:
        print("  %-14s %d/%d" % (name, agree[name], total))
    print("")
    print("判读：若某配置在多数图上与原结果一致，说明该尺寸下识别质量没退化；")
    print("      若出现空结果或明显缺字，说明缩得太狠，需要调大 limit_side_len。")


if __name__ == "__main__":
    main()
