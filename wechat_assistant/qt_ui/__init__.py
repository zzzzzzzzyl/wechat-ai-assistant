# -*- coding: utf-8 -*-
"""Qt 版界面层（`wechat_assistant.qt_ui`）。

从 Tk 迁移而来。子模块：

    base.py        QApplication 初始化 / 缩放 / Style / 小工具
    widgets.py     胶囊按钮 / 混色等基础件
    cards.py       卡片 / 开关
    inputs.py      圆角输入框（多行 / 单行）
    layout.py      滚动区 / 换行组 / 圆角透明面板（Panel）
    panel.py       **悬浮窗** FloatingAssistant
    settings_ui.py 设置窗口
    session_ui.py  会话级设置面板

⚠️ **业务层不在这里**：reader / recorder / ai / store / context / follow /
   window / sessions 等模块完全与界面无关。
   （`follow.py` 里只有"怎么问窗口几何"那几个访问器跟界面打交道，
   见该模块的 `ui_native_handle` / `ui_rect`。）

⚠️ **界面只有这一套。** Tk 版（`wechat_assistant/ui.py`、`widgets.py`、
   `settings_ui.py`、`session_ui.py`、`winchrome.py`）已于 2026-09-26 删除。
   之前是两套并存、靠 `ui.backend` 切换；删掉的理由是两套的观感与尺寸
   各自在漂，留着只会让人分不清"现在跑的到底是哪套"。
   **不要再往仓库里加第二套 UI。**
"""

from .base import Style, init_app, px, scale  # noqa: F401

__all__ = ["Style", "init_app", "px", "scale"]
