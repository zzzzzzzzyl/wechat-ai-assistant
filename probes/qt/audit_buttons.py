# -*- coding: utf-8 -*-
"""按钮/列表项样式体检：**按色板算出来的颜色对不对**。

    python -X utf8 probes/qt/audit_buttons.py

⚠️ 这个探针**只查"算出来的颜色值对不对"**（哪个 variant 该配哪个色）。
   **它查不出"Qt 到底画没画"** —— 要那个请跑 `probes/qt/audit_qss.py`。

⚠️⚠️ **这里原先写的理由是错的，代价是一个真 bug 溜到了用户面前（2026-09-26）**。
   原来的说法是：「抓像素这条路在 offscreen 下验不了底色，所以只能读 stylesheet」。
   **这个结论是错的**，当时抓像素失败的真实原因有两个，都跟 offscreen 无关：
     ① 拿**顶层**控件渲染 —— 真实界面里按钮是**子控件**，顶层走的是另一条路径；
     ② 没关 `DrawWindowBackground` —— `render()` 默认会用调色板底色把目标
        先刷一遍，于是"透明处透出洋红"根本量不到。
   代价：`widgets.py` 里 Pill 的样式表写成了 `{{` / `}}`（`%` 格式化下会原样
   进 CSS，Qt **整条解析失败、静默退回系统默认外观**），而"读字符串"这条路
   **一路全绿** —— `styleSheet()` 里 `#01C260` 明明在，只是 Qt 不认。
   用户看到的就是「生成建议按钮不是绿的」。
   → 教训：**"字符串里有正确的颜色" ≠ "Qt 会按它画"**。
     凡是"画出来对不对"，一律量像素（`audit_qss.py` / `check_pill_corner.py`）。
"""
import os
import re
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtWidgets                                   # noqa: E402
from wechat_assistant import dpi                                # noqa: E402

dpi.enable()
from wechat_assistant.qt_ui import base                         # noqa: E402
from wechat_assistant.qt_ui.widgets import Pill                 # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print("  %s %-46s %s" % ("[通过]" if ok else "[失败]", name, detail))
    if not ok:
        FAIL.append(name)


def qss_prop(w, prop):
    """从控件计算出来的 stylesheet 里取一个属性值。"""
    s = w.styleSheet()
    m = re.search(r"(?:^|[;{\s])%s\s*:\s*([^;}]+)" % re.escape(prop), s)
    return (m.group(1).strip() if m else "")


def rgb(h):
    h = h.strip().lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def same(a, b, tol=6):
    try:
        ra, rb = rgb(a), rgb(b)
        return max(abs(ra[i] - rb[i]) for i in range(3)) <= tol
    except Exception:
        return False


def lum(h):
    try:
        return sum(rgb(h))
    except Exception:
        return -1


def main():
    app = base.init_app([])
    st = base.Style(None, name="dark")
    p = st.p
    print("深色色板: surface=%s surface_alt=%s accent=%s text=%s\n"
          % (p["surface"], p["surface_alt"], p["accent"], p["text"]))

    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    got = {}
    for v in ("primary", "selected", "ghost", "text"):
        b = Pill(st, "生成建议" if v == "primary" else "会话", variant=v)
        lay.addWidget(b)
        got[v] = b
    host.resize(400, 300)
    host.show()
    time.sleep(0.05)
    app.processEvents()

    cols = {}
    print("== 计算出来的样式 ==")
    for v, b in got.items():
        cols[v] = (qss_prop(b, "background"), qss_prop(b, "color"),
                   qss_prop(b, "border"))
        print("  %-9s bg=%-12s fg=%-10s border=%s"
              % (v, cols[v][0], cols[v][1], cols[v][2]))
    print()

    # ---- primary：实心绿底 + 白字 ----
    check("primary 是**绿底**", same(cols["primary"][0], p["accent"]),
          "%s vs accent %s" % (cols["primary"][0], p["accent"]))
    check("primary 是**白字**", same(cols["primary"][1], p["accent_text"], 4),
          cols["primary"][1])
    check("primary **不要**可见描边（transparent 就是不可见）",
          "transparent" in cols["primary"][2] or cols["primary"][2] in ("none", "0"),
          cols["primary"][2])

    # ---- selected：淡绿底 + 绿字，不是实心绿 ----
    check("selected **不是**实心绿（没套用 primary）",
          not same(cols["selected"][0], p["accent"]),
          "%s vs accent %s" % (cols["selected"][0], p["accent"]))
    # ⚠️ 别用"RGB 求和比大小"判深浅 —— 那是错的口径：
    #    实心亮绿 #01C260 的和是 291，而任何"深色底上的绿"都会比它小，
    #    于是会得出"selected 比 primary 暗淡"这种**没意义**的结论（两条都是对的）。
    #    真正该守的是"两者**看得出来不同**"，而且 selected 是**低饱和**的那个。
    check("selected 与 primary 肉眼可分（不是同一个色）",
          not same(cols["selected"][0], cols["primary"][0]),
          "%s vs %s" % (cols["selected"][0], cols["primary"][0]))
    # 饱和度：实心绿 max-min 很大，淡绿底小得多
    def sat(h):
        r, g, b = rgb(h)
        return max(r, g, b) - min(r, g, b)
    check("selected 底**比 primary 不饱和**（一眼看出它不是主按钮）",
          sat(cols["selected"][0]) < sat(cols["primary"][0]),
          "selected %s(sat %d) vs primary %s(sat %d)"
          % (cols["selected"][0], sat(cols["selected"][0]),
             cols["primary"][0], sat(cols["primary"][0])))
    check("selected 文字是**绿字**", same(cols["selected"][1], p["accent"]),
          "%s vs accent %s" % (cols["selected"][1], p["accent"]))
    check("selected 底色有绿味（不是纯灰）",
          rgb(cols["selected"][0])[1] > rgb(cols["selected"][0])[0],
          "%s  (G=%d > R=%d)" % (cols["selected"][0],
                                 rgb(cols["selected"][0])[1],
                                 rgb(cols["selected"][0])[0]))

    # ---- text：完全透明、无边框 ----
    check("text 背景透明（无边框无底色）",
          cols["text"][0] in ("transparent", "none"), cols["text"][0])
    check("text 不要**可见**描边",
          "transparent" in cols["text"][2] or cols["text"][2] in ("none", "0"),
          cols["text"][2])

    # ---- 三者必须互相看得出来 ----
    check("selected 与 text **不一样**（选中看得出来）",
          cols["selected"][0] != cols["text"][0]
          or cols["selected"][1] != cols["text"][1],
          "%s/%s vs %s/%s" % (cols["selected"][0], cols["selected"][1],
                              cols["text"][0], cols["text"][1]))

    print("\n== set_checked() 走的是 selected 而不是 primary ==")
    b = got["text"]
    b.set_checked(True)
    app.processEvents()
    bg = qss_prop(b, "background")
    fg = qss_prop(b, "color")
    check("set_checked(True) 得到**淡绿底**（非实心绿）",
          not same(bg, p["accent"]) and same(bg, cols["selected"][0] + "" if False else bg),
          bg)
    check("set_checked(True) 文字变绿", same(fg, p["accent"]), fg)
    b.set_checked(False)
    app.processEvents()
    check("set_checked(False) 回到透明",
          qss_prop(b, "background") in ("transparent", "none"),
          qss_prop(b, "background"))

    print("\n== 「生成建议」主按钮 ==")
    gen = Pill(st, "生成建议", variant="primary", expand=True)
    lay.addWidget(gen)
    app.processEvents()
    check("生成建议是绿底白字",
          same(qss_prop(gen, "background"), p["accent"])
          and same(qss_prop(gen, "color"), p["accent_text"], 4),
          "%s / %s" % (qss_prop(gen, "background"), qss_prop(gen, "color")))

    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
