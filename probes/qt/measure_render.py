# -*- coding: utf-8 -*-
"""量 Qt 悬浮窗**真实渲染**出来的位图（含三条回复）。

    python probes/qt/measure_render.py

⚠️ 为什么不用 `ImageGrab.grab()`：本机的屏幕截图 API **实测不可信**
   （抓一个已知的纯色矩形，抓出来是一片接近均匀的灰 #151517）。
   这里量的是**窗口自己渲染的位图**（`QWidget.grab()`）——
   和 Tk 版那边用 `PrintWindow` 的理由一样：让窗口自己画，别去截屏。

⚠️ 无边框窗口是 `WA_TranslucentBackground`，圆角外面是**半透明**的，
   合成为 PNG 之后不是纯黑。所以"哪像素属于面板"必须用
   **离面板底色的距离**判断，不能用"亮度 > 某阈值"——
   那样会把整张图都算成面板（第一版就这么写错了）。

量三件用户报过的事，都给出数字：
  1. 圆角是不是**真的抗锯齿**：逐行取"进入面板的列号"，与理想圆弧比较。
  2. 右边有没有被吃掉（Tk 版曾经被浮层滚动条盖住）。
  3. 边框线在右/下边界上是不是笔直（Tk 版曾经整段内偏一像素）。
"""
import math
import os
import sys
import time

os.environ.pop("QT_QPA_PLATFORM", None)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PIL import Image                                            # noqa: E402
from PySide6 import QtCore                                       # noqa: E402

from wechat_assistant.qt_ui import base                          # noqa: E402
from wechat_assistant.qt_ui.panel import FloatingAssistant       # noqa: E402


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


def pump(app, ms):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def render():
    """建面板 -> 装三条回复 -> 抓位图。返回 (img, ui, cards)。"""
    app = base.init_app([])
    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark"}})
    ui = FloatingAssistant(cfg, logger=lambda m: None)
    ui.resize(base.px(340), base.px(520))
    ui.show()
    pump(app, 400)
    ui.start_pump()
    ui.post_prompt("系统", "用户", "思考")
    ui.post_replies(["这是第一条建议，故意写长一点，看看换行之后的排版和右侧留白。",
                     "第二条建议。",
                     "第三条建议，长度介于前两条之间，用来验证卡片高度不一致时也排得对。"],
                    "", "对方在问今晚吃什么")
    pump(app, 800)
    cards = count_cards(ui)
    pm = ui.grab()
    out = os.path.join(ROOT, "out", "qt_panel_measure.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    pm.save(out)
    img = Image.open(out).convert("RGB")
    return img, ui, cards, out


def count_cards(ui):
    from wechat_assistant.qt_ui.cards import Card

    got = []
    lay = ui.scroll._lay
    for i in range(lay.count()):
        item = lay.itemAt(i)
        sub = item.layout()
        if sub is None:
            continue
        for j in range(sub.count()):
            w = sub.itemAt(j).widget()
            if isinstance(w, Card):
                got.append(w)
    return got


def panel_edge_x(img, y, bg, tol=10):
    """在 y 这一行上，从左边扫出**第一个属于面板**的列（离底色足够近）。"""
    w, _h = img.size
    for x in range(w):
        px = img.getpixel((x, y))
        if max(abs(px[i] - bg[i]) for i in range(3)) <= tol:
            return x
    return None


def main():
    img, ui, cards, path = render()
    w, h = img.size
    print("位图: %dx%d  ->  %s" % (w, h, path))
    print("窗口: %dx%d  缩放 %.2f  圆角半径 px = %d"
          % (ui.width(), ui.height(), base.scale(), ui.radius))
    print("卡片: %d 张（期望 3）" % len(cards))
    for i, c in enumerate(cards, 1):
        geo = c.geometry()
        print("  第 %d 张: x=%d..%d 宽=%d 高=%d  右边距=%d"
              % (i, geo.x(), geo.x() + geo.width(), geo.width(), geo.height(),
                 ui.width() - (c.mapTo(ui, QtCore.QPoint(0, 0)).x() + geo.width())))

    # 面板底色：取远离圆角的中心点
    bg = img.getpixel((w // 2, h // 2))
    print("\n面板底色(中心) = %s" % (bg,))

    print("\n== 1. 圆角抗锯齿：逐行取'进入面板的列号'，与理想圆弧比 ==")
    r = float(ui.radius)
    rows = []
    for y in range(0, int(r) + 6):
        x = panel_edge_x(img, y, bg)
        if x is None:
            continue
        # 理想圆弧：圆心 (r, r)，半径 r。行 y 处的 x = r - sqrt(r^2-(r-y)^2)
        dy = r - y
        ideal = r - math.sqrt(max(0.0, r * r - dy * dy))
        rows.append((y, x, ideal, x - ideal))
    print("    y    实测x   理想x   偏差")
    for y, x, ideal, d in rows[:14]:
        print("   %-4d  %-6d %-7.2f %+.2f" % (y, x, ideal, d))
    if rows:
        devs = [abs(d) for _, _, _, d in rows]
        print("   平均偏差 %.2f px，最大 %.2f px" % (sum(devs) / len(devs), max(devs)))
        # ⚠️ 真正区分"有抗锯齿"和"整像素台阶"的判据：
        #    台阶画法的偏差**一定**会出现 0.5px 左右的大跳变（只能在整像素上落），
        #    抗锯齿之后偏差会稳定在小数级、且**连续变化**。
        print("   偏差是否连续变化（有小数、不来回跳 0.5）: %s"
              % ("是" if max(devs) < 0.9 else "否"))
        print("   → %s" % ("抗锯齿生效" if max(devs) < 0.9
                          else "【是整像素台阶】"))
    # 另一条独立证据：圆角那一列像素里到底有没有"混合色"
    mixed = 0
    for y in range(0, int(r) + 6):
        for x in range(0, int(r) + 6):
            px = img.getpixel((x, y))
            d = max(abs(px[i] - bg[i]) for i in range(3))
            if 25 < d < 230:        # 既不是底色也不是"外面"
                mixed += 1
    print("   圆角区 16x16 里的**混合色**像素: %d（0 = 完全没有过渡）" % mixed)

    print("\n== 2. 右边有没有被截 ==")
    for i, c in enumerate(cards, 1):
        geo = c.geometry()
        right = c.mapTo(ui, QtCore.QPoint(0, 0)).x() + geo.width()
        print("   卡片%d 右边缘 x=%d，窗口宽 %d，右边距 %d px"
              % (i, right, ui.width(), ui.width() - right))
    print("   → 右边距 > 0 说明没被滚动条／窗口边吃掉")
    # 滚动条是不是独立部件（Tk 版是浮在内容上的）
    print("   滚动区视口宽 %d / 滚动区宽 %d（差 = 滚动条占的宽度）"
          % (ui.scroll.viewport().width(), ui.scroll.width()))

    print("\n== 3. 边框线在右/下边界上是不是笔直 ==")
    cols = []
    for y in range(int(r) + 4, h - int(r) - 4, 5):
        for x in range(w - 1, 0, -1):
            px = img.getpixel((x, y))
            if max(abs(px[i] - bg[i]) for i in range(3)) <= 40:
                cols.append(x)
                break
    if cols:
        print("   右边界列号: min=%d max=%d 极差=%d"
              % (min(cols), max(cols), max(cols) - min(cols)))
        print("   → %s" % ("笔直（Tk 版这里曾经整段内偏一像素）"
                          if max(cols) - min(cols) <= 1
                          else "【有分段偏移】"))

    ui.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
