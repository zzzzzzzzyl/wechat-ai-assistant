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
FAKE_SESSION = "李工"
FAKE_TARGET = (
    "李工：明天上午的评审你几点能到？\n"
    "我：九点前应该行\n"
    "李工：那我八点四十在楼下等你，顺便把上次那份需求过一遍？"
)
FAKE_REPLIES = [
    "行，八点四十楼下见，我把需求打印一份带上",
    "九点前到没问题，堵车的话你们先上去，我直接进会议室",
    "需求让我先看十分钟吧，不然会上现翻太慢了",
]
FAKE_STATUS = "已生成 3 条 · 点一下卡片就复制"

# 模型对「对方在说什么」的判断 —— 面板上会单独显示一张卡（`IntentCard`）。
# ⚠️ 同样是编的：真机上是模型返回的 `intent` 字段，由 `ai.parse_intent()` 抠出来。
# ⚠️ 卡片自己带「对方在说：」前缀，**这里别再把"对方"写进去**（会读成"对方在说：对方…"）。
FAKE_INTENT = "他在确认明早评审的到场时间，顺便想提前把那份需求过一遍"

# 「只看对话」窗口里显示的东西：程序这次读到的对话，目标那几条前面带 `→`。
FAKE_READ = (
    "李工：评审材料我刚才发到群里了\n"
    "我：收到，晚上看一下\n"
    "→ 李工：明天上午的评审你几点能到？\n"
    "→ 我：九点前应该行\n"
    "→ 李工：那我八点四十在楼下等你，顺便把上次那份需求过一遍？\n"
)

# 「看提示词」窗口里显示的东西：这一次真正发出去的内容（截取版，够看清结构就行）。
FAKE_PROMPT = """你是一个聊天助手。下面是人设、和对方的关系，以及你平时说话的样子。

【人设】25 岁，男生，说话直接，不绕弯子。
【关系】同组同事，一起做项目两年，平时互相催进度。
【口吻样本】
收到，我下午看
行，那就按这个来
我先拉个文档，晚点同步给你

【最高优先级：我想说】
可以早点到，但需求得他先给我

【硬性禁止，必须遵守】
1. 不要用"好的呢""没问题呀"这种语气词
2. 不要解释你为什么这么回，只给候选

【上下文规则】
1. 上面的对话历史可能和当前话题无关，别硬套
2. 以【当前屏幕上的对话】为准

【输出要求】
输出 3 条候选，每条一行，前面带序号。

================ user ================

【对话历史】
李工：评审材料我刚才发到群里了
我：收到，晚上看一下

【当前屏幕上的对话 —— 要回复的就是这一段】
李工：明天上午的评审你几点能到？
我：九点前应该行
李工：那我八点四十在楼下等你，顺便把上次那份需求过一遍？
"""


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
    ui.post_replies(list(FAKE_REPLIES), "", FAKE_INTENT)
    ui.post_status(FAKE_STATUS)
    # 「我想说」那一行留空 —— 空的时候它显示占位提示，比塞一句话更能说明这是输入框。
    pump(app, 900)

    pix = ui.grab()
    pix.save(path, "PNG")
    print("[shot] %-6s %s  %dx%d" % (theme, path, pix.width(), pix.height()))
    return pix


def save_window(w, name: str):
    """抓一个已经摆好内容的窗口（只用它自己的位图，不抓屏幕）。"""
    pix = w.grab()
    path = os.path.join(OUT, name)
    pix.save(path, "PNG")
    print("[shot] %s  %dx%d" % (path, pix.width(), pix.height()))
    w.close()


def shot_settings(app, group: str, name: str, values: dict):
    """渲染「设置 → 某个分组」那一页 —— 给用户看**东西填在哪**。

    ⚠️ 三层配置全部指到临时目录（`SettingsStore` 的路径都能传），
       **绝不碰真 `state/`**；里面的地址、模型、Key、人设都是编的。
    """
    import json
    import shutil
    import tempfile

    from wechat_assistant.qt_ui import base              # noqa: F401
    from wechat_assistant.qt_ui.settings_ui import SettingsWindow
    from wechat_assistant.settings import SettingsStore

    tmp = tempfile.mkdtemp(prefix="wai_shot_")
    try:
        shutil.copy2(os.path.join(ROOT, "config.yaml"),
                     os.path.join(tmp, "config.yaml"))
        with open(os.path.join(tmp, "settings.json"), "w",
                  encoding="utf-8") as f:
            json.dump(values, f, ensure_ascii=False)
        with open(os.path.join(tmp, "secrets.yaml"), "w",
                  encoding="utf-8") as f:
            f.write("ai:\n  api_key: sk-xxxxxxxxxxxxxxxxxxxx\n")

        store = SettingsStore(config_path=os.path.join(tmp, "config.yaml"),
                              settings_path=os.path.join(tmp, "settings.json"),
                              secrets_path=os.path.join(tmp, "secrets.yaml"))
        cfg = store.load()
        win = SettingsWindow(cfg, store, base.Style(cfg, 0, name="dark"),
                             logger=lambda m: None, on_saved=lambda: None,
                             session_source=lambda: {},
                             on_mode_change=lambda k, m: None)
        win.show_dialogs = False           # 模态框会挡住出图
        win.open(group)
        pump(app, 700)

        pix = win.win.grab()
        path = os.path.join(OUT, name)
        pix.save(path, "PNG")
        print("[shot] %s  %dx%d" % (path, pix.width(), pix.height()))
        win.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    from PySide6 import QtCore, QtWidgets          # noqa: F401
    from wechat_assistant import dpi
    from wechat_assistant.qt_ui import base
    from wechat_assistant.qt_ui.panel import (BASE_WIDTH, FloatingAssistant,
                                              PromptWindow, ReadWindow)

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
    # ⚠️ 高度按内容给：太矮会被 `_min_height()` 顶回去，太高中间会空一大截
    #    （截图不是真机摆放，只是为了好看 —— 真机上高度是跟着微信走的）。
    ui.resize(px(BASE_WIDTH), px(470))
    ui.show()
    pump(app, 300)
    ui.start_pump()

    os.makedirs(OUT, exist_ok=True)
    shoot(ui, app, "dark", os.path.join(OUT, "public_panel_dark.png"))
    shoot(ui, app, "light", os.path.join(OUT, "public_panel_light.png"))

    # ---- 另外两张：「只看对话」/「看提示词」 --------------------------------
    #
    # ⚠️ 这两个窗口平时是产品代码弹出来的，内容来自**真实读屏**；这里一样是假数据。
    #    填充用的是窗口自己的 `txt`（QPlainTextEdit）—— 和真机上产品往里塞文本
    #    是同一个控件、同一套样式表，所以这不是"自己拼的假图"。
    st = base.Style(None, name="dark")

    rw = ReadWindow(st, lambda: None)
    rw.resize(px(760), px(430))
    rw.txt.setPlainText(FAKE_READ)
    rw.show()
    pump(app, 400)
    save_window(rw, "public_read.png")

    pw = PromptWindow(st, lambda: None)
    pw.resize(px(820), px(560))
    pw.txt.setPlainText(FAKE_PROMPT)
    pw.show()
    pump(app, 400)
    save_window(pw, "public_prompt.png")

    ui.stop()
    shot_settings(app, "ai", "public_settings_ai.png",
                  {"ai": {"base_url": "https://api.deepseek.com",
                          "model": "deepseek-chat"}})
    shot_settings(app, "persona", "public_settings_persona.png",
                  {"persona": {
                      "default": "你帮「我」写微信回复，用第一人称，直接能发出去。\n"
                                 "我说话偏短，不太用感叹号，也不用「好的呢」这类语气词。",
                      "default_relationship": "朋友"}})
    print("[shot] 完成（图在 out/public_*.png，可以拷进站点 public/images/）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
