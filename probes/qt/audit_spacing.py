# -*- coding: utf-8 -*-
"""盘点 `qt_ui/` 里**写死的** margin / padding / spacing / 圆角。

    python -X utf8 probes/qt/audit_spacing.py

⚠️ 为什么要先盘这个（用户提的第 2、3 条）：
    「全局统一边距常量，全部复用，禁止写死随机 margin/padding」
    「统一圆角档位：窗口 10px，按钮/输入框/气泡用偏小圆角，各处圆角不要混乱」
    要收敛成常量，先得知道**现在散了多少个不同的值**。
    这个脚本按"值 → 出现次数 → 出处"列出来，改完再跑一遍就能看出收敛效果。

⚠️ 只读代码不跑界面，所以它不会因为控件没建出来而漏掉。
"""
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
UI = os.path.join(ROOT, "wechat_assistant", "qt_ui")

# setContentsMargins(a, b, c, d) / setSpacing(x) / setSpacing 里带 px() 的
RE_MARGINS = re.compile(r"setContentsMargins\s*\(([^)]*)\)")
RE_SPACING = re.compile(r"setSpacing\s*\(([^)]*)\)")
# QSS 里的 border-radius / padding / margin
RE_RADIUS = re.compile(r"border-radius\s*:\s*([^;]+);")
RE_PADDING = re.compile(r"(?:^|[;{]\s*)padding(?:-left|-right|-top|-bottom)?\s*:\s*([^;]+);",
                        re.M)
RE_MARGIN_CSS = re.compile(r"(?:^|[;{]\s*)margin\s*:\s*([^;]+);", re.M)


def norm(tok):
    """把实参剥成"看得懂的值"，px(12) -> px(12)；12 -> 12。"""
    return " ".join(tok.split())


def scan():
    margins = Counter()
    spacing = Counter()
    radius = Counter()
    padding = Counter()
    where = defaultdict(list)
    for fn in sorted(os.listdir(UI)):
        if not fn.endswith(".py"):
            continue
        path = os.path.join(UI, fn)
        src = open(path, encoding="utf-8").read()
        for i, line in enumerate(src.split("\n"), 1):
            for m in RE_MARGINS.finditer(line):
                v = norm(m.group(1))
                margins[v] += 1
                where[("margins", v)].append("%s:%d" % (fn, i))
            for m in RE_SPACING.finditer(line):
                v = norm(m.group(1))
                spacing[v] += 1
                where[("spacing", v)].append("%s:%d" % (fn, i))
            for m in RE_RADIUS.finditer(line):
                v = norm(m.group(1)).rstrip("%").strip()
                radius[v] += 1
                where[("radius", v)].append("%s:%d" % (fn, i))
            for m in RE_PADDING.finditer(src[i - 1:i + 1] and line):
                pass
        # padding 可能跨行写在 QSS 字符串里，这里按整份源码扫
        for m in RE_PADDING.finditer(src):
            padding[norm(m.group(1))] += 1
        for m in RE_MARGIN_CSS.finditer(src):
            padding["margin:" + norm(m.group(1))] += 1
    return margins, spacing, radius, padding, where


def show(title, counter, limit=40):
    print("\n== %s ==" % title)
    print("   %d 个不同的值，共 %d 处" % (len(counter), sum(counter.values())))
    for v, n in counter.most_common(limit):
        print("   %-42s x%d" % (v, n))
    if len(counter) > limit:
        print("   ...（还有 %d 个没列）" % (len(counter) - limit))


def main():
    m, s, r, p, where = scan()
    show("setContentsMargins 的实参", m)
    show("setSpacing 的实参", s)
    show("QSS border-radius（像素）", r)
    show("QSS padding / margin", p)

    print("\n== 结论 ==")
    print("   边距组合 %d 种 / 间距 %d 种 / 圆角 %d 种"
          % (len(m), len(s), len(r)))
    # 圆角是个位数量才有意义
    if len(r) > 4:
        print("   ⚠️ 圆角有 %d 种不同取值 —— 用户说的「各处圆角数值不要混乱」就是这个"
              % len(r))

    print("\n== 圆角取值明细（按出现次数）==")
    for v, n in r.most_common():
        files = where[("radius", v)]
        print("   %-8s x%-3d  %s" % (v, n, ", ".join(files[:6])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
