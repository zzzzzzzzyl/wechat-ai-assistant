# -*- coding: utf-8 -*-
"""圆角是否真的画出来了：抓窗口自身的位图，看四个角的 alpha。

    python probes/qt/check_corners.py

⚠️ 为什么要单独造一个"把面板填满纯色"的探针：`QWidget.grab()` 会把
   **子部件**画进去，所以只要子部件铺到了窗口的 (0,0)，圆角就会被盖住 ——
   这正是实测踩到的缺陷（内容层没有缩进到圆角以内，四个角变成直角，
   而自绘代码明明画了圆角、也没有任何报错）。

   `QWidget.render()` 只调这个部件自己的 `paintEvent`，正好用来
   **单独验证外壳那一个 paintEvent 画对了没有**；两者一起看才能分清
   "外壳没画对" 和 "外壳被盖住了"。
"""
import os
import sys

os.environ.pop("QT_QPA_PLATFORM", None)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtCore, QtGui                                # noqa: E402

from wechat_assistant.qt_ui import base                          # noqa: E402
from wechat_assistant.qt_ui.panel import FloatingAssistant       # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("[通过]" if ok else "[失败]", name,
                         ("  " + detail) if detail else ""))
    if not ok:
        FAIL.append(name)


class Cfg:
    def __init__(self, d):
        self._data = d

    def get(self, path, default=None):
        node = self._data
        for part in str(path).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def alpha_at(img, x, y):
    return QtGui.QColor(img.pixelColor(x, y)).alpha()


def to_image(pm):
    im = pm.toImage().convertToFormat(QtGui.QImage.Format_ARGB32)
    return im


def corner_report(tag, im, w, h, r):
    """看四个角上"圆角以外"的点是否透明，以及圆角内侧的点是否不透明。"""
    d = max(1, int(r * 0.2))          # 取一个明显在圆角之外的点
    pts = {
        "左上": (d, d),
        "右上": (w - 1 - d, d),
        "左下": (d, h - 1 - d),
        "右下": (w - 1 - d, h - 1 - d),
    }
    inner = (r + 2, r + 2)
    out_alpha = []
    for name, (x, y) in pts.items():
        a = alpha_at(im, x, y)
        out_alpha.append((name, a))
    print("    %s 圆角外侧 alpha: %s" % (tag, out_alpha))
    a_in = alpha_at(im, *inner)
    print("    %s 圆角内侧 alpha: %d @ %s" % (tag, a_in, inner))
    return out_alpha, a_in


def main():
    app = base.init_app([])
    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark"}})
    ui = FloatingAssistant(cfg, logger=lambda m: None)
    ui.resize(base.px(340), base.px(520))
    ui.show()
    for _ in range(40):
        app.processEvents()
    ui.start_pump()

    w, h, r = ui.width(), ui.height(), ui.radius
    print("窗口 %dx%d  圆角半径 px=%.0f  内容缩进=%d"
          % (w, h, r, ui._content_inset()))

    print("\n== A. 只渲染外壳自己（QWidget.render）==")
    im_self = to_image(ui.grab())            # 见下：grab 已包含子部件
    im_self = QtGui.QImage(w, h, QtGui.QImage.Format_ARGB32)
    im_self.fill(QtCore.Qt.transparent)
    pt = QtGui.QPainter(im_self)
    ui.render(pt, QtCore.QPoint(0, 0))
    pt.end()
    out_self, in_self = corner_report("render", im_self, w, h, r)
    check("外壳自己画出来的圆角：四个角是透明的",
          all(a == 0 for _, a in out_self), str(out_self))
    check("圆角内侧是不透明的（不是整块透明）", in_self > 200, str(in_self))

    print("\n== B. 整窗抓图（QWidget.grab，含所有子部件）==")
    im_all = to_image(ui.grab())
    out_all, in_all = corner_report("grab", im_all, w, h, r)
    check("整窗抓图后四个角**仍然**透明（内容层没有盖住圆角）",
          all(a == 0 for _, a in out_all), str(out_all))
    check("整窗抓图中间不透明", in_all > 200, str(in_all))

    print("\n== C. 四个角对称（不能只有某一个角是圆的）==")
    check("四个角的 alpha 一致", len(set(a for _, a in out_all)) == 1,
          str(out_all))

    print("\n== D. 圆角半径肉眼可辨（扫左上角那一列）==")
    col = []
    for y in range(0, int(r) + 3):
        for x in range(0, int(r) + 3):
            if alpha_at(im_all, x, y) > 128:
                col.append((y, x))
                break
    for y, x in col:
        print("    y=%-3d 第一个不透明列 x=%d" % (y, x))
    check("圆角是条斜线（不是直角，也不是整块透明）",
          bool(col) and col[0][1] > col[-1][1], str(col))
    check("圆角收在半径之内（最外那一行还没到 x=r）",
          bool(col) and col[0][1] < int(r) + 3, str(col[:2]))

    print("\n== E. 圆角是不是**抗锯齿**的（数部分透明的过渡像素）==")
    # ⚠️ 用硬阈值（alpha > 128）扫列号**分不出**抗锯齿：整像素台阶和
    #    平滑过渡在阈值化之后都能给出"单调递减的列号"。
    #    真正区分两者的是**部分透明的像素有多少**：
    #      · 整数落点的硬边 → 角落区几乎全是 alpha 0 或 255，过渡像素极少；
    #      · 抗锯齿        → 沿弧线一圈都是中间 alpha（覆盖率混色）。
    #    参照：Tk 8.6 的 Canvas **不做抗锯齿**（那边只能靠 PIL 4 倍超采样位图）。
    box = int(r) + 4
    partial = 0
    for y in range(0, box):
        for x in range(0, box):
            a = alpha_at(im_all, x, y)
            if 0 < a < 255:
                partial += 1
    print("    左上角 %dx%d 区域里**部分透明**的像素: %d" % (box, box, partial))
    # ⚠️ 阈值别定太高：抗锯齿**不会**凭空多出像素，它只是把每个边缘像素的
    #    alpha 改成覆盖率。弧线是"每行大约一个像素"，所以量级就是**半径那么大**
    #    （实测 r=15 时 29 个）。第一版把阈值写成 40，把一个正确的实现判成了失败。
    #    硬边台阶的判据是"几乎没有中间值"，所以下面那条"渐变"才是主证据。
    check("沿弧线有足量的过渡像素（约每行一个）",
          partial >= int(r), "%d 个（半径 %d）" % (partial, int(r)))
    # 再取一条更严格的证据：弧线最陡的那一段应当出现越来越小的 alpha
    vals = [alpha_at(im_all, x, 3) for x in range(0, box)]
    nonzero = [v for v in vals if v > 0]
    print("    y=3 那一行从左往右的 alpha: %s" % nonzero[:8])
    mids = sorted(set(v for v in nonzero if 0 < v < 255))
    print("    出现过的中间 alpha: %s" % mids)
    # ⚠️ 这才是"抗锯齿 vs 台阶"的**决定性**判据：
    #    整像素台阶的 alpha 只可能是 0 或 255；
    #    看到 107 / 252 这种覆盖率混色，就只可能是真抗锯齿。
    check("过渡是**渐变**的（存在多个不同的中间 alpha）", len(mids) >= 2,
          str(mids))

    pm = ui.grab()
    out_png = os.path.join(ROOT, "out", "qt_corners.png")
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    pm.save(out_png)
    print("\n已存图: %s" % out_png)

    ui.stop()
    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
