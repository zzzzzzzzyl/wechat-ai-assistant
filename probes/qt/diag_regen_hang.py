# -*- coding: utf-8 -*-
"""复现「点生成建议整个程序卡死」。

    python probes/qt/diag_regen_hang.py

做法：把 `on_regenerate` 换成一个**假装在干活**的子线程回调（不调 API、不花钱），
然后真的 `click()` 按钮，看主线程还转不转得动。
⚠️ 必须**真点按钮**，不能直接调 `_regen()` —— 卡死的成因很可能就在
「在按钮自己的 clicked 处理器里把它 disable 掉」这条路上。
"""
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from PySide6 import QtCore                                        # noqa: E402

from wechat_assistant import dpi                                  # noqa: E402

dpi.enable()
from wechat_assistant.qt_ui import base                           # noqa: E402
from wechat_assistant.qt_ui.panel import FloatingAssistant        # noqa: E402


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


def heartbeat(app, ms, label):
    """转 ms 毫秒事件循环，数它转了多少次 —— 卡死的话次数会停住。"""
    n = [0]
    t = QtCore.QTimer()
    t.timeout.connect(lambda: n.__setitem__(0, n[0] + 1))
    t.start(10)
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.002)
    t.stop()
    print("    %s: 事件循环转了 %d 拍" % (label, n[0]))
    return n[0]


def main():
    app = base.init_app([])
    print("平台=%s 缩放=%.2f" % (app.platformName(), base.scale()))

    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark"}})
    got = {}
    ui = FloatingAssistant(
        cfg, logger=lambda m: print("      [log] %s" % m),
        on_regenerate=lambda intent: got.update(intent=intent))
    ui.resize(base.px(340), base.px(520))
    ui.show()
    heartbeat(app, 300, "建好之后")
    ui.start_pump()

    print("\n[1] 直接调 _regen()（不走按钮）")
    t0 = time.time()
    ui._regen()
    print("    _regen 返回耗时 %.0f ms，回调收到 intent=%r"
          % ((time.time() - t0) * 1000, got.get("intent")))
    heartbeat(app, 300, "    之后")
    got.clear()

    print("\n[2] 真的点按钮（btn_gen.click()）")
    got.clear()
    t0 = time.time()
    ui.btn_gen.click()
    print("    click() 返回耗时 %.0f ms，回调收到 intent=%r"
          % ((time.time() - t0) * 1000, got.get("intent")))
    heartbeat(app, 400, "    之后")

    print("\n[3] 点按钮时按钮被 disable 了没有")
    print("    enabled=%s text=%r" % (ui.btn_gen.isEnabled(), ui.btn_gen.text()))
    ui.set_busy(False)
    heartbeat(app, 200, "    set_busy(False) 之后")
    print("    enabled=%s text=%r" % (ui.btn_gen.isEnabled(), ui.btn_gen.text()))

    print("\n[4] 输入框里有字时再点一次")
    ui.input.set("今晚想吃火锅")
    ui._mirror_intent()
    ui.btn_gen.click()
    heartbeat(app, 300, "    之后")
    print("    回调收到 intent=%r" % got.get("intent"))

    print("\n[5] 连点两次（第二次应在 busy 里被忽略）")
    ui.set_busy(False)
    ui.btn_gen.click()
    ui.btn_gen.click()
    heartbeat(app, 300, "    之后")

    ui.stop()
    print("\n如果每一段都打印了『事件循环转了 N 拍』且 N > 0，就没卡死。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
