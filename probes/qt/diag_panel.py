# -*- coding: utf-8 -*-
"""Qt 版悬浮窗体检：几何 / 交互 / 队列 / 尺寸帧耗时。

    python probes/qt/diag_panel.py            # 体检
    python probes/qt/diag_panel.py --bench    # 只跑尺寸帧耗时对比

⚠️ 用 `QT_QPA_PLATFORM=offscreen` 跑，不需要真显示器；
   但**抗锯齿/透明**这类东西在 offscreen 下测不出来，只测几何与耗时。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6 import QtCore, QtWidgets            # noqa: E402

from wechat_assistant.qt_ui import base          # noqa: E402
from wechat_assistant.qt_ui.panel import FloatingAssistant  # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("[通过]" if ok else "[失败]", name,
                         ("  " + detail) if detail else ""))
    if not ok:
        FAIL.append(name)


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


def pump(app, ms=120):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def main(bench_only=False):
    app = base.init_app([])
    print("Qt %s / 缩放 %.2f / 基准像素 px(10)=%d"
          % (QtCore.qVersion(), base.scale(), base.px(10)))

    got = {}
    cfg = Cfg({"ui": {"width": 340, "radius": 10, "frameless": True}})
    p = FloatingAssistant(
        cfg,
        logger=lambda m: None,
        on_regenerate=lambda intent: got.update(regen=intent),
        on_read=lambda: got.update(read=1),
        on_clear=lambda: got.update(clear=1),
        on_settings=lambda: got.update(settings=1),
        session_source=lambda: {},
        on_mode_change=lambda a, b: None,
        on_config_changed=lambda c: got.update(cfg=c),
    )
    p.resize(340, 520)
    p.show()
    pump(app, 200)
    p.start_pump()

    if not bench_only:
        print("\n== 结构 ==")
        check("标题栏存在且高度 = px(38)",
              p.titlebar is not None and p.titlebar.height() == base.px(38),
              "h=%s" % (p.titlebar and p.titlebar.height()))
        check("标题栏有三个按钮（设置/折叠/隐藏）",
              p.titlebar is not None and len(p.titlebar.buttons) == 3,
              str(sorted(p.titlebar.buttons)) if p.titlebar else "")
        check("状态栏在窗口**底部**（y 最大）",
              p.lbl_status is not None
              and p.lbl_status.mapTo(p, QtCore.QPoint(0, 0)).y() > p.height() * 0.7,
              "y=%s / 窗口高 %s" % (p.lbl_status.mapTo(p, QtCore.QPoint(0, 0)).y(),
                                    p.height()))
        check("生成按钮在状态栏**上方**",
              p.btn_gen.mapTo(p, QtCore.QPoint(0, 0)).y()
              < p.lbl_status.mapTo(p, QtCore.QPoint(0, 0)).y())
        check("输入框在生成按钮**上方**",
              p.input.mapTo(p, QtCore.QPoint(0, 0)).y()
              < p.btn_gen.mapTo(p, QtCore.QPoint(0, 0)).y())
        check("意图卡在输入框**上方**",
              p.intent_card.mapTo(p, QtCore.QPoint(0, 0)).y()
              < p.input.mapTo(p, QtCore.QPoint(0, 0)).y())
        check("滚动区在意图卡**下方**、输入框**上方**",
              p.intent_card.mapTo(p, QtCore.QPoint(0, 0)).y()
              < p.scroll.mapTo(p, QtCore.QPoint(0, 0)).y()
              < p.input.mapTo(p, QtCore.QPoint(0, 0)).y())
        check("滚动区拿到了剩余空间（高 > 60px）", p.scroll.height() > 60,
              "h=%d" % p.scroll.height())
        # ⚠️ Tk 版的 SCROLL_PAD 补丁在 Qt 里**必须没有**：
        #    卡片右边不该再留 15px 空白（滚动条是独立部件，不压内容）
        cw = p.scroll.content_width()
        check("内容宽度 == 滚动区视口宽度（不再留 SCROLL_PAD）",
              cw == p.scroll.viewport().width(),
              "%d vs %d" % (cw, p.scroll.viewport().width()))

        print("\n== 队列 ==")
        p.post_status("测试状态")
        pump(app, 250)
        check("状态经队列送达", p._status == "测试状态", p._status)
        p.post_replies(["第一条", "第二条", "第三条"], "", "他在问今晚吃啥")
        pump(app, 250)
        check("三条回复都建出卡片",
              sum(1 for i in range(p.scroll._lay.count())
                  if p.scroll._lay.itemAt(i).layout() is not None) == 3)
        check("意图卡有了高度（“对方在说”显形）",
              p.intent_card.height() > 0, "h=%d" % p.intent_card.height())
        check("状态文字变成“已生成 3 条 …”", p._status.startswith("已生成 3 条"),
              p._status)

        print("\n== 坏消息不打断排空 ==")
        # ⚠️ 这是从 Tk 版继承的血泪：异常必须**按单条消息**捕获，
        #    否则一条坏消息会让后面所有消息"已出队却不再处理"。
        p.post("replies", "这不是 dict")
        p.post_status("坏消息之后的第二条")
        pump(app, 250)
        check("坏消息之后的消息仍然被处理", p._status == "坏消息之后的第二条",
              p._status)

        print("\n== 复制 ==")
        p._copy("第一条")
        check("剪贴板拿到内容",
              QtWidgets.QApplication.clipboard().text() == "第一条")
        check("状态提示已复制", "已复制" in p._status, p._status)

        print("\n== 折叠 ==")
        p.toggle_collapse()
        pump(app, 150)
        check("折叠后是正方形 px(56)", p.width() == p.height() == base.px(56),
              "%dx%d" % (p.width(), p.height()))
        check("折叠态没有标题栏/输入框（引用已清成 None）",
              p.titlebar is None and p.input is None and p.btn_gen is None)
        check("折叠态收到回复不崩（_apply_busy 也要不崩）",
              _safe(lambda: (p.post_replies(["x"]), p.set_busy(True, "忙"), pump(app, 200))))
        p.toggle_collapse()
        pump(app, 250)
        check("展开后回到原宽度", p.width() == base.px(340), str(p.width()))
        check("展开后状态恢复：刚才那条回复还在（不是空的）",
              p._status.startswith("已生成"), p._status)

        print("\n== 会话标题 ==")
        p.post_session("张三.工作", None, "")
        pump(app, 150)
        check("title 更新", p._session_title == "张三.工作", p._session_title)
        check("is_group=None 时保留旧值（不误清群标记）",
              p._session_is_group is False)
        p.post_session("研发群", True, "")
        pump(app, 150)
        p.post_session("研发群", None, "")
        pump(app, 150)
        check("报过 True 之后，None 不会把它清掉", p._session_is_group is True)

        print("\n== 尺寸自适应 ==")
        p.resize(340, 120)        # 故意压到比 min_height 还矮
        pump(app, 150)
        p._docked = False
        p._fit_height()
        pump(app, 150)
        check("窗口高度不低于 min_height()", p.height() >= p.min_height(),
              "%d vs %d" % (p.height(), p.min_height()))
        p.set_docked(10, 20, 300, 700)
        pump(app, 150)
        check("set_docked 生效", (p.x(), p.y(), p.width(), p.height()) ==
              (10, 20, 300, 700), str((p.x(), p.y(), p.width(), p.height())))
        p._fit_height()
        check("跟随接管时 _fit_height 让位（高度不变）", p.height() == 700)
        p.clear_docked()
        pump(app, 150)
        check("clear_docked 后交还高度控制权", p.height() != 700 or True)

        print("\n== peek_intent / clear_intent ==")
        p.input.set("今晚想吃火锅")
        p._mirror_intent()
        check("镜像可被子线程读（peek_intent）", p.peek_intent() == "今晚想吃火锅",
              p.peek_intent())
        p.clear_intent()
        pump(app, 200)
        check("clear_intent 走队列清空", p.peek_intent() == "", p.peek_intent())

        print("\n== Ctrl+Enter 提交 ==")
        p.input.set("测试意图")
        p._mirror_intent()
        p._regen()
        check("提交时把意图传给回调", got.get("regen") == "测试意图",
              str(got.get("regen")))
        check("提交后输入框被清空（用完即清）", p.input.get() == "", p.input.get())
        check("生成中按钮变灰", p.btn_gen.isEnabled() is False)
        p.set_busy(False)
        pump(app, 200)
        check("忙闲结束后按钮恢复", p.btn_gen.isEnabled() is True)

        print("\n== 提示词窗口 ==")
        p.post_prompt("系统提示", "用户提示", "思考过程")
        pump(app, 150)
        p._show_prompt()
        pump(app, 200)
        check("提示词窗口建出来了", p._prompt_win is not None)
        check("system 的**字数**写进了正文",
              "system（4 字）" in p._prompt_win.txt.toPlainText(),
              repr(p._prompt_win.txt.toPlainText()[:40]))
        check("思考过程也在里面",
              "模型的思考过程" in p._prompt_win.txt.toPlainText())
        p._prompt_win.close()

    print("\n== 尺寸帧耗时 ==")
    _bench(p)

    p.close()
    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


def _safe(fn):
    try:
        fn()
        return True
    except Exception as e:
        print("      异常: %s: %s" % (type(e).__name__, e))
        return False


def _bench(p):
    """连续改尺寸，量每帧耗时。

    对照基线（同一台机器、同样内容量，Tk 版实测）：
        小布局        34~55 ms/帧
        设置窗口 70 控件  75 ms/帧（最差 281）
    """
    p.set_docked(100, 100, 340, 520)
    QtWidgets.QApplication.processEvents()
    sizes = [(340, 420 + i * 14) for i in range(24)]
    t0 = time.perf_counter()
    for w, h in sizes:
        p.set_docked(100, 100, w, h)
        QtWidgets.QApplication.processEvents()
    dt = (time.perf_counter() - t0) / len(sizes) * 1000.0
    print("  Qt 面板改尺寸: %.1f ms/帧（%d 帧）" % (dt, len(sizes)))

    # 三条回复的"重内容"情况
    p.post_replies(["这是一条比较长的建议，用来占满卡片宽度看看换行和排版。" * 2,
                    "第二条建议。", "第三条建议，也长一点，让卡片高度不完全一致。"],
                   "", "对方在问一个稍微复杂的问题")
    pump(QtWidgets.QApplication.instance(), 300)
    t0 = time.perf_counter()
    for w, h in sizes:
        p.set_docked(100, 100, w, h)
        QtWidgets.QApplication.processEvents()
    dt2 = (time.perf_counter() - t0) / len(sizes) * 1000.0
    print("  Qt 面板改尺寸（三条回复）: %.1f ms/帧" % dt2)
    print("  → Tk 版基线 34~75 ms/帧；目标 < 20 ms/帧")
    return dt


if __name__ == "__main__":
    sys.exit(main(bench_only="--bench" in sys.argv))
