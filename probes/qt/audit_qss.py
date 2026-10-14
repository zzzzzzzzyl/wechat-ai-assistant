# -*- coding: utf-8 -*-
"""样式表体检：**每个控件的样式表 Qt 都解析得动吗**。

    python -X utf8 probes/qt/audit_qss.py

⚠️ 这个探针是为一个**真实缺陷**写的，它的教训值得记：
    `widgets.py` 里 Pill 的样式表用了 `{{` `}}`（`.format()` 风格的转义），
    但代码用的是 **`%` 格式化** —— `%` 不认识 `{{`，于是双括号原样进了 CSS，
    变成 `QPushButton {{`，**Qt 解析整条失败、静默退回系统默认外观**。
    用户看到的就是「生成建议不是绿的」（一个浅灰白按钮）。

    ⚠️ 为什么原来的 `audit_buttons.py` 没抓到：
       它读的是 `widget.styleSheet()` 里**有没有 `#01C260` 这个字符串** ——
       字符串**在**（只是 Qt 解析不了），所以它一路全绿。
       **"字符串里有正确的颜色" ≠ "Qt 会按它画"**。

    这里改成验两件**渲染层面**的事：
      ① 样式表里没有 `{{` / `}}`（那是最常见的解析杀手）
      ② 把控件渲染到一张**洋红底**上，看它有没有真的把自己的底色画上去
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtCore, QtGui, QtWidgets                    # noqa: E402
from wechat_assistant import dpi                                # noqa: E402

dpi.enable()
from wechat_assistant.qt_ui import base                         # noqa: E402
from wechat_assistant.qt_ui.cards import Card, Switch           # noqa: E402
from wechat_assistant.qt_ui.inputs import EntryBox, InputBox    # noqa: E402
from wechat_assistant.qt_ui.layout import ScrollArea            # noqa: E402
from wechat_assistant.qt_ui.widgets import Pill                 # noqa: E402

FAIL = []
MAGENTA = "#FF00FF"


def check(name, ok, detail=""):
    print("  %s %-44s %s" % ("[通过]" if ok else "[失败]", name, detail))
    if not ok:
        FAIL.append(name)


def pump(app, ms=200):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def walk(w):
    out = [w]
    stack = [w]
    while stack:
        cur = stack.pop()
        for c in cur.children():
            if isinstance(c, QtWidgets.QWidget):
                out.append(c)
                stack.append(c)
    return out


def on_magenta(w):
    """把控件渲染到**洋红底**上，返回 (左侧中部, 上方中部) 两个像素色。

    ⚠️ 三个必需项，少一个结论就不可信（都踩过）：
       · **只传 `DrawChildren`**：`render()` 默认还会 `DrawWindowBackground`，
         它会先用调色板底色把目标刷一遍 —— 于是"透明处透出洋红"这个判据
         直接失效，透明的地方量到的是调色板底色而不是洋红。
         ⚠️ `renderFlags` 是**第 4 个**参数，第 3 个 `sourceRegion` 不能跳过，
            否则 PySide6 匹配不上重载、抛 TypeError。
       · **采样点要落在胶囊内部**：Pill 是胶囊（半径 = 高度/2），
         角上那一小块**本来就没有像素**（透明），拿 (4,4) 去采样会得到洋红，
         然后把"没画底色"误判成 bug。这里取"左中"和"上方中部"两个点。
       · **hover 先归零**：hover 由**真实鼠标位置**决定，不归零的话
         量的就是鼠标停在哪 —— 同一份代码会时绿时深绿（实测踩到过）。
    """
    if hasattr(w, "_apply"):
        w._hover = False
        w._apply()
    pm = QtGui.QPixmap(max(1, w.width()), max(1, w.height()))
    pm.fill(QtGui.QColor(MAGENTA))
    pt = QtGui.QPainter(pm)
    w.render(pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
             QtWidgets.QWidget.RenderFlag.DrawChildren)
    pt.end()
    im = pm.toImage()

    def at(x, y):
        c = QtGui.QColor(im.pixelColor(min(max(0, x), im.width() - 1),
                                       min(max(0, y), im.height() - 1)))
        return "#%02X%02X%02X" % (c.red(), c.green(), c.blue())
    # 左中：胶囊左端的中点（一定在图形内部，且避开文字）
    # 上方中部：竖直方向靠上但不在角上（一定在图形内部，且避开文字）
    return at(6, im.height() // 2), at(im.width() // 2, max(2, im.height() // 8))


def main():
    app = base.init_app([])
    st = base.Style(None, name="dark")
    p = st.p

    print("== 1. 所有内置控件的样式表里没有双花括号 ==")
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    built = {}
    for v in ("primary", "selected", "ghost", "text", "secondary"):
        b = Pill(st, "生成建议", variant=v)
        lay.addWidget(b)
        built["Pill/" + v] = b
    built["Card"] = Card(st, "一条建议", index=1, width_px=300)
    built["Switch"] = Switch(st, True)
    built["InputBox"] = InputBox(st, height=60, placeholder="x")
    built["EntryBox"] = EntryBox(st, "x")
    built["ScrollArea"] = ScrollArea(st, "bg")
    for k, w in built.items():
        lay.addWidget(w)
    host.resize(420, 620)
    host.show()
    pump(app, 400)

    bad = []
    for k, w in built.items():
        # 连同子控件一起查（Card 是容器，真正的样式在它自己身上）
        for c in walk(w):
            ss = c.styleSheet() or ""
            if "{{" in ss or "}}" in ss:
                bad.append("%s(%s)" % (k, type(c).__name__))
    check("没有控件带着双花括号样式表", not bad, str(bad[:6]))

    print("\n== 2. 控件真的把自己的底色画上去了吗（洋红底测试）==")
    # 每种控件给出"期望它不是洋红"的判据（洋红还在 = 它没画底色 = 样式没生效）
    for k in ("Pill/primary", "Pill/selected", "Pill/ghost", "Card", "Switch"):
        w = built[k]
        tl, ct = on_magenta(w)
        check("%s 画出了自己的底色（洋红被盖住）" % k,
              tl != MAGENTA or ct != MAGENTA, "左上=%s 中心=%s" % (tl, ct))

    print("\n== 3. 主按钮必须**真的是绿的**（渲染层面）==")
    gen = built["Pill/primary"]
    tl, ct = on_magenta(gen)
    def isgreen(h):
        r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
        return g > 120 and g > r + 40 and g > b + 20

    def notgreen(h):
        return not isgreen(h)
    check("生成建议的渲染结果里有绿色像素",
          isgreen(tl) or isgreen(ct), "左上=%s 中心=%s" % (tl, ct))

    print("\n== 4. 选中态不是实心亮绿（跟主按钮可分）==")
    sel = built["Pill/selected"]
    stl, sct = on_magenta(sel)
    check("selected 的渲染结果**不是**主按钮那种亮绿",
          not (isgreen(stl) and isgreen(sct)),
          "selected 左上=%s 中心=%s" % (stl, sct))

    print("\n== 5. text 变体是真透明（洋红透出来）==")
    tx = built["Pill/text"]
    ttl, _ = on_magenta(tx)
    check("text 变体透出底色（说明真的透明）", ttl == MAGENTA, ttl)

    # ============================================================ 6. 对比度
    # ⚠️ 这一节回答的是**人眼能不能看出来**，前五节都答不了：
    #    "画出来了" 和 "看得出来" 是两件事。
    #    用户的原话是「选中后变绿」—— 如果选中态跟侧栏底色几乎一样，
    #    那就等于没变绿，用户还是会来问"怎么还是没绿"。
    #
    # ⚠️ **判据为什么是两个通道**：WCAG 的对比度是**纯亮度**指标，
    #    而"选中"在这里主要靠**色相**（灰 → 绿）区分。只卡亮度会误判 ——
    #    浅色下 `#B5ECCF` 和 `#DCDCE1` 亮度几乎相同（1.03:1），
    #    但一个是淡绿、一个是灰，肉眼分得开。所以：
    #      · 底色：**亮度差 或 彩度差**，满足其一即可（任一通道能被感知）
    #      · 文字：**必须卡亮度**，因为读字就是亮度的事（>= 3.0）
    print("\n== 6. 选中态跟底色的对比度（人眼看得出来吗）==")

    def _rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

    def _lum(h):
        """WCAG 相对亮度。"""
        def f(c):
            c /= 255.0
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = _rgb(h)
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def _contrast(a, b):
        la, lb = _lum(a), _lum(b)
        hi, lo = max(la, lb), min(la, lb)
        return (hi + 0.05) / (lo + 0.05)

    def _chroma(h):
        """彩度（HSV 的 S）。

        ⚠️ 别拿 RGB 三通道求和去比"深浅" —— 那个数对色相不敏感，
           实心亮绿的和永远大于任何一个暗绿，第一版就是这么错的。
           彩度才真的在量"有多彩"。
        """
        r, g, b = _rgb(h)
        mx, mn = max(r, g, b), min(r, g, b)
        return 0.0 if mx == 0 else (mx - mn) / float(mx)

    for tname in ("dark", "light"):
        tst = base.Style(None, name=tname)
        tsel = Pill(tst, "会话", variant="selected")
        host2 = QtWidgets.QWidget()
        l2 = QtWidgets.QVBoxLayout(host2)
        l2.addWidget(tsel)
        host2.resize(200, 60)
        host2.show()
        pump(app, 150)
        sel_px, _ = on_magenta(tsel)

        # ⚠️ 底色必须取**侧栏真正的底色**，不是 `bg`。
        #    左侧分组栏铺的是 `surface_alt`（`settings_ui.py` 里 `nav` 的
        #    样式表），拿 `bg` 去比是量错了对象 —— 这个探针第一版就是这么错的。
        nav_bg = tst.p["surface_alt"]
        lum_ratio = _contrast(sel_px, nav_bg)
        d_chroma = abs(_chroma(sel_px) - _chroma(nav_bg))
        check("%s：选中底色跟侧栏底色**至少有一个通道可分**" % tname,
              lum_ratio >= 1.5 or d_chroma >= 0.10,
              "亮度 %.2f:1 / 彩度差 %.3f（%s vs %s）"
              % (lum_ratio, d_chroma, sel_px, nav_bg))

        # ⚠️ 字色取**控件真正算出来的那个**（`Pill.colors()`），不要在这里
        #    按同一套规则再推一遍 —— 推的那份会漂移，然后两边一起错。
        #    ⚠️ 也**不要**去渲染结果里采样文字像素：文字带抗锯齿，
        #       还得正好采到笔画上，采到空白就得到底色，量出来是错的。
        _bg_c, sel_fg, _bd_c = tsel.colors()
        check("%s：控件的底色跟渲染出来的像素一致（样式真的被解析了）" % tname,
              _bg_c.lower() == sel_px.lower(), "%s vs %s" % (_bg_c, sel_px))

        # ⚠️ 选中态的**文字**能不能读。这一条卡的是纯亮度，没有替代通道。
        #    ⚠️ 这里只有这个**下限**判据，**没有**"选中必须比未选中更醒目"那条——
        #    我第一版写了那条，然后发现它**几乎必然失败**、而且失败得没道理：
        #    深色下未选中是 `text_dim`(#979799) 铺在近黑底上，亮度对比高达 4.91；
        #    任何"绿字 + 暗绿底"的选中态都赢不了它。
        #    也就是说那条不变式会**把设计意图正确的实现判成错**。
        #    "选中"靠的是**底色**换了颜色，不是靠文字更亮。
        sel_fg_ratio = _contrast(sel_fg, sel_px)
        check("%s：选中态文字 %s 压在自己的底色上 >= 3.0" % (tname, sel_fg),
              sel_fg_ratio >= 3.0, "%.2f:1" % sel_fg_ratio)
        host2.deleteLater()
        pump(app, 80)

    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
