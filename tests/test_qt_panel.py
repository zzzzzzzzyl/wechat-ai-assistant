# -*- coding: utf-8 -*-
"""Qt 版悬浮窗测试：结构、队列、复制、折叠、尺寸自适应、跟随接口、红线。

对照 Tk 版（`tests/test_ui.py`，118 项）
--------------------------------------
**验的行为一模一样**，只是访问方式跟着实现换了：

    Tk 版                          Qt 版
    ----------------------------   -----------------------------
    `btn.cget("text")`             `btn.text()`
    `ui.scroll.inner.winfo_children()`   `ui.scroll._lay`
    `ui.root.winfo_width()`        `ui.width()`
    `root.withdraw()`              `hide()`
    `canvas.itemcget(..., "fill")`  部件自身的样式（Qt 不用自绘多边形）

⚠️ **不打开真实窗口**：用 `QT_QPA_PLATFORM=offscreen`，所以不需要显示器，
   在 CI / 无图形环境的机器上也能跑。代价是**抗锯齿 / 透明这类东西测不出来**
   （那要靠 `probes/qt/diag_panel.py` 目视），这里只验几何与行为。

用法：python tests/test_qt_panel.py
"""

import os
import sys
import time

# ⚠️ 必须在**任何 Qt import 之前**设。offscreen 平台不依赖显示器，
#    但仍然会走完整的布局 / 事件 / 绘制链路 —— 几何断言是真实值。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, SKIP = 0, 0, 0

#: 渲染测试用的**底板色**。洋红是故意的：任何一套正常配色都不可能命中它，
#: 所以"渲染结果里还看得到洋红"就等价于"控件没画自己的底色"。
MAGENTA = "#FF00FF"


def _is_wechat_green(h):
    """这个色号算不算「微信那种绿」。

    ⚠️ 判据是**通道间的关系**，不是"跟某个色号相等"：
       深色的 `#01C260` 和浅色的 `#07C160` 都是绿的，
       把判据写死成某一个色号的话，换主题就会误报。
    """
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return False
    return g > 120 and g > r + 40 and g > b + 20


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def skip(name, why):
    global SKIP
    SKIP += 1
    print("  [跳过] %s —— %s" % (name, why))


class Cfg:
    """最小的配置替身：支持 `cfg.get("a.b", 默认值)` 和 `cfg._data`。"""

    def __init__(self, data=None):
        self._data = data or {}

    def get(self, path, default=None):
        node = self._data
        for part in str(path).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _contrast(c1, c2):
    """两个 `#RRGGBB` 的 WCAG 对比度（1.0~21.0）。

    ⚠️ 只在测试里重算一遍**尺子**，不重算**颜色**：
       颜色一定要从控件自己身上取（`Pill.colors()`），
       否则测试和实现各推一份，两边会一起漂。
    """
    def _lum(h):
        def f(c):
            c /= 255.0
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        try:
            r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
        except Exception:
            return 0.0
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    a, b = _lum(c1), _lum(c2)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def pump(app, ms=140):
    """转一段事件循环 —— 队列 / 布局 / deleteLater 都要靠它推进。"""
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def _safe(fn):
    """跑一段可能抛异常的代码，抛了就**打出来并返回 False**。

    ⚠️ 别把它省掉直接 `check(..., fn())` —— 抛异常会让整个套件中断，
      后面所有断言都不再执行，而报错只有一句 NameError/Traceback，
      看起来像"测试脚本坏了"而不是"被测的东西坏了"。
    """
    try:
        fn()
        return True
    except Exception as e:
        print("      异常: %s: %s" % (type(e).__name__, e))
        return False


def main():
    print("=" * 70)
    print("Qt 悬浮窗测试")
    print("=" * 70)

    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        from wechat_assistant import dpi
        from wechat_assistant.qt_ui import base
        from wechat_assistant.qt_ui.panel import (BASE_WIDTH, COLLAPSED_SIZE,
                                                  BASE_MIN_HEIGHT,
                                                  FloatingAssistant, TitleBar)
    except Exception as e:                                   # pragma: no cover
        print("  PySide6 不可用: %s: %s" % (type(e).__name__, e))
        return 0

    # ⚠️ **必须先 `dpi.enable()`**，和 `run_watch.py` 的启动顺序一致。
    #    不调的话本进程没有 DPI 感知，`GetDpiForSystem()` 会**谎报 96**，
    #    于是 `base.scale()` 静默变成 1.0、所有 `px()` 缩水一半 ——
    #    断言还是自洽的、测试还是"绿"的，但**它验的根本不是真机那套尺寸**。
    #    本机是 150%，所以这里要跑在 1.50 下才有意义。
    dpi.enable()
    app = base.init_app([])
    px = base.px
    print("Qt %s / 缩放 %.2f" % (QtCore.qVersion(), base.scale()))

    called = []
    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark",
                      "frameless": True, "min_width": 200, "width_max": 640}})
    ui = FloatingAssistant(
        cfg,
        logger=lambda m: None,
        on_regenerate=lambda intent: called.append(("regen", intent)),
        on_read=lambda: called.append(("read", None)),
        on_clear=lambda: called.append(("clear", None)),
        on_settings=lambda: called.append(("settings", None)),
        session_source=lambda: {"会话": {}},
        on_mode_change=lambda a, b: called.append(("mode", (a, b))),
        on_config_changed=lambda c: called.append(("cfg", c)))
    ui.resize(px(BASE_WIDTH), px(520))
    ui.show()
    pump(app, 200)
    ui.start_pump()

    # ============================================================ 1. 结构
    print("\n1. 界面结构（布局顺序 = 空间不够时谁被牺牲）")
    check("标题栏存在", ui.titlebar is not None)
    # ⚠️ 高度 38 → 32（基准像素）是**故意的**：微信自己的标题栏约 32，
    #    38 在 340 宽的面板上占了 1/6 高，头重脚轻。
    #    断言跟着改成常量，别写死数字 —— 常量今后再调时这里不该红。
    check("标题栏高度 = px(TitleBar.TITLEBAR_H)",
          ui.titlebar.height() == px(TitleBar.TITLEBAR_H),
          "h=%d 期望=%d" % (ui.titlebar.height(), px(TitleBar.TITLEBAR_H)))
    check("标题栏有三个按钮（设置/折叠/隐藏）",
          sorted(ui.titlebar.buttons) == ["collapse", "hide", "settings"],
          str(sorted(ui.titlebar.buttons)))
    # ⚠️ 2026-10-14：设置那颗经过三代 —— emoji `"⚙"`(U+2699，Windows 上会走
    #    **彩色 emoji 字体**) → 文字「设置」 → 自绘齿轮 → **用户给的 `gear.svg`**。
    #    所以这条同时卡：① 没有字形（不是文字、也不是任何 emoji 码位）；
    #    ② 真的**画**出了东西；③ 中心是**空的**；④ ⚠️ **颜色是主题色**，
    #    不是 SVG 自带的 `#272636`（那在深色主题上几乎看不见）。
    _sb = ui.titlebar.buttons["settings"]
    check("设置按钮**没有字形**（不是文字、也不是 emoji）",
          not getattr(_sb, "_glyph", ""), repr(getattr(_sb, "_glyph", "")))
    check("  （它是 `gear=True` 那种自绘按钮）",
          getattr(_sb, "_gear", False) is True, repr(getattr(_sb, "_gear", None)))
    check("齿轮和「—/✕」**等宽**（回到基准宽，不再是文字那种撑开）",
          _sb.width() == ui.titlebar.buttons["hide"].width(),
          "%d vs %d" % (_sb.width(), ui.titlebar.buttons["hide"].width()))

    # ⚠️ 图标资源必须在包里（`wechat_ai.spec` 收的是**整个 assets 目录**，
    #    所以放进去就够了；这条卡的是"文件还在 / 没被误删"）。
    from wechat_assistant import paths as _paths
    from wechat_assistant.qt_ui import panel as _panel
    _svg = _paths.asset_path("gear.svg")
    check("图标资源 gear.svg 在包里", os.path.exists(_svg) and os.path.getsize(_svg) > 500,
          "%s (%s 字节)" % (_svg, os.path.getsize(_svg) if os.path.exists(_svg) else "-"))
    check("SVG 能被解析（QtSvg 在位、文件没坏）", _panel.gear_svg_ready(),
          str(_panel._GEAR_SVG_STATE))

    # ---- 量像素
    # ⚠️⚠️ 画布必须用 **ARGB32 的 QImage**，不能用 `QPixmap`：
    #    `QPixmap(w, h)` 默认是**没有 alpha 通道**的，往它上面画一个带透明区的
    #    pixmap（齿轮是线框，中间全是透明）时，透明像素会被当成**不透明黑**贴上去 ——
    #    实测整块变成 `#000000`，于是"中心是空的"这类断言全反了（第一版就踩了）。
    import math
    _sb._hover = False
    pump(app, 60)
    _w, _h = _sb.width(), _sb.height()

    def _render_to_image(widget, w=None, h=None):
        w = _w if w is None else w
        h = _h if h is None else h
        img = QtGui.QImage(w, h, QtGui.QImage.Format_ARGB32_Premultiplied)
        img.fill(QtGui.QColor(MAGENTA))
        pt = QtGui.QPainter(img)
        widget.render(pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
                      QtWidgets.QWidget.RenderFlag.DrawChildren)
        pt.end()
        return img

    _bim = _render_to_image(_sb)

    def _is_magenta(im, x, y):
        _c = QtGui.QColor(im.pixelColor(x, y))
        return "#%02X%02X%02X" % (_c.red(), _c.green(), _c.blue()) == MAGENTA

    _painted = sum(1 for _x in range(_w) for _y in range(_h)
                   if not _is_magenta(_bim, _x, _y))
    check("齿轮**真的画出来了**（有像素落在洋红底上）", _painted > 0,
          "画了 %d 个像素" % _painted)
    check("  （但肯定不是铺满整块：它是**线框**齿轮）", _painted < _w * _h * 0.75,
          "%d / %d" % (_painted, _w * _h))
    check("中心是**空的**（齿轮中心那个环里露底）",
          _is_magenta(_bim, _w // 2, _h // 2),
          "中心=%s" % ("露底" if _is_magenta(_bim, _w // 2, _h // 2) else "有东西"))

    # ⚠️⚠️ **尺寸**：用户看见第一版（几乎顶满按钮）就说了「图标大了点」。
    #    判据 = 齿轮的**墨水范围**占按钮短边的比例，和旁边两颗字形同一个量级
    #    （实测「✕」33%、「—」50%）。上界卡得比 0.94 紧得多，下界防它缩成一个小点。
    _xs = [_x for _x in range(_w) for _y in range(_h) if not _is_magenta(_bim, _x, _y)]
    _ys = [_y for _x in range(_w) for _y in range(_h) if not _is_magenta(_bim, _x, _y)]
    _ink = max(max(_xs) - min(_xs) + 1, max(_ys) - min(_ys) + 1)
    _ratio = _ink / float(min(_w, _h))
    check("齿轮**没顶满按钮**（用户报过「图标大了点」：0.55~0.82 之间）",
          0.55 <= _ratio <= 0.82, "墨水占短边 %.0f%%（%d/%d）"
          % (100 * _ratio, _ink, min(_w, _h)))

    # 齿：在**齿轮自己的外圈**上采样（半径按刚量到的墨水算，
    # 这样以后调 `GEAR_BOX` 不会把这条断言调坏）
    _r = int(_ink / 2 * 0.92)
    _ring = [_is_magenta(_bim, _w // 2 + int(_r * math.cos(t)),
                         _h // 2 + int(_r * math.sin(t)))
             for t in [i * 0.1 for i in range(63)]]
    check("外圈上**既有线也有空**（不是一整圈实心环）",
          any(_ring) and not all(_ring), "63 个采样点里 %d 个是底" % sum(_ring))

    # ⚠️⚠️ 最关键的一条：**颜色必须是主题色**。
    #    `gear.svg` 自带 `fill="#272636"`（深灰）—— 忘了换色的话深色主题上
    #    它有像素、能过上面所有断言，但**肉眼几乎看不见**。
    _fg = _panel.qc(ui.style.p["text_dim"]).name().upper()
    _hit = {}
    for _x in range(_w):
        for _y in range(_h):
            if _is_magenta(_bim, _x, _y):
                continue
            _c = QtGui.QColor(_bim.pixelColor(_x, _y)).name().upper()
            _hit[_c] = _hit.get(_c, 0) + 1
    _top = max(_hit, key=_hit.get) if _hit else "-"
    check("齿轮画的是**主题色**（不是 SVG 自带的 #272636）",
          _top == _fg, "画出来最多的是 %s，主题色是 %s" % (_top, _fg))
    check("  （也确认它真的不是那个深灰）", _top != "#272636", _top)

    # 悬停态要**换成更亮的字色**（两套主题都是：平时 text_dim、悬停 text）
    _sb._hover = True
    _hover_im = _render_to_image(_sb)
    _sb._hover = False
    _hfg = _panel.qc(ui.style.p["text"]).name().upper()
    _hhit = {}
    for _x in range(_w):
        for _y in range(_h):
            if _is_magenta(_hover_im, _x, _y):
                continue
            _c = QtGui.QColor(_hover_im.pixelColor(_x, _y)).name().upper()
            if _c == _panel.qc(ui.style.p["hover"]).name().upper():
                continue                      # 悬停底那一圈不算
            _hhit[_c] = _hhit.get(_c, 0) + 1
    _htop = max(_hhit, key=_hhit.get) if _hhit else "-"
    check("悬停时齿轮换成**更亮的字色**", _htop == _hfg,
          "悬停画出来最多的是 %s，期望 %s" % (_htop, _hfg))

    # ⚠️ 兜底：SVG 拿不到时（打包漏了资源 / 没装 QtSvg）**不许白屏**，
    #    要回落到自绘齿轮 —— 所以这里**把 SVG 那条路按死**再量一次。
    _saved = dict(_panel._GEAR_SVG_STATE)
    _panel._GEAR_SVG_STATE.update({"tried": True, "ok": False, "why": "测试按死"})
    try:
        _im2 = _render_to_image(_sb)
        _n2 = sum(1 for _x in range(_w) for _y in range(_h)
                  if not _is_magenta(_im2, _x, _y))
        check("SVG 用不了时**回落到自绘齿轮**（不白屏）", _n2 > 0,
              "兜底画了 %d 个像素" % _n2)
    finally:
        _panel._GEAR_SVG_STATE.update(_saved)
    # ⚠️ 顺序是**行为**不是审美：Tk 版曾把可伸缩的滚动区排前面，
    #    结果窗口一矮，被压成 3px 的是**按钮**、状态栏直接不显示。
    ys = {
        "意图卡": ui.intent_card.mapTo(ui, QtCore.QPoint(0, 0)).y(),
        "滚动区": ui.scroll.mapTo(ui, QtCore.QPoint(0, 0)).y(),
        "输入框": ui.input.mapTo(ui, QtCore.QPoint(0, 0)).y(),
        "生成按钮": ui.btn_gen.mapTo(ui, QtCore.QPoint(0, 0)).y(),
        "状态栏": ui.lbl_status.mapTo(ui, QtCore.QPoint(0, 0)).y(),
    }
    order = sorted(ys, key=lambda k: ys[k])
    check("自上而下顺序正确", order == ["意图卡", "滚动区", "输入框",
                                        "生成按钮", "状态栏"], str(order))
    check("滚动区拿到了剩余空间（高 > px(60)）", ui.scroll.height() > px(60),
          "h=%d" % ui.scroll.height())
    check("状态栏在窗口底部 (y > 80% 高)",
          ys["状态栏"] > ui.height() * 0.8, "y=%d / h=%d" % (ys["状态栏"], ui.height()))

    # ⚠️ Tk 版为了「滚动条浮在内容上、把卡片右边缘盖住」加了 SCROLL_PAD，
    #    要求在卡片右侧留 10 基准像素。**Qt 里这个补丁必须消失** ——
    #    滚动条是独立部件。留着的话卡片会比别处窄一截（用户报过
    #    「结果的气泡与其他宽度不一致」）。
    check("内容宽度 == 滚动区视口宽度（没有 SCROLL_PAD 残余）",
          ui.scroll.content_width() == ui.scroll.viewport().width(),
          "%d vs %d" % (ui.scroll.content_width(), ui.scroll.viewport().width()))

    # ============================================================ 2. 队列
    print("\n2. 队列（子线程 -> UI 线程）")
    ui.post_status("测试状态")
    pump(app, 200)
    check("状态经队列送达", ui._status == "测试状态", ui._status)

    # ============================================================ 2b. 单实例唤醒
    #
    # 用户要求（2026-10-13）：「打包后……已经打开了程序就不要重新开一个，
    # 再次打开 exe 就是把原来的跳出来」。
    # 这一节验的是**最后那一棒**：信号从子线程来 -> 面板真的回到屏幕上。
    # ⚠️ 机制本身（互斥体/事件）在 `tests/test_single_instance.py`，那边不依赖 Qt。
    print("\n2b. 第二次启动 exe：信号从子线程来 -> 面板被唤出来")
    try:
        import threading as _th
        import uuid as _uuid

        from wechat_assistant import single_instance as _si

        _ev = "Local\\wxai-uitest-" + _uuid.uuid4().hex[:10]
        ui.hide()
        pump(app, 120)
        check("先藏起来（模拟用户按过 ✕）", ui.is_visible() is False)
        check("唤醒线程挂得上", _si.watch(ui.wake_from_second_launch,
                                         name=_ev) is True)
        # ⚠️ 从**子线程**发信号：真实场景就是另一个进程干的，
        #    而"跨线程"正是最容易写错的地方（Qt 里子线程碰控件会崩或死锁）。
        _th.Thread(target=lambda: _si.signal_primary(name=_ev),
                   daemon=True).start()
        pump(app, 900)                      # 等待线程 + 队列投递都要真实时间
        check("面板被唤回可见（这一棒断了，第二次双击 exe 就「没反应」）",
              ui.is_visible() is True)
    except Exception as e:
        check("单实例唤醒一节跑通", False, "%s: %s" % (type(e).__name__, e))

    ui.post_replies(["第一条", "第二条", "第三条"], "", "他在问今晚吃啥")
    pump(app, 250)
    check("三条回复都建出了卡片", len(cards(ui)) == 3, str(len(cards(ui))))
    check("意图卡有高度（「对方在说」显形）", ui.intent_card.height() > 0,
          "h=%d" % ui.intent_card.height())
    check("状态变成「已生成 3 条」", ui._status.startswith("已生成 3 条"), ui._status)
    check("_replies 记住了原文", ui._replies == ["第一条", "第二条", "第三条"])

    ui.post_replies([], "网络超时", "")
    pump(app, 250)
    check("出错时状态变成「生成失败」", ui._status == "生成失败", ui._status)
    check("错误文案画进了滚动区",
          any("网络超时" in (find_label(w) or "") for w in scroll_children(ui)))

    ui.post_replies([], "", "")
    pump(app, 250)
    check("没有回复时给出引导文案",
          any("还没有建议" in (find_label(w) or "") for w in scroll_children(ui)))

    print("\n3. 队列里一条坏消息不能拖垮后面所有消息")
    # ⚠️ 这是 Tk 版实测踩出来的：异常处理写在整个 while 循环**外面**，
    #    一条坏消息就中断排空循环，剩下的消息**已出队却不再处理** ——
    #    界面从此静默失联，而且看起来一切正常。
    ui.post("replies", "这不是 dict")
    ui.post_status("坏消息之后的第二条")
    pump(app, 250)
    check("坏消息之后的消息仍然被处理", ui._status == "坏消息之后的第二条",
          ui._status)
    ui.post("busy", None)
    ui.post_status("最后一棒")
    pump(app, 250)
    check("连着两条坏消息也不影响后面的", ui._status == "最后一棒", ui._status)

    # ============================================================ 4. 复制
    print("\n4. 点卡片即复制")
    ui.post_replies(["要点一下这条"], "", "")
    pump(app, 250)
    copied = []
    ui._on_copy = lambda: copied.append(1)
    cards(ui)[0]._on_click("要点一下这条")
    check("剪贴板拿到内容",
          QtWidgets.QApplication.clipboard().text() == "要点一下这条")
    check("状态提示已复制", "已复制" in ui._status, ui._status)
    check("通知了外部（触发源据此进入冷静期，避免打扰）", copied == [1])
    check("卡片闪了一下（反馈发生在手指点的地方）", cards(ui)[0]._flash is True)

    # ============================================================ 5. 折叠
    print("\n5. 折叠 / 展开")
    ui.toggle_collapse()
    pump(app, 200)
    check("折叠后是正方形 px(56)", ui.width() == ui.height() == px(COLLAPSED_SIZE),
          "%dx%d" % (ui.width(), ui.height()))
    # ⚠️ 判据用 `is None` 而不是 hasattr —— Tk 版折叠时属性还在、控件已销毁，
    #    留着"已销毁"的对象比留着 None 更糟（外面看它"存在"，一用就抛）。
    check("折叠态把控件引用清成了 None",
          ui.titlebar is None and ui.input is None and ui.btn_gen is None)
    check("折叠态有那个小方块", getattr(ui, "collapse_bar", None) is not None)

    # 折叠态收到回复 -> 只存状态，展开时恢复（不能去碰已销毁的控件）
    ui.post_replies(["折叠期间来的"], "", "折叠期间的判断")
    ui.set_busy(True, "忙")
    pump(app, 250)
    check("折叠态收到回复不崩，且记住了内容",
          ui._replies == ["折叠期间来的"], str(ui._replies))

    ui.toggle_collapse()
    pump(app, 300)
    check("展开后回到原宽度", ui.width() == px(BASE_WIDTH), str(ui.width()))
    check("展开后把那一条回复显示出来了", len(cards(ui)) == 1,
          str(len(cards(ui))))
    check("展开后意图也恢复了", ui._intent == "折叠期间的判断", ui._intent)

    # ⚠️ 折叠/展开**不能把窗口挪走**（`reposition=False`）。
    #    跳回屏幕角落的话，贴边跟随就白做了（实测反馈过"缩小后跑到右上角去"）。
    pos_before = (ui.x(), ui.y())
    ui.toggle_collapse()
    pump(app, 150)
    ui.toggle_collapse()
    pump(app, 300)
    check("折叠再展开位置不变", (ui.x(), ui.y()) == pos_before,
          "%s -> %s" % (pos_before, (ui.x(), ui.y())))

    # ============================================================ 6. 会话标题
    print("\n6. 会话标题（is_group=None 要保留旧值）")
    ui.post_session("张三.工作", None, "")
    pump(app, 150)
    check("标题更新（会话名可以带句点）", ui._session_title == "张三.工作",
          ui._session_title)
    check("is_group=None 时不动群标记（首报保留 False）",
          ui._session_is_group is False)
    ui.post_session("研发群", True, "")
    pump(app, 150)
    check("报 True 之后群标记为真", ui._session_is_group is True)
    ui.post_session("研发群", None, "")
    pump(app, 150)
    # ⚠️ 记录器为了"切会话就更新标题"，在只读到标题时先报一次 None。
    #    此时**误清**的后果是：从群聊切到单聊，群标记会被清掉（反过来也一样）。
    check("再报 None 不会把已确认的群标记清掉", ui._session_is_group is True)
    check("标题栏重画过（不报错）", ui.titlebar.isVisible())

    # ---- 6b. 群标记**不再显示**（用户要求：「不需要显示群标签」）
    # ⚠️ `_session_is_group` 的**状态**还留着（上面三条在验），
    #    但界面**不再画**那个绿色「群」圆标。这里量像素确认它真的没画 ——
    #    只删代码不量的话，以后有人把 `paintEvent` 里那段加回来也不会被发现。
    _tbp = QtGui.QPixmap(max(1, ui.titlebar.width()), max(1, ui.titlebar.height()))
    _tbp.fill(QtGui.QColor(MAGENTA))
    _tbpt = QtGui.QPainter(_tbp)
    ui.titlebar.render(_tbpt, QtCore.QPoint(0, 0), QtGui.QRegion(),
                       QtWidgets.QWidget.RenderFlag.DrawChildren)
    _tbpt.end()
    _tbim = _tbp.toImage()
    _accent = ui.style.p["accent"].upper()
    _n_accent = 0
    for _y in range(_tbim.height()):
        for _x in range(_tbim.width()):
            _c = QtGui.QColor(_tbim.pixelColor(_x, _y))
            if ("#%02X%02X%02X" % (_c.red(), _c.green(), _c.blue())).upper() == _accent:
                _n_accent += 1
    check("群聊会话下标题栏里**没有**群标记（没有 accent 色像素）",
          _n_accent == 0, "%d 个 accent 像素" % _n_accent)

    # ---- 6c. 每个会话**各自记住**自己的建议（用户要求）
    # 用户先说"换会话别留着"，看了之后改成"**保留着这个会话的建议，切回去还有**"，
    # 并明确选了"只在本次运行内保留（不写磁盘）"。
    # ⚠️ 两个诉求其实都要满足：B 会话**不能**看到 A 的建议（张冠李戴），
    #    但 A 的建议**也不能被丢掉**（切回去要有）。
    print("\n6c. 换会话：各自记住自己的建议")
    ui.post_session("甲", False, "")
    ui.post_replies(["甲的第一条", "甲的第二条", "甲的第三条"], intent="甲的想法")
    pump(app, 300)
    check("甲有 3 条建议", len(ui._replies) == 3, repr(ui._replies))
    # ① 同一个会话反复上报（记录器每 2 秒一次）不能动
    ui.post_session("甲", False, "")
    ui.post_session("甲", None, "")
    pump(app, 250)
    check("同一个会话反复上报不动建议", len(ui._replies) == 3
          and ui._replies[0] == "甲的第一条", repr(ui._replies))
    # ② 换到乙：看不到甲的（张冠李戴要避免）
    ui.post_session("乙", False, "")
    pump(app, 300)
    check("切到乙后看不到甲的建议", ui._replies == [], repr(ui._replies))
    # ⚠️⚠️ **判据必须是"屏幕上还挂着几张卡片"，不能是 `content_height()`。**
    #    踩过的坑：`ScrollArea.clear()` 原来漏删了"包在嵌套布局里的卡片"，
    #    卡片成了**孤儿**——从布局里摘掉了，但还挂在 inner 上按原几何继续画。
    #    此时 `content_height()` **报 0**（孤儿不在布局里了），
    #    拿它当判据的断言会**假绿**：测试通过、屏幕上三张卡片纹丝不动。
    #    用户就是照着屏幕报的「气泡没变」，而测试是绿的。
    _live = []
    if ui.scroll is not None:
        for _c in ui.scroll.inner.findChildren(QtWidgets.QWidget):
            if type(_c).__name__ == "Card" and _c.isVisible() \
                    and _c.width() > 4 and _c.height() > 4:
                _live.append(_c)
    check("切到乙后屏幕上**一张卡片都没有了**（不是只看状态/布局）",
          not _live, "还剩 %d 张可见卡片" % len(_live))
    check("空态提示确实显示了（不是一片空白）",
          ui.scroll is not None and ui.scroll.content_height() > 0,
          "content_height=%s" % (ui.scroll.content_height() if ui.scroll else None))
    # ③ 乙自己生成一份
    ui.post_replies(["乙的一条"])
    pump(app, 250)
    check("乙有自己的 1 条", ui._replies == ["乙的一条"], repr(ui._replies))
    # ④ 切回甲：**要恢复**
    ui.post_session("甲", False, "")
    pump(app, 350)
    check("切回甲后**恢复**了原来的 3 条", len(ui._replies) == 3,
          repr(ui._replies))
    check("恢复的是甲的内容（不是乙的）", ui._replies[0] == "甲的第一条",
          repr(ui._replies[:1]))
    check("连当时的「我想说」一起恢复", ui._intent == "甲的想法", ui._intent)
    check("状态栏明说这是上次的建议（不然以为是自己刚生成的）",
          "上次" in ui._status, ui._status)
    # ⑤ 再切回乙：乙的那 1 条也还在
    ui.post_session("乙", False, "")
    pump(app, 300)
    check("切回乙后乙的 1 条还在", ui._replies == ["乙的一条"], repr(ui._replies))
    # ⑥ 缓存只在内存里：新建一个实例就没有了
    check("缓存是实例级的（不写磁盘）", isinstance(ui._replies_by_session, dict)
          and "甲" in ui._replies_by_session)
    # 复原，别影响后面的用例
    ui.post_session("张三.工作", False, "")
    pump(app, 150)

    # ============================================================ 7. 尺寸
    print("\n7. 高度自适应与下限")
    ui._docked = False
    ui.resize(px(BASE_WIDTH), px(120))       # 故意压到比硬需求还矮
    pump(app, 200)
    ui._fit_height()
    pump(app, 200)
    check("窗口高度不低于 min_height()", ui.height() >= ui.min_height(),
          "%d vs %d" % (ui.height(), ui.min_height()))
    check("min_height() 有缓存（跟随器每秒问 20 次）",
          ui._min_h_cache is not None)

    check("desired_width 在展开态 = ui.width",
          ui.desired_width() == px(340), str(ui.desired_width()))
    ui.collapsed = True
    check("desired_width 在折叠态 = px(56)", ui.desired_width() == px(COLLAPSED_SIZE))
    check("desired_size 折叠态是正方形",
          ui.desired_size() == (px(COLLAPSED_SIZE), px(COLLAPSED_SIZE)))
    ui.collapsed = False

    # ============================================================ 8. 跟随接口
    print("\n8. 贴边跟随接口（follow.WindowFollower 就靠这几个）")
    ui.set_docked(10, 20, 300, 700)
    pump(app, 150)
    check("set_docked 摆好了位置和尺寸",
          (ui.x(), ui.y(), ui.width(), ui.height()) == (10, 20, 300, 700),
          str((ui.x(), ui.y(), ui.width(), ui.height())))
    check("接管标记为真", ui._docked is True)
    ui._fit_height()
    check("接管期间 _fit_height 让位（不跟微信抢高度）", ui.height() == 700)

    # ⚠️ 「尺寸没变就只挪位置」——写成"每次都 resize"会让 Qt 重跑整棵布局，
    #    拖动微信窗口时就是一片卡顿。
    w0 = ui.width()
    ui.set_docked(30, 40, 300, 700)
    pump(app, 120)
    check("尺寸不变时只挪位置（宽度没被重设）", ui.width() == w0,
          "%d -> %d" % (w0, ui.width()))
    check("位置确实挪了", (ui.x(), ui.y()) == (30, 40),
          str((ui.x(), ui.y())))

    ui.clear_docked()
    pump(app, 200)
    check("clear_docked 交还高度控制权", ui._docked is False)
    check("交还后高度回到内容自适应（不再是 700）", ui.height() != 700,
          str(ui.height()))

    # ⚠️ **卡死复现**：跟随器把面板摆成微信的高度之后，`_docked` 可能已经是
    #    `False`（比如跟随器自己先清过），这时如果 `clear_docked()` 直接
    #    `if not self._docked: return` 早退，高度就**永远**回不到内容自适应 ——
    #    窗口保持一个跟内容无关的高度，而且看不出哪里不对。
    #    实测探针量到的原话：`_docked=False h=780 min_height=496`。
    #
    # ⚠️ 但现在**已经构造不出那个卡死态了**：`resizeEvent` 会在高度被外部
    #    改过之后重新按内容算一遍（自愈），所以 `resize(780)` 之后窗口自己
    #    就回到 504。这比原来更好 —— 那个 bug 被结构性挡住了。
    #    所以这里改成验两件**仍然可验**的事：
    #      ① 自愈确实生效（外部改高度之后会回到内容自适应）
    #      ② `clear_docked()` 本身仍然显式纠正高度（不靠自愈兜底）
    ui.resize(px(BASE_WIDTH), px(780))
    pump(app, 300)
    check("自愈：外部把高度改大之后会回到内容自适应（不会卡住）",
          ui.height() == ui._target_height(),
          "h=%d 目标=%d" % (ui.height(), ui._target_height()))
    ui._docked = False
    ui.clear_docked()
    pump(app, 300)
    check("_docked 已经是 False 时，clear_docked 仍会把高度纠正回来",
          ui.height() == ui._target_height(),
          "%d vs %d" % (ui.height(), ui._target_height()))
    check("纠正到的高度 == _target_height()",
          ui.height() == ui._target_height(),
          "%d vs %d" % (ui.height(), ui._target_height()))
    # 幂等：再调一次不该变
    h1 = ui.height()
    ui.clear_docked()
    pump(app, 200)
    check("重复 clear_docked 是幂等的（高度不再动）", ui.height() == h1,
          "%d -> %d" % (h1, ui.height()))
    check("_target_height() 有定义且是正数", ui._target_height() > 0,
          str(ui._target_height()))
    check("高度不低于 min_height()（下限不会自相矛盾）",
          ui.height() >= ui.min_height(),
          "h=%d 下限=%d" % (ui.height(), ui.min_height()))

    # ============================================================ 9. 我想说
    print("\n9. 「我想说」（子线程只读镜像）")
    ui.input.set("今晚想吃火锅")
    ui._mirror_intent()
    check("镜像可被子线程读（peek_intent）", ui.peek_intent() == "今晚想吃火锅",
          ui.peek_intent())
    ui.clear_intent()
    pump(app, 250)
    check("clear_intent 走队列清空输入框", ui.input.get() == "", ui.input.get())
    check("镜像也清了（不然下个会话会误用上一句）", ui.peek_intent() == "")

    ui.input.set("测试意图")
    ui._mirror_intent()
    ui._regen()
    check("提交时把意图传给了回调", ("regen", "测试意图") in called,
          str(called[-3:]))
    check("提交后输入框被清空（用完即清）", ui.input.get() == "")
    check("生成中按钮变灰", ui.btn_gen.isEnabled() is False)
    check("生成中按钮文案变成「生成中…」", ui.btn_gen.text() == "生成中…",
          ui.btn_gen.text())
    ui.set_busy(False)
    pump(app, 250)
    check("忙闲结束后按钮恢复", ui.btn_gen.isEnabled() is True)
    check("忙闲结束后文案回到「生成建议」", ui.btn_gen.text() == "生成建议",
          ui.btn_gen.text())

    # ============================================================ 9b. 样式真的生效
    # ⚠️ 这一节是为一个**真实缺陷**加的，别删（2026-09-26）：
    #    `widgets.py` 里 Pill 的样式表写成了 `{{` `}}`（`.format()` 风格的转义），
    #    而代码用的是 **`%` 格式化**。`%` 不认识 `{{`，双括号就原样进了 CSS：
    #        QPushButton {{  ...  }}
    #    **Qt 解析整条失败，然后静默退回系统默认外观** —— 用户看到的是
    #    「生成建议按钮不是绿的」，而代码里明明写着 `#01C260`。
    #
    # ⚠️ 为什么必须验**渲染**而不是验字符串：
    #    `widget.styleSheet()` 里 **`#01C260` 这个字符串是在的**，只是 Qt 不认。
    #    之前那个 `audit_buttons.py` 正是只查字符串，所以一路全绿、
    #    把这个 bug 直接放行到了用户面前。**"字符串里有正确的颜色" ≠
    #    "Qt 会按它画"** —— 这是这一节存在的全部理由。
    print("\n9b. 样式表 Qt 真的解析得动吗（渲染层面，不是字符串层面）")

    def walk_widgets(w):
        out, stack = [w], [w]
        while stack:
            for c in stack.pop().children():
                if isinstance(c, QtWidgets.QWidget):
                    out.append(c)
                    stack.append(c)
        return out

    doubled = ["%s" % type(c).__name__ for c in walk_widgets(ui)
               if "{{" in (c.styleSheet() or "") or "}}" in (c.styleSheet() or "")]
    check("悬浮窗里没有控件带着双花括号样式表（`%` 格式化的解析杀手）",
          not doubled, str(doubled[:5]))

    def on_magenta(w):
        """把控件渲染到**洋红底**上。

        ⚠️ 洋红是故意的：它不可能被任何一条正常配色命中，所以
           「渲染结果里还看得到洋红」= 控件根本没画自己的底色。
        ⚠️ 三个必需项（都踩过，少一个结论就不可信）：
           · `renderFlags` 只留 `DrawChildren` —— 默认还会
             `DrawWindowBackground`，它会先用调色板底色把目标刷一遍，
             于是透明处量到的是调色板底色而不是洋红，判据失效。
           · `renderFlags` 是**第 4 个**参数，第 3 个 `sourceRegion` 不能跳过，
             否则 PySide6 匹配不上重载、抛 TypeError。
           · 采样点要落在**胶囊内部**：Pill 的半径 = 高度/2，角上那块
             本来就没有像素，拿 (4,4) 采样会把"圆角"误判成"没画底色"。
           · hover 归零：hover 由真实鼠标位置决定，否则同一份代码会时好时坏。
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
        return at(6, im.height() // 2), at(im.width() // 2,
                                           max(2, im.height() // 8))

    tl, ct = on_magenta(ui.btn_gen)
    check("生成建议**真的画出了自己的底色**（洋红被盖住）",
          tl != MAGENTA or ct != MAGENTA, "%s / %s" % (tl, ct))
    check("生成建议**真的是绿的**（不是系统默认的浅灰白）",
          _is_wechat_green(tl) or _is_wechat_green(ct), "%s / %s" % (tl, ct))

    # 选中态必须跟主按钮**可分**：它是淡绿底 + 绿字，不是实心亮绿。
    # ⚠️ 悬浮窗自己**没有** selected 控件（那个变体在设置/会话窗口里），
    #    所以这里现建一个来比对，别去悬浮窗里找 —— 找不着的。
    #    两个都判的理由：都亮绿的话，左侧设置栏的选中项会和主按钮糊成一片，
    #    "哪个是当前分组"就没法一眼看出来了。
    from wechat_assistant.qt_ui.widgets import Pill as _Pill
    _sel = _Pill(ui.style, "会话", variant="selected")
    _sel.resize(px(180), px(34))
    _sel.show()
    pump(app, 120)
    _stl, _sct = on_magenta(_sel)
    check("selected 是**淡绿底**（跟主按钮的实心亮绿可分）",
          not (_is_wechat_green(_stl) and _is_wechat_green(_sct)),
          "selected %s / %s" % (_stl, _sct))
    _sel.deleteLater()
    pump(app, 80)

    # ⚠️ 选中态的**字**要读得清，而且**两套色板都要**。
    #    实测过的缺陷（2026-09-26）：浅色下选中底是"白 + 一点点绿"(#B5ECCF)，
    #    很亮；而字色固定用 `accent`(#07C160) 也是个亮绿 —— 两个亮色叠一起，
    #    对比只有 **1.80:1**，比未选中的灰字还难读。深色下同一个实现是 3.38:1，
    #    所以**只看深色会漏掉它**，而 `config.yaml` 里 `theme: auto` 是会切浅色的。
    #    修法是字色跟着底色选（不够就压深），这条测试把它锁住。
    from wechat_assistant.qt_ui import base as _base
    from wechat_assistant.qt_ui.widgets import _contrast_ratio as _cr

    for _tname in ("dark", "light"):
        _s = _base.Style(None, name=_tname)
        _p = _Pill(_s, "会话", variant="selected")
        _bgc, _fgc, _ = _p.colors()
        _ratio = _cr(_fgc, _bgc)
        check("选中态文字对比度够读（%s 主题）" % _tname, _ratio >= 3.0,
              "%s 压 %s = %.2f:1" % (_fgc, _bgc, _ratio))
        # 顺带锁住"两套色板别漂成一样"：浅色下必须真的压深了。
        _primary_fg = _s.p["accent"]
        if _tname == "light":
            check("浅色主题的字色**没有**直接拿 accent 用（那样只有 1.80:1）",
                  _fgc.lower() != _primary_fg.lower(),
                  "%s vs accent %s" % (_fgc, _primary_fg))
        _p.deleteLater()

    # ================================================= 9c. 卡片上不该有多余的框
    # ⚠️ 这一节是为用户报的「三个气泡文字有灰色框，复制也有」加的。
    #    根因是 Qt 样式表的一个经典坑：`QLabel` **是 `QFrame` 的子类**，
    #    而不带点的类型选择器 `QFrame` 会**把子类一起选中** ——
    #    于是卡片里每条标签都被套上了那一圈 `border:1px solid card_line`，
    #    而另一条 `QLabel { background:transparent }` **只清了底色、没清边框**。
    #    → 每个标签外面多一圈灰框。
    #
    # ⚠️ 判据是**"卡片内部有没有成行的描边色像素"**，不是"样式表里写了什么"。
    #    这个项目已经栽过一次"字符串对 ≠ 画出来对"，所以这里一律量像素。
    print("\n9c. 卡片（候选回复气泡）内部不该有多余的描边")
    from wechat_assistant.qt_ui.cards import Card as _Card

    _cfg2 = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark",
                        "frameless": True, "min_width": 200, "width_max": 640}})
    _host = QtWidgets.QWidget()
    _hlay = QtWidgets.QVBoxLayout(_host)
    _hlay.setContentsMargins(0, 0, 0, 0)
    _card = _Card(ui.style, "一条用来检查描边的建议内容", index=1, width_px=300)
    _hlay.addWidget(_card)
    _host.resize(px(320), px(90))
    _host.show()
    pump(app, 300)

    _line = ui.style.p.get("card_line", ui.style.p["surface"])
    _pm = QtGui.QPixmap(max(1, _host.width()), max(1, _host.height()))
    _pm.fill(QtGui.QColor(MAGENTA))
    _pt = QtGui.QPainter(_pm)
    _host.render(_pt, QtCore.QPoint(0, 0), QtGui.QRegion(),
                 QtWidgets.QWidget.RenderFlag.DrawChildren)
    _pt.end()
    _im = _pm.toImage()

    def _is_line(x, y):
        c = QtGui.QColor(_im.pixelColor(x, y))
        return ("#%02X%02X%02X" % (c.red(), c.green(), c.blue())).upper() == _line.upper()

    _H = _im.height()
    _W = _im.width()
    _inner_rows = []
    for _y in range(4, _H - 4):
        _n = sum(1 for _x in range(_W) if _is_line(_x, _y))
        # 中间那些行里出现"接近整行"的描边色 = 标签自己的框
        if 20 < _n < _W - 20:
            _inner_rows.append((_y, _n))
    check("卡片内部没有多余的描边（标签不该有边框）", not _inner_rows,
          "内部成行的描边: %s" % (_inner_rows[:5],))
    # 卡片自己的那一圈**必须还在**（别为了清子控件的框把卡片描边也删了）
    # ⚠️ 扫最上面几行取最大值：卡片是布局第一项、贴着 y=0，
    #    而且它现在有圆角，边缘那一两行会被圆角吃掉一部分 —— 只看 y=1 会误判成"没有"。
    _top = max(sum(1 for _x in range(_W) if _is_line(_x, _y))
               for _y in range(0, 4))
    check("卡片自己的描边还在（没有连它一起删掉）", _top > _W // 2,
          "顶部描边像素最多的一行 = %d / 宽 %d" % (_top, _W))
    _card.deleteLater()
    _host.deleteLater()
    pump(app, 100)

    # ================================================= 9d. 禁用态不能"变成背景"
    # ⚠️ 这一节是为用户报的「生成中没有边框，颜色固定浅白色，只看对话也是」加的。
    #    根因在 `Pill._apply()` 的禁用分支：原来是
    #        `bg, fg, bd = p["bg"], p["text_faint"], "transparent"`
    #    —— ① `p["bg"]` 是**面板自己的底色**，按钮于是和背景一模一样、边界消失
    #       （浅色下就是一片浅白）；② 描边写死 `transparent`，连形状都不画了。
    #    而生成期间 `_apply_busy()` 会把主按钮和【只看对话】**一起**禁用，
    #    所以两颗同时变成没有边界的浅白色块 ——「只看对话也是」就是这条。
    #
    # ⚠️ 判据同样是**量像素**，而且必须**铺洋红**：禁用态的底色本来就该很淡，
    #    光看"是不是白色"分不出"画了淡底"和"根本没画"。
    #    铺上洋红之后，**中心点还是洋红 = 这个按钮压根没画底色**。
    print("\n9d. 禁用态：有描边、底色不是面板底色（两套主题都量）")
    _old_theme = ui.style.theme_name
    # ⚠️ 2026-10-14：面板上**只剩「生成建议」一颗按钮**（「只看对话」「看提示词」
    #    搬到设置里去了，见 `settings_ui._build_view_tools`）。
    #    而那两条**幽灵按钮的禁用态**判据（描边 + 对比度）不能跟着丢 ——
    #    它们守的是 `Pill` 的 ghost 变体契约，所以改成**就地造一颗 ghost 按钮**来量。
    #    ⚠️ 每换一次主题要**重新造**（`Pill` 在构造时吃 style，没有 set_style）。
    from wechat_assistant.qt_ui.widgets import Pill
    for _theme in ("dark", "light"):
        ui.set_theme(_theme)
        pump(app, 120)
        _ghost = Pill(ui.style, "次要操作", lambda: None, variant="ghost",
                      parent=ui)
        _ghost.resize(ui.style.px(96), ui.style.px(30))
        _p = ui.style.p
        for _label, _b in (("主按钮", ui.btn_gen), ("ghost 按钮", _ghost)):
            _b.set_enabled(False)
            _b._hover = False          # 悬停态由真实鼠标位置决定，量之前必须归零
            _b._apply()
            pump(app, 80)
            _bg, _fg, _bd = _b.colors()
            _wm = max(1, _b.width())
            _hm = max(1, _b.height())
            _bpm = QtGui.QPixmap(_wm, _hm)
            _bpm.fill(QtGui.QColor(MAGENTA))
            _bpt = QtGui.QPainter(_bpm)
            _b.render(_bpt, QtCore.QPoint(0, 0), QtGui.QRegion(),
                      QtWidgets.QWidget.RenderFlag.DrawChildren)
            _bpt.end()
            _bim = _bpm.toImage()

            def _hex(x, y):
                _c = QtGui.QColor(_bim.pixelColor(x, y))
                return "#%02X%02X%02X" % (_c.red(), _c.green(), _c.blue())

            _top = _hex(_wm // 2, 0)
            _mid = _hex(_wm // 2, _hm // 2)
            check("[%s] %s 禁用时**画了底色**（中心不是洋红）" % (_theme, _label),
                  _mid != MAGENTA, "中心=%s" % _mid)
            check("[%s] %s 禁用时底色**不是面板底色**（不然按钮融进背景）"
                  % (_theme, _label),
                  _bg.upper() != _p["bg"].upper(),
                  "按钮底=%s 面板底=%s" % (_bg, _p["bg"]))
            if _label == "ghost 按钮":
                # 幽灵按钮的禁用态**必须留着描边** —— 这是"看得出这里有个
                # 按钮、只是现在点不了"的唯一线索。
                check("[%s] %s 禁用时描边还在（顶边 == 描边色）"
                      % (_theme, _label),
                      _top.upper() == _bd.upper() and _bd != "transparent",
                      "顶边=%s 声明的描边=%s" % (_top, _bd))
                check("[%s] %s 禁用时的文字仍然读得清（对比度 >= 2.0）"
                      % (_theme, _label),
                      _contrast(_fg, _bg) >= 2.0,
                      "%s on %s = %.2f:1" % (_fg, _bg, _contrast(_fg, _bg)))
            _b.set_enabled(True)
            _b._hover = False
            _b._apply()
            pump(app, 40)
        _ghost.deleteLater()
        pump(app, 40)
    ui.set_theme(_old_theme)
    pump(app, 120)
    # ⚠️ 生成期间主按钮被禁用，量完必须确认能恢复 ——
    #    不然用户下一次看到的还是那块灰的（"忙完不恢复"是比配色更严重的 bug）。
    ui._apply_busy(True, "")
    pump(app, 80)
    check("生成中：主按钮确实被禁用", ui.btn_gen._enabled is False)
    ui._apply_busy(False, "")
    pump(app, 80)
    check("忙完主按钮恢复可点", ui.btn_gen._enabled is True)
    check("主按钮文字恢复成「生成建议」", ui.btn_gen.text() == "生成建议",
          ui.btn_gen.text())
    check("⚠️ 面板上**没有**「只看对话 / 看提示词」按钮了（2026-10-14 搬进设置）",
          not hasattr(ui, "btn_read") and not hasattr(ui, "btn_prompt"),
          "btn_read=%s" % getattr(ui, "btn_read", None))

    # ============================================================ 10. 只读按钮
    print("\n10. 「只看对话」/「看提示词」没接上时要说话")
    called[:] = []
    ui._read()
    check("「只看对话」调到了回调", ("read", None) in called, str(called))
    ui._on_read = None
    ui._read()
    check("回调没接上时状态栏明说",
          ui._status == "读取功能未接入", ui._status)
    ui._on_clear = None
    ui._clear()
    check("清空没接上时也说话", ui._status == "清空功能未接入", ui._status)

    # ============================================================ 11. 提示词
    print("\n11. 看提示词")
    ui._show_prompt()
    check("还没提交过时给提示，不开窗口",
          "还没有提示词" in ui._status and ui._prompt_win is None, ui._status)

    ui.post_prompt("系统提示", "用户提示", "模型在思考")
    pump(app, 200)
    check("post_prompt 存进了 _prompt",
          ui._prompt["system"] == "系统提示" and ui._prompt["reasoning"] == "模型在思考")
    ui._show_prompt()
    pump(app, 250)
    check("提示词窗口建出来了", ui._prompt_win is not None)
    body = ui._prompt_win.txt.toPlainText()
    check("写了 system 的**字数**（不是内容长度写错）", "system（4 字）" in body,
          repr(body[:36]))
    check("写了 user 的字数", "user（4 字）" in body)
    check("思考过程也在里面（它照样计费，要能看见）",
          "模型的思考过程" in body and "模型在思考" in body)
    check("提示词框是只读的", ui._prompt_win.txt.isReadOnly() is True)

    ui._copy_prompt()
    check("复制全文进剪贴板",
          "模型在思考" in QtWidgets.QApplication.clipboard().text())
    check("复制后状态提示", "已复制" in ui._status, ui._status)
    ui._prompt_win.close()
    ui._prompt_win = None

    # ============================================================ 12. 主题
    print("\n12. 主题切换（原地重建，不挪窗口）")
    pos = (ui.x(), ui.y())
    ui.set_theme("light")
    pump(app, 250)
    check("主题名切到 light", ui.style.theme_name == "light",
          ui.style.theme_name)
    check("切主题后窗口没被挪走", (ui.x(), ui.y()) == pos,
          "%s -> %s" % (pos, (ui.x(), ui.y())))
    check("切主题后回复还在（重建时状态被推回界面）",
          len(cards(ui)) == len(ui._replies),
          "%d cards / %d replies" % (len(cards(ui)), len(ui._replies)))
    ui.set_theme("dark")
    pump(app, 250)
    check("切回 dark", ui.style.theme_name == "dark", ui.style.theme_name)

    # ============================================================ 13. 显示隐藏
    print("\n13. 窗口找得回来（Tk 版的老缺陷）")
    ui.hide()
    pump(app, 150)
    check("hide 之后不可见", ui.is_visible() is False)
    ui.wake()
    pump(app, 400)
    # ⚠️ 原版点「隐藏」会 withdraw 且全项目没有 deiconify —— 点一下就彻底
    #    消失只能重启程序。wake 必须能找回来。
    check("wake 之后又能看见", ui.is_visible() is True)
    ui.toggle()
    pump(app, 150)
    check("toggle 可见时 -> 隐藏", ui.is_visible() is False)
    ui.toggle()
    pump(app, 150)
    check("toggle 不可见时 -> 显示", ui.is_visible() is True)

    # ============================================================ 14. 接线
    print("\n14. 三个标题栏按钮的接线")
    called[:] = []
    ui._on_settings = lambda: called.append(("settings", None))
    ui._titlebar_action("hide")
    pump(app, 150)
    check("✕ -> 隐藏", ui.is_visible() is False)
    ui.show()
    pump(app, 150)
    ui._titlebar_action("collapse")
    pump(app, 250)
    check("— -> 折叠", ui.collapsed is True)
    ui._titlebar_action("collapse")
    pump(app, 250)
    check("再点一次 -> 展开", ui.collapsed is False)
    ui._titlebar_action("settings")
    # ⚠️ 这一条**不能**假设设置窗口一定开得起来：Qt 版设置窗口迁移完成前后
    #    行为是不同的（迁完 -> 真的开窗；没迁完 -> ImportError 走兜底回调）。
    #    所以按"两条路各自的结果"验，而不是赌其中一条。
    if ui._settings_win is not None and ui._settings_win_is_open():
        check("⚙ -> 打开 Qt 版设置窗口", True, type(ui._settings_win).__name__)
        check("打开设置窗口时**不需要**兜底回调", ("settings", None) not in called,
              str(called))
        try:
            ui._settings_win.close()
        except Exception:
            pass
        ui._settings_win = None
    else:
        check("⚙ -> 设置窗口不可用时走 on_settings 兜底",
              ("settings", None) in called, str(called))

    # 兜底分支要能**独立**验出来：把设置模块的 import 打断
    called[:] = []
    import builtins

    real_import = builtins.__import__

    def no_settings(name, *a, **kw):
        if name.endswith("settings_ui"):
            raise ImportError("模拟：Qt 版设置窗口尚未迁移")
        return real_import(name, *a, **kw)

    builtins.__import__ = no_settings
    try:
        ui.open_settings()
    finally:
        builtins.__import__ = real_import
    check("设置窗口 import 失败时明说（不静默）",
          "打不开" in ui._status, ui._status)
    check("并且仍然走 on_settings 兜底", ("settings", None) in called, str(called))
    ui._settings_win = None

    # ⚠️ 复用已开着的设置窗口前**必须先确认它没被 Qt 收走**：
    #    被 deleteLater 的部件是悬空引用，再调它的方法直接崩在 C++ 侧
    #    （那不是可以 try/except 的 Python 异常）。
    class Dead:
        def is_open(self):
            raise RuntimeError("Internal C++ object already deleted")

    ui._settings_win = Dead()
    check("设置窗口被 Qt 收走后判定为「不在了」",
          ui._settings_win_is_open() is False)
    check("顺手把悬空引用清掉", ui._settings_win is None)
    ui._settings_win = None
    check("没有设置窗口时判定为「不在了」", ui._settings_win_is_open() is False)

    # ============================================================ 15. 拖拽
    print("\n15. 拖动窗口被夹在桌面可用区里")
    # ⚠️ 无边框窗口**没有任务栏条目、alt-tab 也切不到**，一旦被拖到屏幕外
    #    就只能靠热键找回来（实测反馈过"移动之后点不了"）。
    ui.move_clamped(-10 ** 6, -10 ** 6)
    check("往左上拖会被夹住（不跑到屏幕外）", ui.x() > -10 ** 5, str(ui.x()))
    try:
        from wechat_assistant.follow import work_area

        gl, gt, _gr, gb = work_area()
        check("夹到了可用区之内", ui.y() >= gt, "y=%d vs gt=%d" % (ui.y(), gt))
        check("标题栏始终留在可用区里",
              ui.y() + px(38) <= gb, "y+h=%d vs gb=%d" % (ui.y() + px(38), gb))
    except Exception as e:
        skip("可用区夹取", "follow.work_area 不可用: %s" % e)
    ui.move_clamped(10 ** 6, 10 ** 6)
    check("往右下拖也被夹住", ui.x() < 10 ** 5, str(ui.x()))
    check("拖过之后标记了 _user_moved（这一轮不跟用户抢位置）",
          ui._user_moved is True)
    ui.post_replies(["新内容"], "", "")
    pump(app, 250)
    check("新回复来了就恢复跟随（_user_moved 复位）", ui._user_moved is False)

    # ---- 15b. `move_only`：跟随器**推**位置时用的那条路
    # ⚠️ 固定悬浮（`ui.attach: float`）靠它把面板按"你定的相对偏移"平移。
    #    这里必须验**真面板**上有这个方法、而且它**不设** `_user_moved` ——
    #    跟随器每拍都在调它，一旦标记了"用户拖过"，跟随着就会以为用户又拖了一下、
    #    每拍重新定基准，结果就是**窗口永远不动**（跟随静默失效，不报任何错）。
    print("\n15b. move_only：只挪位置，不算「用户拖过」")
    check("真面板上有 move_only", callable(getattr(ui, "move_only", None)))
    ui._user_moved = False
    ui.move_clamped(300, 200)
    pump(app, 80)
    x0, y0 = ui.x(), ui.y()
    ui._user_moved = False
    ui.move_only(x0 + px(40), y0 + px(30))
    pump(app, 80)
    check("move_only 真的挪了窗口",
          (ui.x(), ui.y()) == (x0 + px(40), y0 + px(30)),
          "%s -> %s" % ((x0, y0), (ui.x(), ui.y())))
    check("move_only **没有**把 _user_moved 置真（否则跟随会静默失效）",
          ui._user_moved is False, str(ui._user_moved))
    # 夹取逻辑只有一份：两条路都要夹在可用区里
    _safe(lambda: ui.move_only(10 ** 6, 10 ** 6))
    pump(app, 60)
    check("move_only 也走同一套夹取（不会跑到屏幕外）", ui.x() < 10 ** 5,
          str(ui.x()))
    check("move_only 不会顺带把 _user_moved 打开", ui._user_moved is False)

    # ---- 15c. `set_docked` 必须跟**窗口实际尺寸**比，不能跟 `_size` 比
    # ⚠️ 用户报的日志（每秒刷一行，没有做任何操作）：
    #     面板几何被外部改过（(1636, 270, 406, 820) -> (1636, 270, 463, 820)），重新跟随
    #    位置一样，只有**宽度**不同，而且永远对不上、永远循环。
    #
    #    根因在 `Panel.set_docked()`：它原来是
    #        old_w, old_h = self._size          # ← 面板自己的**信念**
    #        if (w, h) != (old_w, old_h): ...resize...
    #    而 `_size` 和真实窗口是会**分叉**的（`resize()` 某一刻被布局最小宽度顶过，
    #    真实窗口比请求值宽，而 `_size` 还记着请求值）。一旦分叉：
    #        `_verify()` 清掉 `_last` → 重新下发 406 → 这里判"没变"直接跳过 resize
    #        → 实际还是 463 → 一秒后再来一次……无限循环。
    #    而且每次重摆都要**整棵布局重排**（最贵的那条路）。
    #
    #    这条断言就是**人为制造那个分叉**，然后要求它自愈。
    print("\n15c. 面板尺寸「信念」和实际窗口分叉之后，必须能自愈")
    ui.set_docked(200, 150, px(300), px(400))
    pump(app, 120)
    check("先正常摆一次", ui.width() == px(300),
          "%d vs %d" % (ui.width(), px(300)))
    # 制造分叉：真实窗口被"别人"改宽了，但 `_size` 还是 px(300)
    ui.resize(px(300) + px(60), ui.height())
    pump(app, 120)
    check("分叉已制造（实际比记录宽）", ui.width() == px(360),
          str(ui.width()))
    check("`_size` 还记着旧值（这就是分叉）", ui._size[0] == px(300),
          str(ui._size[0]))
    # 跟随器再下发**同一个尺寸** —— 这正是原来会卡死的那一下
    ui.set_docked(200, 150, px(300), px(400))
    pump(app, 120)
    check("set_docked 按**实际尺寸**判，分叉被修回来了", ui.width() == px(300),
          "%d vs %d（还是 %.0f 就说明又跳过 resize 了）"
          % (ui.width(), px(300), px(300)))
    check("修回来之后 `_size` 和实际一致", ui._size[0] == ui.width(),
          "%s vs %d" % (str(ui._size), ui.width()))

    # ---- 15d. 状态栏那行小字**不能**决定面板的宽度
    # ⚠️ 用户报的「点了只看对话之后，鼠标放到【生成建议】上右边框往右变宽」就是这个：
    #    状态栏是个**不换行**的 `QLabel`，而不换行的 QLabel
    #    `minimumSizeHint().width()` = **整行文字的宽度**；它在根布局里，
    #    于是**直接成了面板的最小宽度**。状态文字一变长，Qt 就把窗口顶宽。
    #    实测（`probes/qt/diag_hover_width.py` [6]）：窗口架到 300，
    #    状态换成「只看对话，未调用 AI（点【生成建议】或按热键才会生成）」
    #    → **从 300 跳到 490**，而且状态换回短的之后**缩不回去**。
    #    ⚠️ 跟按钮 hover 无关：hover 前后 `ui.width` 一点没变（同一探针 [1]~[3]）。
    print("\n15d. 状态栏的长文字不能把面板顶宽")
    check("状态栏允许换行（不换行的 QLabel 最小宽度=整行文字）",
          ui.lbl_status.wordWrap() is True)
    check("状态栏横向 sizePolicy 不参与抢宽度（Ignored）",
          ui.lbl_status.sizePolicy().horizontalPolicy()
          == QtWidgets.QSizePolicy.Ignored,
          str(ui.lbl_status.sizePolicy().horizontalPolicy()))
    _short = "已生成 3 条"
    _long = "只看对话，未调用 AI（点【生成建议】或按热键才会生成）" * 2
    ui.post_status(_short)
    pump(app, 200)
    _w0 = ui.lbl_status.minimumSizeHint().width()
    ui.post_status(_long)
    pump(app, 250)
    _w1 = ui.lbl_status.minimumSizeHint().width()
    # ⚠️ 不能断言"完全不变"：换行的 QLabel 最小宽度仍会随**最长那段**轻微浮动
    #    （实测 52 -> 68）。要卡的是**量级** —— 不换行时这一条会到 **484**，
    #    那才是能把窗口顶宽的量级。
    check("状态文字变长，状态栏的最小宽度仍然很小（不会顶宽面板）",
          _w1 <= px(80), "%d -> %d（不换行时是 484）" % (_w0, _w1))
    # 真的架到窄宽度上量一次：长状态不许把窗口顶宽
    ui.set_docked(100, 100, px(300), px(500))
    pump(app, 150)
    _before = ui.width()
    ui.post_status(_long)
    pump(app, 250)
    check("架在 300 宽上，长状态不会把窗口顶宽",
          ui.width() <= max(_before, px(320)),
          "要 300 -> 实际 %d（原来会跳到 490）" % ui.width())
    ui.post_status("已生成 3 条 · 点一下卡片就复制")
    pump(app, 150)

    # ---- 15e. 「只看对话」走**单独窗口**，不塞进主面板
    # 用户提的：「我感觉只看对话也用单独窗口来吧」。
    # 原来它 `show_replies(对话全文)` —— 把整段对话当成"建议"铺在主面板里，
    # 于是①刚生成的建议被顶掉、②长对话把面板撑成一坨可滚动的东西。
    print("\n15e. 只看对话：单独开窗口，不占主面板")
    _n_cards_before = len(cards(ui))
    _replies_before = list(ui._replies)
    ui.post_read("对方: 你好\n我: 你也好\n→ 对方: 在吗")
    pump(app, 300)
    check("主面板的回复**一条没变**（建议没被顶掉）",
          ui._replies == _replies_before, repr(ui._replies))
    check("主面板也没多出卡片", len(cards(ui)) == _n_cards_before,
          "%d -> %d" % (_n_cards_before, len(cards(ui))))
    check("对话全文存下来了", "在吗" in (ui._read_text or ""),
          repr((ui._read_text or "")[:40]))
    _rw = getattr(ui, "_read_win", None)
    check("单独窗口建出来了", _rw is not None)
    if _rw is not None:
        check("窗口里是那段对话", "在吗" in _rw.txt.toPlainText(),
              repr(_rw.txt.toPlainText()[:40]))
        check("对话窗口是只读的", _rw.txt.isReadOnly() is True)
        check("「只看对话」窗口**没有系统边框**（跟其他弹窗一致）",
              bool(_rw.windowFlags() & QtCore.Qt.FramelessWindowHint))
        _rw.deleteLater()
        ui._read_win = None
        pump(app, 120)

    # ---- 15f. 程序图标（任务栏 / Alt-Tab 用）
    # 用户要求：「这个作为图标，再添加进任务栏」。
    # ⚠️ 任务栏按钮的图标在 Windows 上要**两件事同时成立**：
    #    ① `QApplication.setWindowIcon()` —— 否则是系统默认图标；
    #    ② `SetCurrentProcessExplicitAppUserModelID` —— 否则用 `python.exe` 启动时
    #       任务栏按钮归 python.exe，图标是 **Python 的**，还跟别的脚本挤一组。
    #    只做①不做②的话，改完看起来"没生效"。
    print("\n15f. 程序图标")
    from wechat_assistant import paths as _paths

    _icon = _paths.icon_path()
    check("图标文件存在", os.path.exists(_icon), _icon)
    check("用的是 .ico（要带 16~256 多档）", _icon.endswith(".ico"), _icon)
    check("应用图标已设（不是默认的空图标）",
          not app.windowIcon().isNull())
    _sizes = sorted(int(s.width()) for s in app.windowIcon().availableSizes())
    # ⚠️ 多档是**必须**的：任务栏取 32、Alt-Tab 取 48、小视图取 16，
    #    只给一档会由系统硬缩，小尺寸会糊。
    check("图标带了多个尺寸（任务栏/Alt-Tab 各取各的）",
          len(_sizes) >= 4, str(_sizes))
    check("里面有 16×16（任务栏小图标用这个）", 16 in _sizes, str(_sizes))
    check("里面有 256×256（大图标视图用这个）", 256 in _sizes, str(_sizes))
    check("悬浮窗自己也带着图标（不靠 Qt 传播）",
          not ui.windowIcon().isNull())
    # ⚠️ 悬浮窗**必须**是普通顶层窗口，不能是 `Qt.Tool` ——
    #    Tool 窗口不进任务栏。这条在 offscreen 下也能查（看的是 flags 不是 HWND）。
    # ⚠️ **不能写 `flags & Qt.Tool`**：`Qt.Tool`(0xb) 是**低字节的取值**，不是一个位。
    #    按位与的结果是 `Qt.Window`(0x1)，恒为真 —— 那样写这条断言永远失败
    #    （我第一版就是这么写的）。要比的是低字节整体：`WindowType_Mask`。
    check("悬浮窗不是 Qt.Tool（Tool 窗口不进任务栏）",
          (ui.windowFlags() & QtCore.Qt.WindowType_Mask) != QtCore.Qt.Tool,
          "窗口类型 = 0x%X" % int(ui.windowFlags() & QtCore.Qt.WindowType_Mask))

    # ---- 15g. 系统托盘（右下角通知区域）
    # 用户要求：「还有要加入右边的状态栏？」—— 就是右下角的托盘/通知区域。
    # ⚠️ 它解决的是**"藏起来就找不回来"**：本程序的 ✕ 是"隐藏"，而 Windows
    #    在窗口隐藏时**连任务栏按钮一起收走**（实测过，见 CLAUDE.md），
    #    于是既没有任务栏条目、alt-tab 也切不到，只剩热键一条路。
    # ⚠️ **离屏平台没有托盘**（`isSystemTrayAvailable()` 恒 False），所以这里
    #    只能测①纯逻辑 ②"没有托盘时不崩"。真正的托盘外观要靠
    #    `probes/qt/check_taskbar.py` 在真机上量。
    print("\n15g. 托盘图标：纯逻辑 + 没有托盘的环境不能崩")
    from wechat_assistant.qt_ui.tray import TrayIcon, tray_menu, tray_tooltip

    _m_show = dict(tray_menu(False))
    _m_hide = dict(tray_menu(True))
    check("菜单有三个动作：显示/隐藏 · 设置 · 退出（⚠️ 2026-10-14 撤掉了「只看对话」）",
          sorted(_m_show) == ["quit", "settings", "toggle"],
          str(sorted(_m_show)))
    check("⚠️ 托盘里**没有**「只看对话」了（查看类动作去设置里点）",
          "read" not in _m_show, str(sorted(_m_show)))
    # ⚠️ 「显示/隐藏」的文案必须跟着状态变：面板开着时还写"显示悬浮窗"，
    #    用户点它没反应（因为它本来就在），会以为菜单坏了。
    check("面板可见时第一项是「隐藏悬浮窗」",
          _m_hide["toggle"] == "隐藏悬浮窗", _m_hide["toggle"])
    check("面板不可见时第一项是「显示悬浮窗」",
          _m_show["toggle"] == "显示悬浮窗", _m_show["toggle"])
    check("退出那一项在（不然有托盘就可能关不掉）", "quit" in _m_show)

    check("tooltip 带当前会话名", tray_tooltip("张三") == "微信 AI 助手 · 张三",
          tray_tooltip("张三"))
    # ⚠️ "—" 是"当前没打开会话"的内部占位，不该出现在给人看的提示里
    check("没有会话时只说程序名（不显示占位符「—」）",
          tray_tooltip("—") == "微信 AI 助手", tray_tooltip("—"))
    check("空会话名同理", tray_tooltip("") == "微信 AI 助手", tray_tooltip(""))

    check("离屏环境下托盘不可用（测试要容忍这一点）",
          TrayIcon.available() is False, str(TrayIcon.available()))
    check("没有托盘时 build_tray 返回 None（不抛异常）",
          ui.build_tray() is None, str(ui.tray))
    check("并且 tray 属性是干净的 None", ui.tray is None, str(ui.tray))
    check("没有托盘时 _sync_tray 也不崩", _safe(lambda: ui._sync_tray()))

    # ---- 四个菜单动作**各自**接到对的地方
    # ⚠️ 用替身，别真调 `stop()` —— 它会 `QTimer.singleShot(0, QApplication.quit)`，
    #    把这一轮的事件循环拆掉，**后面几条用例会莫名其妙地失败**
    #    （第一版就是这么写的：`pump_now 之后队列被排空了 0 张`，
    #     看起来跟托盘八竿子打不着）。
    _calls = []
    _real = {n: getattr(ui, n) for n in
             ("toggle", "_read", "open_settings", "stop")}
    ui.toggle = lambda: _calls.append("toggle")
    ui._read = lambda: _calls.append("read")
    ui.open_settings = lambda: _calls.append("settings")
    ui.stop = lambda: _calls.append("quit")
    try:
        for _aid in ("toggle", "read", "settings", "quit"):
            ui._on_tray_action(_aid)
    finally:
        for _n, _f in _real.items():
            setattr(ui, _n, _f)
    check("四个托盘动作各接各的（没有全接成同一个）",
          _calls == ["toggle", "read", "settings", "quit"], str(_calls))
    check("托盘退出走的是 stop()（它才会退出主循环）",
          _calls[-1] == "quit", str(_calls))
    check("未知动作不会崩", _safe(lambda: ui._on_tray_action("没这个动作")))

    # ============================================================ 16. 红线
    print("\n16. 红线（Qt 版不许引入新的越线操作）")

    import io

    src = io.open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "wechat_assistant", "qt_ui", "panel.py"), encoding="utf-8").read()
    for bad, why in (("SendInput", "不模拟键鼠"),
                     ("keybd_event", "不模拟键鼠"),
                     ("SetForegroundWindow", "不抢前台"),
                     ("mouse_event", "不模拟鼠标")):
        check("panel.py 里没有 %s（%s）" % (bad, why), bad not in src)
    check("panel.py 只写剪贴板，不碰微信",
          "clipboard" in src and "weixin" not in src.lower())
    check("不在这里发网络请求", "requests" not in src and "urllib" not in src)

    # ============================================================ 17. 公开 API
    print("\n17. 公开 API（迁移后 follow / run_watch 的调用点一个都不能少）")
    for name in ("post", "post_session", "post_status", "post_replies", "post_target",
                 "post_prompt", "set_busy", "peek_intent", "clear_intent",
                 "min_height", "desired_width", "desired_size", "set_docked",
                 "clear_docked", "set_geometry", "show", "hide", "is_visible",
                 "wake", "toggle", "toggle_collapse", "set_theme", "run", "stop",
                 "open_settings", "open_session_settings", "_copy", "_regen",
                 "_read", "_clear", "_render_replies", "_handle", "_pump",
                 "_fit_height", "_content_width", "_apply_busy", "_copy_prompt",
                 "_show_prompt", "_save_width"):
        check("有 %s()" % name, callable(getattr(ui, name, None)))

    # ============================================================ 19. 有界排空
    print('\n19. pump_now（给 --demo 那条"不进主循环"的路用）')
    # ⚠️ `--demo` 是"触发一次就退出"，**不进 `run()` 的主循环**；而生成跑在
    #    子线程、结果通过 `post_replies()` 进队列。不主动排空的话
    #    `--demo` 只在命令行打印，窗口里空着 ——「验证链路用」的那条路
    #    自己看不见结果，等于没验证。
    import threading

    ui.scroll.clear()
    ui._replies = []
    ui._status = ""

    def worker():
        time.sleep(0.2)
        ui.post_replies(["子线程来的一条建议", "第二条"], "", "子线程的判断")

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    ui.pump_now(1200)
    th.join(timeout=2)
    check("pump_now 之后队列被排空了（卡片建出来）", len(cards(ui)) == 2,
          "%d 张" % len(cards(ui)))
    check("状态也更新了", ui._status.startswith("已生成 2 条"), ui._status)
    check("意图也到了", ui._intent == "子线程的判断", ui._intent)
    # ⚠️ 判据用"有界"：这个方法必须**会返回**，不能变成第二个主循环
    t0 = time.perf_counter()
    ui.pump_now(300)
    dt = (time.perf_counter() - t0) * 1000.0
    check("pump_now 是有界的（会返回，不是第二个主循环）", dt < 2000,
          "%.0f ms" % dt)

    ui.stop()
    pump(app, 100)

    # ============================================================ 18. 性能
    print("\n18. 改窗口尺寸的帧耗时（迁移的**理由**，必须有量化证据）")
    # Tk 版基线（同一台机器、同样内容量，实测）：小布局 34~55 ms/帧，
    # 设置窗口 75 ms/帧（最差 281）。目标 < 20 ms/帧。
    ui.collapsed = False
    ui._docked = True          # 走 set_docked 这条路（跟随器就是它）
    ui.post_replies(["一条比较长的建议，用来占满卡片宽度看看换行和排版。" * 2,
                     "第二条建议。",
                     "第三条建议，也长一点，让卡片高度不完全一致。"], "", "在问一个稍复杂的问题")
    pump(app, 300)
    sizes = [(px(340), px(420) + i * px(14)) for i in range(16)]
    t0 = time.perf_counter()
    for w, h in sizes:
        ui.set_docked(100, 100, w, h)
        app.processEvents()
    ms_per_frame = (time.perf_counter() - t0) / len(sizes) * 1000.0
    print("      实测 %.1f ms/帧（%d 帧，三条回复）" % (ms_per_frame, len(sizes)))
    print("      Tk 版基线 34~75 ms/帧；目标 < 20 ms/帧")
    check("改尺寸帧耗时 < 20ms（目标）", ms_per_frame < 20.0,
          "%.1f ms/帧" % ms_per_frame)
    check("并且比 Tk 版基线快 3 倍以上", ms_per_frame * 3 < 34.0,
          "%.1f ms/帧 vs 基线 34" % ms_per_frame)

    # ============================================================ 20. 跨线程
    print("\n20. 跨线程调用不能被 Qt 当成 UI 操作（会死锁）")
    # ⚠️ 这是用户报的「点生成建议整个程序卡死」的**根因**，实测抓到的栈：
    #      主线程: on_trigger → threading.start   ← 等子线程起来
    #      子线程: do_generate → wake()           ← 等主线程响应
    #    `wake()` 里那句 `setWindowFlag()` 会让 Qt 重建原生窗口、等窗口过程
    #    响应，而窗口过程在主线程 —— 两边互等，永久卡死。
    #    Tk 里没这个问题（子线程调控件会被排进主循环），**这是 Qt 与 Tk 最危险的差异**。
    import threading

    done = []
    err = []

    def worker():
        try:
            # 子线程调 wake()：必须**立刻返回**，不能碰控件
            ui.wake()
            done.append("returned")
        except Exception as e:                               # pragma: no cover
            err.append("%s: %s" % (type(e).__name__, e))

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(timeout=5.0)
    check("子线程调 wake() 不会卡住（5 秒内返回）", not th.is_alive(),
          "还活着 = 死锁了")
    check("子线程没抛异常", err == [], str(err))
    check("子线程确实调完返回了", done == ["returned"], str(done))
    # 排进队列的那次 wake 由主线程执行，这里转一圈让它跑掉
    pump(app, 400)
    check("主线程把排队的 wake 执行了（窗口还活着）", ui.isVisible() is True)

    check("在 UI 线程上直接调 wake() 也不会卡", _safe(lambda: ui.wake()))
    pump(app, 400)

    # ---- 20b. 唤醒**不能重建原生窗口**（用户报「窗口会闪两下」）
    # ⚠️ 根因：`QtWidgets.QWidget.setWindowFlag()` 在窗口**已经可见**时会
    #    **销毁并重建原生窗口**（官方文档：它内部调 `setParent()`，
    #    部件因此被隐藏，必须再 `show()`）。于是每调一次闪一下。
    #    旧实现是「设成置顶 → show() → 300ms 后设回去」= **两个来回 = 闪两下**，
    #    而且正好落在点「生成建议」的那一刻（`run_watch.do_generate` 第一句
    #    就是 `ui.wake()`，【只看对话】走同一条路）。
    # ⚠️ 这条断言**必须真的盯着 `setWindowFlag` 有没有被调到**，
    #    只看"窗口还在、还可见"是验不出来的 —— 重建之后它照样可见。
    print("\n20b. 唤醒只改 z 序，不重建原生窗口（否则每点一次闪两下）")
    flag_calls = []
    _orig_flag = ui.setWindowFlag

    def _spy_flag(*a, **kw):
        flag_calls.append(a)
        return _orig_flag(*a, **kw)

    ui.setWindowFlag = _spy_flag
    try:
        _safe(lambda: ui.wake())
        pump(app, 400)
        _safe(lambda: ui.wake())
        pump(app, 400)
    finally:
        ui.setWindowFlag = _orig_flag
    check("wake() 两次都没有调用 setWindowFlag（0 次重建原生窗口）",
          flag_calls == [], "被调了 %d 次: %r" % (len(flag_calls), flag_calls[:3]))
    check("wake() 之后窗口仍然可见（提上去了、但是没有闪）", ui.isVisible() is True)
    # ⚠️ 顺带验：「临时置顶再换回来」那套（它靠一个 300ms 的 QTimer 兜底）
    #    整个删掉了。留着的槽函数没人调就等于每次唤醒都少闪一下。
    check("没有残留的 _restore_topmost 定时器回调",
          not hasattr(ui, "_restore_topmost"))

    # 这几个入口都可能被子线程调到（run_watch 的 do_generate 就是子线程），
    # 所以它们要么走队列、要么自己判线程。
    check("set_busy 是纯投队列（子线程可调）",
          _safe(lambda: threading.Thread(target=lambda: ui.set_busy(True, "忙"),
                                         daemon=True).start() is None or True))
    pump(app, 300)
    check("peek_intent 只读镜像字符串（子线程安全）", isinstance(ui.peek_intent(), str))
    check("clear_intent 只投队列", _safe(lambda: ui.clear_intent()))
    pump(app, 300)

    # ============================================================ 外观类立刻生效
    # 用户 2026-10-14 报的两件事，都是"值写进配置了、界面没变、看着像要重启"：
    #   ① 「为什么不透明度还得重启后才有用？」
    #   ② 「保持置顶有时候会出错」（运行期从来没应用过它 + raise_above 会剥掉置顶，
    #      后者在 test_follow §12b 里钉住）
    print("\n16. 不透明度 / 置顶：保存后**立刻生效**（不能只在建窗口时读一次）")
    cfg2 = Cfg({"ui": {"width": 340, "theme": "dark", "frameless": True,
                       "min_width": 200, "width_max": 640,
                       "opacity": 1.0, "topmost": False}})
    ui2 = FloatingAssistant(cfg2, logger=lambda m: None)
    ui2.show()
    pump(app, 200)
    check("建窗口时按配置设了不透明度", abs(ui2.windowOpacity() - 1.0) < 0.01,
          repr(ui2.windowOpacity()))

    # 模拟"用户在设置里改完并保存"：换掉 cfg + 给个假的 store，走同一条回调
    new_cfg = Cfg({"ui": {"width": 340, "theme": "dark", "frameless": True,
                          "min_width": 200, "width_max": 640,
                          "opacity": 0.7, "topmost": False}})

    class _Store:
        def load(self):
            return new_cfg

        def get(self, k, d=None):
            return new_cfg.get(k, d)

    ui2._settings_store = _Store()
    ui2._on_settings_saved()
    pump(app, 200)
    check("保存成 0.7 之后**不用重启**就变了",
          abs(ui2.windowOpacity() - 0.7) < 0.01, repr(ui2.windowOpacity()))

    # ⚠️ 两个边界，语义**不一样**，都得钉住：
    #   · 0.1（手改配置）-> 夹到 0.2：不然面板几乎看不见
    #   · 0   —— `v or 1.0` 把它当"没设" -> 1.0（**故意**的：看不见的窗口点不到、
    #            也找不回来）。这是 `float(v or 1.0)` 那个老写法在这里的既定语义。
    zero_cfg = Cfg({"ui": {"opacity": 0.1, "theme": "dark"}})
    ui2.cfg = zero_cfg
    ui2._apply_opacity()
    check("手改配置成 0.1 -> 夹到 0.2（不让面板几乎看不见）",
          abs(ui2.windowOpacity() - 0.2) < 0.01, repr(ui2.windowOpacity()))
    zero_cfg = Cfg({"ui": {"opacity": 0.0, "theme": "dark"}})
    ui2.cfg = zero_cfg
    ui2._apply_opacity()
    check("手改配置成 0 -> 当成「没设」按 1.0 来（不透明的窗口至少找得回来）",
          abs(ui2.windowOpacity() - 1.0) < 0.01, repr(ui2.windowOpacity()))

    # ⚠️ 运行期改置顶**绝不许**走 `setWindowFlag()` —— 窗口可见时它会销毁重建
    #    原生窗口（"闪一下"，跟随器记的几何也全作废）。
    _flags = []
    _orig_swf = ui2.setWindowFlag
    ui2.setWindowFlag = lambda *a: _flags.append(a)
    ui2.cfg = new_cfg
    ui2._apply_topmost(runtime=True)
    ui2.setWindowFlag = _orig_swf
    check("运行期改置顶**没有**调 setWindowFlag（不会重建原生窗口）",
          not _flags, repr(_flags))

    # ⚠️⚠️ 而且要**走 follow.set_window_pos**（它有明确的 argtypes）——
    #    自己调 `ctypes.windll.user32.SetWindowPos(h, -2, ...)` 会**静默失败**：
    #    不设 argtypes 时指针宽度的参数按 32 位传，`HWND_NOTOPMOST(-2)` 是负数，
    #    x64 下高 32 位不保证符号扩展 → 内核收到无效句柄 → 返回 0、不报错。
    #    （真窗口的真值表在 `probes/qt/check_topmost.py`，这里钉住"用了哪个入口"。）
    from wechat_assistant import follow as _follow
    zcalls = []
    _orig_swp = _follow.set_window_pos
    _follow.set_window_pos = lambda h, ins, fl: zcalls.append((h, ins, fl)) or True
    try:
        ui2.cfg = Cfg({"ui": {"opacity": 1.0, "attach": "off", "topmost": True}})
        ui2._apply_topmost(runtime=True)
        check("运行期置顶走 follow.set_window_pos（带 argtypes 的那个入口）",
              len(zcalls) == 1, repr(zcalls))
        check("  而且传的是 HWND_TOPMOST（不是自己写 -1）",
              zcalls and zcalls[0][1] == _follow.HWND_TOPMOST,
              repr(zcalls))
        zcalls.clear()
        ui2.cfg = Cfg({"ui": {"opacity": 1.0, "attach": "dock", "topmost": True}})
        ui2._apply_topmost(runtime=True)
        check("取消置顶传 HWND_NOTOPMOST",
              zcalls and zcalls[0][1] == _follow.HWND_NOTOPMOST, repr(zcalls))
    finally:
        _follow.set_window_pos = _orig_swp

    # ⚠️ 「总是置顶」**只在「不跟随」时才生效**（用户 2026-10-14 的口径：
    #    「总是置顶是在不跟随才置顶」）。跟随时由跟随器在微信前台时抬它，
    #    常年置顶会让你切到别的程序时它还压在最上面。
    print("\n16b. 「总是置顶」只在「不跟随」时生效")
    for attach, topmost, want in (("dock", True, False), ("float", True, False),
                                  ("off", True, True), ("off", False, False),
                                  ("dock", False, False)):
        ui2.cfg = Cfg({"ui": {"opacity": 1.0, "attach": attach, "topmost": topmost}})
        check("attach=%-5s topmost=%-5s -> 置顶=%s" % (attach, topmost, want),
              ui2._want_topmost() is want, repr(ui2._want_topmost()))
    _safe(lambda: ui2.close())

    # ⚠️ 面板**再矮也不会矮过** `BASE_MIN_HEIGHT`（用户 2026-10-14 要求
    #    「默认高度最小改大一点，大概就是目前微信高度」——他机器上微信常规窗口是
    #    623 设备像素 = 415 基准，所以取 420 基准 ≈ 630 设备像素）。
    #    判据两条：① 内容算法算出来的下限**不低于**它；② 面板开起来的默认高度就是它。
    print("\n16c. 面板最小高度 = BASE_MIN_HEIGHT（不再缩成窄窄一条）")
    cfg3 = Cfg({"ui": {"width": 340, "theme": "dark", "frameless": True,
                       "min_width": 200, "width_max": 640,
                       "opacity": 1.0, "topmost": False}})
    ui3 = FloatingAssistant(cfg3, logger=lambda m: None)
    ui3.show()
    pump(app, 200)
    _floor = px(BASE_MIN_HEIGHT)
    check("BASE_MIN_HEIGHT = %d 基准（= %d 设备像素）" % (BASE_MIN_HEIGHT, _floor),
          _floor >= px(420), "px(%d) = %d" % (BASE_MIN_HEIGHT, _floor))
    check("min_height() 不低于它（内容算出来更小也夹到它）",
          ui3.min_height() >= _floor,
          "min_height=%d vs floor=%d" % (ui3.min_height(), _floor))
    check("面板开起来的默认高度就是它",
          ui3.height() >= _floor,
          "height=%d vs floor=%d" % (ui3.height(), _floor))
    # 真实数字记得住：他机器上微信窗口 623 设备像素 -> 面板 630，基本齐平
    check("  和「一屏三条回复」的真实需要比，垫高了但没离谱",
          _floor <= px(520), "px(%d) = %d" % (BASE_MIN_HEIGHT, _floor))
    _safe(lambda: ui3.close())

    print("\n" + "=" * 70)
    print("通过 %d / 失败 %d / 跳过 %d" % (PASS, FAIL, SKIP))
    return 1 if FAIL else 0


# ---------------------------------------------------------------- 小工具

def scroll_children(ui):
    """滚动区里的直接内容部件。"""
    out = []
    lay = ui.scroll._lay
    for i in range(lay.count()):
        item = lay.itemAt(i)
        w = item.widget()
        if w is not None:
            out.append(w)
        sub = item.layout()
        if sub is not None:
            for j in range(sub.count()):
                w2 = sub.itemAt(j).widget()
                if w2 is not None:
                    out.append(w2)
    return out


def cards(ui):
    from wechat_assistant.qt_ui.cards import Card

    return [w for w in scroll_children(ui) if isinstance(w, Card)]


def find_label(w):
    """控件自身的文字（QLabel 直接取，容器组件里没有就返回 None）。"""
    try:
        if hasattr(w, "text") and callable(w.text):
            return w.text()
    except Exception:
        return None
    return None


if __name__ == "__main__":
    sys.exit(main())
