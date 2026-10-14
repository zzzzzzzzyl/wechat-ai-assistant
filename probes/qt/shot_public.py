# -*- coding: utf-8 -*-
"""给**对外页面**出图：把悬浮窗用一段**假对话**渲染成 PNG。

    python probes/qt/shot_public.py            # 存到 out/public_*.png

为什么单独写一个探针
--------------------
1. ⚠️⚠️ **`out/` 里那批 `pw_*.png` / `s*_chat.png` 是抓真实微信窗口的**，
   里面是**真的聊天记录**（会话名、真人昵称、聊天内容）。
   它们只能当语料/取证用，**绝对不能贴到公开网页上**。
   这个探针一条真实数据都不读：对话内容是硬编码的假数据。
2. 悬浮窗平时跟着微信走，**真机截图躲不开别人的窗口**（遮挡、隐私、分辨率都不受控）。
   这里用 `QWidget.grab()` 把控件自己渲染成位图 —— 尺寸、主题、DPI 都可控。
3. 出两张（深色 / 浅色），对外页面直接引用。

⚠️ 走的是**产品代码自己的投递接口**（`post_target` / `post_replies` / `post_status`），
   不是自己拼视图 —— 否则图画出来和真机不是一回事（那就是假图）。
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

OUT = os.path.join(ROOT, "out")

# ⚠️ 假数据。会话名、昵称、聊天内容全是编的 —— 别把真聊天记录粘进来。
FAKE_SESSION = "小林"
FAKE_TARGET = (
    "小林：周末有空吗？想找人一起去爬山\n"
    "我：我得看下天气\n"
    "小林：周六晴，20 度，早上八点山脚下等？"
)
FAKE_REPLIES = [
    "周六可以，八点山脚下等我，我提前十分钟到",
    "行，那我把水和吃的都带上，你只管爬",
    "先说好，爬到一半我不背你",
]
FAKE_STATUS = "已生成 3 条 · 点一下卡片就复制"


class Cfg:
    """最小配置替身（和 `tests/test_qt_panel.py` 里那个同一个形状）。"""

    def __init__(self, data=None):
        self._data = data or {}

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


def shoot(ui, app, theme: str, path: str):
    """把面板切到 `theme` 渲染一版：假对话 + 三条建议 + 意图行。"""
    ui.set_theme(theme)
    ui.post_session(FAKE_SESSION, False)
    ui.post_target(FAKE_TARGET)
    ui.post_replies(list(FAKE_REPLIES), "")
    ui.post_status(FAKE_STATUS)
    # 「我想说」那一行留空 —— 空的时候它显示占位提示，比塞一句话更能说明这是输入框。
    pump(app, 900)

    pix = ui.grab()
    pix.save(path, "PNG")
    print("[shot] %-6s %s  %dx%d" % (theme, path, pix.width(), pix.height()))
    return pix


def main():
    from PySide6 import QtCore, QtWidgets          # noqa: F401
    from wechat_assistant import dpi
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.panel import BASE_WIDTH, FloatingAssistant

    # ⚠️ 和 `run_watch.py` 一样**先开 DPI 感知**：不开的话本进程按 96 dpi 算，
    #    出图会缩水一半（本机 150%）。
    dpi.enable()
    app = base.init_app([])
    px = base.px

    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark",
                      "frameless": True, "min_width": 200, "width_max": 640}})
    ui = FloatingAssistant(cfg, logger=lambda m: None,
                           on_regenerate=lambda intent: None,
                           on_read=lambda: None,
                           on_clear=lambda: None,
                           on_settings=lambda: None,
                           session_source=lambda: {},
                           on_mode_change=lambda a, b: None,
                           on_config_changed=lambda c: None)
    ui.resize(px(BASE_WIDTH), px(600))
    ui.show()
    pump(app, 300)
    ui.start_pump()

    os.makedirs(OUT, exist_ok=True)
    shoot(ui, app, "dark", os.path.join(OUT, "public_panel_dark.png"))
    shoot(ui, app, "light", os.path.join(OUT, "public_panel_light.png"))

    ui.stop()
    print("[shot] 完成（图在 out/public_panel_*.png，可以拷进站点 public/images/）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
