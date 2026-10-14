# -*- coding: utf-8 -*-
"""测图片 OCR 的**稳态**耗时 —— 第一次那 1 秒里有多少是模型加载。

为什么要测：记录器一拍 0.2~1 秒。如果 OCR 一次要 1 秒，**绝不能放在那一拍里同步做**，
否则整条记录流程被拖死。必须先知道稳态数字，再决定怎么接。

做法：先 load + warmup，再对同一张图连跑 N 次取中位数；
同时拿一条普通消息行（978x84）做对照，看「图片比文字行慢多少」。

用法：python probes/bench_image_ocr.py <png> [次数，默认 5]
"""

import sys
import time

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402


def bench(eng, img, n=5, tag=""):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        res = eng.recognize(img)
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    print("  %-22s %4dx%-5d 中位 %7.1f ms  最快 %7.1f  最慢 %7.1f  块数 %d"
          % (tag or "", img.width, img.height, ts[len(ts) // 2], ts[0], ts[-1], len(res)))
    return ts[len(ts) // 2]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else \
        r"out\image_capture\01_图片.png"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    from wechat_assistant.ocr import get_engine
    eng = get_engine()

    t0 = time.perf_counter()
    eng.load()
    print("引擎加载 %.0f ms" % ((time.perf_counter() - t0) * 1000))
    t0 = time.perf_counter()
    eng.warmup()
    print("预热 %.0f ms" % ((time.perf_counter() - t0) * 1000))

    img = Image.open(path).convert("RGB")
    print()
    print("稳态耗时（%d 次取中位）：" % n)

    # 对照：一条普通消息行的大小
    bench(eng, img.resize((978, 84)), n, "普通消息行(对照)")
    bench(eng, img.resize((736, 347)), n, "整窗裁剪(缩到736)")
    bench(eng, img, n, "原样(%dx%d)" % (img.size))
    # 收紧到内容
    a = img.crop((0, 0, 271, 432))
    bench(eng, a, n, "收紧后(271x432)")
    # 只留图片本体（去掉头像/昵称那 ~100px）
    a2 = img.crop((100, 40, 271, 432))
    bench(eng, a2, n, "只留图片(171x392)")

    print()
    print("提示：对照一下 README/CLAUDE.md 里记的 0.13 s/张 —— 那个数是")
    print("      **一条 978x84 的消息行**测出来的。图片比它大得多、文字块也多得多。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
