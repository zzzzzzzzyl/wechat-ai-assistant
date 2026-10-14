# -*- coding: utf-8 -*-
"""悬浮窗布局**硬指标**审计：逐条给期望值 / 实测值 / 判定。

    python -X utf8 probes/qt/audit_layout.py

和 `diag_layout.py` 的区别：那个只**打印**几何，这个**下判定**。
每条都写清楚"为什么该是这样"，判定不通过就 FAIL。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtCore                                       # noqa: E402
from wechat_assistant import dpi                                 # noqa: E402

dpi.enable()
from wechat_assistant.qt_ui import base                          # noqa: E402
from wechat_assistant.qt_ui.cards import Card                    # noqa: E402
from wechat_assistant.qt_ui.panel import FloatingAssistant       # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print("  %s %-46s %s" % ("[通过]" if ok else "[失败]", name, detail))
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


def pump(app, ms=250):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def main():
    app = base.init_app([])
    px = base.px
    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark"}})
    ui = FloatingAssistant(cfg, logger=lambda m: None)
    ui.resize(px(340), px(560))
    ui.show()
    pump(app, 300)
    ui.start_pump()
    ui.post_replies(["第一条建议，写长一点看看换行和右边距。", "第二条。",
                     "第三条，长度中等。"], "", "对方在问今晚吃什么")
    pump(app, 500)

    W, H = ui.width(), ui.height()
    print("面板 %dx%d（缩放 %.2f）\n" % (W, H, base.scale()))

    # ---------------- 横向：所有内容该在同一个左右边界内 ----------------
    print("== 横向对齐 ==")
    body = ui.input.parent()
    xs = {}
    for name, w in (("intent_card", ui.intent_card), ("scroll", ui.scroll),
                    ("input", ui.input), ("btn_gen", ui.btn_gen)):
        p = w.mapTo(body, QtCore.QPoint(0, 0))
        xs[name] = (p.x(), body.width() - (p.x() + w.width()))
    lefts = set(v[0] for v in xs.values())
    rights = set(v[1] for v in xs.values())
    check("四个主块左边缘一致", len(lefts) == 1, str(xs))
    check("四个主块右边缘一致", len(rights) == 1, str(xs))
    check("左右留白相等（不偏）", lefts == rights if len(lefts) == 1 == len(rights)
          else False, "左 %s / 右 %s" % (lefts, rights))

    # 状态栏文字要和卡片左边缘对齐
    sb_left = ui.lbl_status.mapTo(ui, QtCore.QPoint(0, 0)).x() + \
        ui.lbl_status.contentsMargins().left()
    card_left = ui.intent_card.mapTo(ui, QtCore.QPoint(0, 0)).x()
    check("状态栏文字与卡片左边缘对齐", abs(sb_left - card_left) <= 2,
          "状态 %d vs 卡片 %d" % (sb_left, card_left))

    # ---------------- 纵向：高度占比 ----------------
    #
    # ⚠️ 别断言"标题栏占比必须 < 8.5%" —— 那个比例**取决于面板高度**，
    #    而面板高度又取决于内容。一条回复时面板只有 477 高，
    #    标题栏 48 就是 10%，这**不是缺陷**（要改就该改标题栏绝对高度，
    #    而 48px 已经和微信实测的 47px 一致）。
    #    真正该守的是下面两条：标题栏不能比微信高、空间不够时别牺牲固定块。
    print("\n== 纵向占比（面板 %d 高）==" % H)
    tb = ui.titlebar.height()
    # ⚠️ **这条断言在 2026-09-27 被用户的要求推翻了**，改动记在这里免得以后有人
    #    "按微信实测值改回去"：
    #      原判据是"标题栏绝对高度 ≈ 微信实测的 px(32)"（微信真机 47 物理px）。
    #      用户看过之后说「悬浮窗顶部宽度可以高一点」——
    #      **明确要求比微信高一点**，于是 32 -> 36 基准像素（真机 54px）。
    #    → 现在守的是"就是 `TitleBar.TITLEBAR_H` 这个值"，不再跟微信比。
    #      （微信那边 31.3 仍然是**已知事实**，只是我们**故意**不照抄。）
    want = ui.titlebar.TITLEBAR_H
    check("标题栏绝对高度 = px(TITLEBAR_H)（用户要求比微信高一点）",
          abs(tb - px(want)) <= 3, "%d vs %d（微信实测是 px(31.3)）" % (tb, px(want)))
    ib = ui.input.height()
    check("输入框不超过标题栏的 1.6 倍", ib <= tb * 1.6, "%d vs %d" % (ib, tb))
    st = ui.lbl_status.height()
    check("状态栏不高于输入框的一半", st <= ib * 0.5, "%d vs %d" % (st, ib))
    check("标题栏不是纵向最大的一块（滚动区才是主体）",
          tb < ui.scroll.height(), "标题栏 %d vs 滚动区 %d" % (tb, ui.scroll.height()))

    # ---------------- 内容高度该跟着内容走 ----------------
    print("\n== 高度自适应（三条回复要尽量一屏全见）==")
    sc0 = ui.scroll
    lay0 = sc0._lay
    cards0 = []
    for i in range(lay0.count()):
        sub = lay0.itemAt(i).layout()
        if sub is None:
            continue
        w = sub.itemAt(0).widget()
        if isinstance(w, Card):
            cards0.append(w)
    if cards0:
        need = sum(c.height() for c in cards0) + px(6) * len(cards0)
        check("三条回复不用滚动就能看全", need <= sc0.height(),
              "需要 %d，滚动区 %d" % (need, sc0.height()))
        check("滚动区高度 > 一条卡片（不是被压扁）",
              sc0.height() > cards0[0].height(),
              "滚动区 %d vs 卡片 %d" % (sc0.height(), cards0[0].height()))

    # ---------------- 纵向：间距一致 ----------------
    print("\n== 纵向间距 ==")
    def gy(w):
        return w.mapTo(body, QtCore.QPoint(0, 0)).y()

    gaps = {
        "意图卡→滚动区": gy(ui.scroll) - (gy(ui.intent_card) + ui.intent_card.height()),
        "滚动区→输入框": gy(ui.input) - (gy(ui.scroll) + ui.scroll.height()),
        "输入框→动作区": gy(ui.btn_gen) - (gy(ui.input) + ui.input.height()),
    }
    for k, v in gaps.items():
        check("%s 间距 %d px" % (k, v), v >= 0, "负数=重叠")
    vals = list(gaps.values())
    check("三段间距一致（不忽大忽小）", max(vals) - min(vals) <= 2, str(vals))

    # ---------------- 内容区宽度 ----------------
    print("\n== 内容区 ==")
    sc = ui.scroll
    check("内容宽度 == 视口宽度（无 SCROLL_PAD 残留）",
          sc.content_width() == sc.viewport().width(),
          "%d vs %d" % (sc.content_width(), sc.viewport().width()))
    check("没有横向滚动", sc.horizontalScrollBar().maximum() == 0)
    cards = []
    lay = sc._lay
    for i in range(lay.count()):
        sub = lay.itemAt(i).layout()
        if sub is None:
            continue
        w = sub.itemAt(0).widget()
        if isinstance(w, Card):
            cards.append(w)
    check("三条卡片都在", len(cards) == 3, "%d 张" % len(cards))
    if cards:
        over = [c.width() - sc.inner.width() for c in cards]
        check("卡片不溢出内容宽", all(o <= 0 for o in over), str(over))

    # ---------------- 空间不够时该牺牲谁 ----------------
    #
    # ⚠️ 面板有 `min_height()` 下限，所以"把窗口压到很矮"这个操作
    #    **本身就会被顶回去** —— 指望"压扁时滚动区被压"是错的期望。
    #    真正该守的是：下限确实生效了（固定块没被牺牲）。
    print("\n== 空间不够时的优先级 ==")
    ui._docked = False
    ui.resize(px(340), px(300))     # 故意很矮
    pump(app, 300)
    check("矮窗口被下限顶回去（不低于 min_height）",
          ui.height() >= ui.min_height(),
          "高 %d vs 下限 %d" % (ui.height(), ui.min_height()))
    check("滚动区仍留有一块（没被压成 0）", ui.scroll.height() > px(40),
          "scroll h=%d" % ui.scroll.height())
    check("输入框没被压小", ui.input.height() >= px(40),
          "input h=%d" % ui.input.height())
    check("主按钮没被压小", ui.btn_gen.height() >= px(30),
          "btn h=%d" % ui.btn_gen.height())
    check("状态栏还在", ui.lbl_status.height() > 10,
          "status h=%d" % ui.lbl_status.height())
    check("标题栏没被压小", ui.titlebar.height() >= px(28),
          "tb h=%d" % ui.titlebar.height())

    # ---------------- 折叠态 ----------------
    print("\n== 折叠态 ==")
    ui.resize(px(340), px(560))
    pump(app, 200)
    ui.toggle_collapse()
    pump(app, 250)
    check("折叠是正方形", ui.width() == ui.height(),
          "%dx%d" % (ui.width(), ui.height()))

    ui.stop()
    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
