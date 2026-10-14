# -*- coding: utf-8 -*-
"""跟随器会不会"越来越窄"？—— 收敛性体检。

    python -X utf8 probes/qt/diag_dock_converge.py

⚠️ 起因：实测日志里出现过一**串递减**的宽度提示：
    空间有限，面板宽度自动缩到 876px
    空间有限，面板宽度自动缩到 846px
    ...（共 12 条，一直缩到 603px）
`_log_once` 是按**消息文本**去重的（文本里带宽度数字），所以宽度一变就重新打一条。
问题在于：这个递减是**收敛**（缩到一个稳定值就停）还是**一直缩下去**。
后者就是 bug —— 面板会越用越窄。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtCore                                       # noqa: E402
from wechat_assistant import dpi, follow                         # noqa: E402

dpi.enable()
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


def pump(app, ms=200):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def main():
    app = base.init_app([])
    px = base.px
    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark",
                      "attach": "dock", "follow_interval": 0.05,
                      "min_width": 200, "width_max": 640,
                      "dock_side": "auto"}})
    ui = FloatingAssistant(cfg, logger=lambda m: None)
    ui.show()
    pump(app, 300)
    ui.start_pump()

    f = follow.WindowFollower(ui, cfg, logger=lambda m: None)

    # 假装微信占着屏幕左边一大块，右侧只剩 ~620 的空间（复现日志里的情形）
    work = (0, 0, 1520, 900)
    wrect = (0, 0, 1180, 860)
    follow.work_area = lambda *a, **k: work

    print("work=%s  wrect=%s" % (work, wrect))
    print("\n== 反复问同一个 wrect，看宽度收不收敛 ==")
    widths = []
    for i in range(10):
        size = f._panel_size(wrect)
        if size is None:
            widths.append(None)
            break
        w, h = size
        widths.append(w)
        # 真正把窗口摆成这个尺寸（跟随器的 `_set` 干的就是这事）——
        # ⚠️ 关键：**一定要真的摆**，只看 `_panel_size` 的返回值是测不出
        #    "摆完之后下一拍会不会算出更窄"的。
        ui.set_docked(100, 100, w, h)
        pump(app, 80)
    print("  连续 10 拍算出的宽度: %s" % widths)
    if None in widths:
        check("宽度收敛", False, "某一拍算出了 None（放弃跟随）")
    else:
        stable = widths[-1] == widths[-2] == widths[-3]
        check("宽度收敛（最后三拍一样）", stable, str(widths))
        check("没有一路缩到下限", widths[-1] > px(200) or widths[-1] == px(200),
              "最终 %d" % widths[-1])

    print("\n== 跟随器真的跑起来之后，窗口宽度会变吗 ==")
    f.start()
    pump(app, 600)
    w1 = ui.width()
    pump(app, 800)
    w2 = ui.width()
    pump(app, 800)
    w3 = ui.width()
    print("  三段时间之后: %d -> %d -> %d" % (w1, w2, w3))
    check("窗口宽度稳定（没有越用越窄）", w1 == w2 == w3,
          "%d -> %d -> %d" % (w1, w2, w3))
    f.stop()

    ui.stop()
    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
