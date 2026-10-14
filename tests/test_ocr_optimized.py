# -*- coding: utf-8 -*-
"""验证优化后的 OCR 模块：速度 + 识别质量 + CPU 占用。

用法：python tests/test_ocr_optimized.py
"""

import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.ocr import DEFAULT_PARAMS, OcrEngine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def main():
    print("=" * 84)
    print("优化后 OCR 模块验证")
    print("=" * 84)

    logs = []
    eng = OcrEngine(logger=lambda m: logs.append(m))

    print("\n1. 引擎加载与预热")
    t0 = time.perf_counter()
    eng.load()
    load_ms = (time.perf_counter() - t0) * 1000
    check("加载成功", eng.load_ms is not None, "%.0f ms" % load_ms)
    warm = eng.warmup()
    check("预热完成", warm is not None, "%.0f ms" % (warm if warm else -1))

    print("\n2. 参数确认")
    check("已限制线程数", DEFAULT_PARAMS["intra_op_num_threads"] == 4,
          "intra=%s" % DEFAULT_PARAMS["intra_op_num_threads"])
    check("已改为按长边限制", DEFAULT_PARAMS["limit_type"] == "max",
          "limit=%s/%s" % (DEFAULT_PARAMS["limit_type"], DEFAULT_PARAMS["limit_side_len"]))

    print("\n3. 单张速度（真实消息行截图）")
    files = sorted(glob.glob(os.path.join(ROOT, "out", "merge_shots", "*.png")))[:6]
    if not files:
        print("  (没有样例图，跳过)")
    else:
        from PIL import Image

        ts = []
        for f in files:
            img = Image.open(f).convert("RGB")
            t0 = time.perf_counter()
            r = eng.recognize(img)
            ms = (time.perf_counter() - t0) * 1000
            ts.append(ms)
            print("    %-22s %6.0f ms  -> %r"
                  % (os.path.basename(f), ms, [x["text"] for x in r][:3]))
        avg = sum(ts) / len(ts)
        check("平均单张 < 600ms", avg < 600, "平均 %.0f ms（优化前 9190 ms）" % avg)
        check("最慢单张 < 1200ms", max(ts) < 1200, "最慢 %.0f ms" % max(ts))

    print("\n4. 识别质量（关键昵称不能丢）")
    expect = {
        "015216_msg01.png": ["李四", "我可是正规的学习资料你在想什么"],
        "015216_msg03.png": ["李四", "看点文献论文什么的"],
    }
    for fn, want in expect.items():
        p = os.path.join(ROOT, "out", "merge_shots", fn)
        if not os.path.exists(p):
            continue
        from PIL import Image

        got = [x["text"] for x in eng.recognize(Image.open(p).convert("RGB"))]
        check("识别 %s" % fn, all(w in got for w in want), "%r" % got)

    print("\n5. 结果结构")
    if files:
        from PIL import Image

        r = eng.recognize(Image.open(files[0]).convert("RGB"))
        if r:
            k = set(r[0].keys())
            check("返回字段完整", {"text", "score", "x1", "y1", "x2", "y2"} <= k, str(sorted(k)))
        else:
            check("返回结构", True, "(该图无文本)")

    print("\n6. 单例复用")
    from wechat_assistant.ocr import get_engine

    a = get_engine()
    b = get_engine()
    check("get_engine 返回同一个实例", a is b)

    print("\n" + "=" * 84)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 84)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
