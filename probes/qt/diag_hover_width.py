# -*- coding: utf-8 -*-
"""「鼠标放到【生成建议】上，右边框就往右变宽」到底是哪儿来的。

用户的描述：点了【只看对话】之后（内容能上下滚），
鼠标移到【生成建议】按钮上，面板右边框会往右鼓出去。

嫌疑名单（都要量，别猜）：
  ① 按钮 hover 换了样式表 -> Qt 重新 polish -> sizeHint 变了 -> 布局把窗口顶宽
  ② 【只看对话】那些长卡片把布局的最小宽度顶上去了（跟 hover 无关，是"点了之后"才有）
  ③ 滚动条出现/消失改变了内容宽度（用户说的"可上下拖动"＝滚得动了）

用法：python probes/qt/diag_hover_width.py
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))) 

from PySide6 import QtWidgets  # noqa: E402


def pump(app, ms=150):
    """转一段**真实时间**的事件循环。

    ⚠️ 不能写 `for _ in range(N): app.processEvents()` —— 那样几乎不花时间，
        而 `_pump` 挂在 QTimer 上，**定时器要等真实时间到期才会响**。
        第一版就是这么写的，于是"塞了 25 条长内容"之后 `content_height`
        一直是 18，后面所有关于滚动条的结论**全是无效的**（白量一轮）。
    """
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


class Cfg:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, path, default=None):
        node = self._data
        for part in str(path).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def main():
    print("=" * 96)
    print("hover 生成建议 会不会把面板顶宽 / 滚动条是不是被 hover 触发的")
    print("=" * 96)

    from wechat_assistant import dpi
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.panel import FloatingAssistant

    dpi.enable()
    app = base.init_app([])
    px = base.px

    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark",
                      "frameless": True, "min_width": 200, "width_max": 640}})
    ui = FloatingAssistant(cfg, logger=lambda m: None,
                           on_regenerate=lambda i: None,
                           on_read=lambda: None,
                           on_clear=lambda: None,
                           on_settings=lambda: None,
                           session_source=lambda: {"会话": {}},
                           on_mode_change=lambda a, b: None,
                           on_config_changed=lambda c: None)
    ui.resize(px(340), px(560))
    ui.show()
    pump(app, 200)
    ui.start_pump()
    pump(app, 200)

    def state(tag):
        pump(app, 250)
        vb = ui.scroll.verticalScrollBar()
        print("  %-26s ui.width=%-5d 布局下限=%-5d 滚动条可见=%-5s "
              "滚动条宽=%-3d 视口宽=%-5d content=%-5d btn_gen.w=%-4d"
              % (tag, ui.width(), ui._root_lay.minimumSize().width(),
                 vb.isVisible(), vb.width(), ui.scroll.viewport().width(),
                 ui.scroll.content_height(), ui.btn_gen.width()))

    def hover(on):
        ui.btn_gen._hover = bool(on)
        ui.btn_gen._apply()
        pump(app, 120)

    print("\n[1] 空白面板（还没点只看对话）")
    state("初始")
    hover(True)
    state("hover 生成建议")
    hover(False)
    state("移开")

    print("\n[2] 塞一堆长回复（模拟点了【只看对话】之后的内容）")
    lines = ["对方：这是一条比较长的聊天消息，用来模拟真实会话里那种一整行很长的文字内容，"
             "看看它会不会把面板的布局最小宽度顶上去。"] * 25
    ui.post_replies(lines, "", "")
    state("长内容")
    hover(True)
    state("长内容 + hover 生成建议")
    hover(False)
    state("移开")

    print("\n[3] 关键：窄面板（用户日志里是 406）")
    ui.set_docked(100, 100, 406, 700)
    state("窄面板 + 长内容")
    hover(True)
    state("窄 + hover 生成建议")
    hover(False)
    state("移开")
    for _ in range(5):
        hover(True)
        hover(False)
    state("来回 hover 5 次之后")

    print("\n[4] 逐个量：谁的最小宽度最大（长内容 + 窄面板下）")
    rows = []
    for w in ui.findChildren(QtWidgets.QWidget):
        try:
            rows.append((w.minimumSizeHint().width(), w))
        except Exception:
            continue
    rows.sort(key=lambda t: -t[0])
    for val, w in rows[:8]:
        txt = ""
        try:
            if hasattr(w, "text") and callable(w.text):
                txt = repr(w.text())[:36]
        except Exception:
            pass
        print("  %5d  %-14s %s" % (val, type(w).__name__, txt))

    print("\n[5] 边缘热区（右边框会不会是宽度拖拽的热区）")
    print("    zone = px(6) = %d px；窗口宽 %d" % (px(6), ui.width()))
    g = ui.btn_gen.geometry()
    parent = ui.btn_gen.parent()
    pg = parent.geometry() if parent is not None else None
    if pg is not None:
        print("    btn_gen 在窗口里的横向范围 = %d .. %d，窗口宽 %d，"
              "右缘热区从 %d 起"
              % (pg.x() + g.x(), pg.x() + g.x() + g.width(), ui.width(),
                 ui.width() - px(6)))
    for x in (5, 20, 100, int(ui.width() / 2),
              ui.width() - 30, ui.width() - 10, ui.width() - 4):
        print("    鼠标在 x=%-4d -> _near_h_edge = %d" % (x, ui._near_h_edge(x)))

    print("\n[6] 真凶候选：**状态栏那行小字**。",
          "它是个**不换行**的 QLabel，")
    print("      不换行的 QLabel，最小宽度 = 整行文字的宽度 ——",
          "而它在根布局里，")
    print("      于是**它就是面板的最小宽度**。状态文字一变长，",
          "窗口就被顶着变宽。")
    ui.post_status("已生成 25 条 · 点一下卡片就复制")
    state("状态：中等长度")
    short = "已生成 25 条"
    ui.post_status(short)
    state("状态：短")
    ui.set_docked(100, 100, 300, 700)
    state("把窗口架到 300（比状态栏窄）")
    ui.post_status("只看对话，未调用 AI（点【生成建议】或按热键才会生成）")
    state("状态：很长（点只看对话之后就是这个）")
    ui.post_status(short)
    state("状态：换回短的")
    lb = ui.lbl_status
    print("    lbl_status.wordWrap()=%s  minimumSizeHint=%d  当前宽=%d"
          % (lb.wordWrap(), lb.minimumSizeHint().width(), lb.width()))
    for txt in ("短", "已生成 25 条 · 点一下卡片就复制",
                "只看对话，未调用 AI（点【生成建议】或按热键才会生成）"):
        lb.setText(txt)
        print("      状态 = %-30s -> lbl.minimumSizeHint=%d"
              % (repr(txt)[:30] + "…" * (len(txt) > 26),
                 lb.minimumSizeHint().width()))
    lb.setText(short)
    pump(app, 150)

    print("\n结论看上面：hover 前后 ui.width 变没变。")
    print("      变了 -> 按钮样式表或滚动条把窗口顶宽了；")
    print("      没变 -> 那是 hover 触发了重排、把某个本来就算错的东西显形了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
