# -*- coding: utf-8 -*-
"""路径解析 —— 程序「文件放在哪」的唯一出口。

为什么需要单独一个模块
----------------------
原先各处都在用 `os.path.dirname(__file__)` 推项目根目录（`config.py` 的 `ROOT`）。
**从源码运行时没问题，打包成 exe 之后会全错**：

    PyInstaller 会把整个程序解压到一个临时目录再运行，
    此时 `__file__` 指向的是那个临时目录，而不是 exe 所在的位置。
    （onefile 模式下每次启动还是**不同的随机目录**。）

后果有三个，而且都不报错，只是"怪怪的"：
    1. 读到的是**打进包里的** `config.yaml`，你在 exe 旁边改的那份完全没生效；
    2. `state/`（会话表、历史库）写进临时目录，**程序一关就没了**；
    3. `secrets.yaml` 找不到 -> API Key 读不到 -> 生成永远失败。

所以把「基准目录是什么」收敛到这一个函数里。以后打包时**不用去翻每一处路径**。

基准目录的规则
--------------
    冻结态（打包后）: exe 所在目录          —— 用户能看见、能改
    源码运行        : 项目根目录            —— 也就是 wechat_assistant 的上一级

⚠️ 改这个函数之前想清楚：它一动，所有文件的位置都跟着动。
"""

import os
import sys

__all__ = [
    "is_frozen", "base_dir", "pkg_dir", "resolve",
    "config_path", "secrets_path", "settings_path",
    "state_dir", "log_dir", "history_path", "log_path",
    "icon_path", "asset_path",
    "ensure_dir", "ensure_parent",
]


def is_frozen() -> bool:
    """是否运行在打包后的 exe 里（PyInstaller / cx_Freeze 都会设 sys.frozen）。"""
    return bool(getattr(sys, "frozen", False))


def pkg_dir() -> str:
    """本包（`wechat_assistant/`）所在目录。

    用途：访问**随程序分发**的只读资源（图标等）。
    打包后它指向临时解压目录 —— 只读资源找它是对的，
    用户可编辑的配置**不要**找它（那要用 `base_dir()`）。
    """
    return os.path.dirname(os.path.abspath(__file__))


def base_dir() -> str:
    """用户数据与配置的基准目录。

    冻结态：exe 所在目录。这样用户能在 exe 旁边看到并编辑 `config.yaml`、
    `secrets.yaml`，数据也留在原地不会被清理。
    源码运行：项目根目录。
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(pkg_dir())


def resolve(*parts) -> str:
    """把相对路径按 `base_dir()` 解析成绝对路径；已经是绝对路径的原样返回。

    `resolve("state/history.db")` -> `<base>/state/history.db`
    """
    if len(parts) == 1:
        p = parts[0]
    else:
        p = os.path.join(*parts)
    if os.path.isabs(p):
        return os.path.abspath(p)
    return os.path.abspath(os.path.join(base_dir(), p))


# ---------------------------------------------------------------- 具体文件位置
#
# 固定位置的用函数给出；可配置位置的（state.path / history.path）由调用方
# 读出配置后再交给 resolve()。

def config_path() -> str:
    """主配置（出厂默认 + 中文注释，**程序永不写入**）。"""
    return resolve("config.yaml")


def secrets_path() -> str:
    """本地密钥文件（API Key）。不进 Git。"""
    return resolve("secrets.yaml")


def settings_path() -> str:
    """用户在设置界面里改的值。

    放在 `state/` 下有两个好处：
      - `state/` 已经在 .gitignore 里，**不用再改忽略规则**
      - 它天然就是"本机的东西"，不需要跟着代码走

    它是**覆盖层**：这里的值优先于 `config.yaml`。
    删掉某一项就等于"恢复默认"。详见 `settings.py`。
    """
    return resolve("state", "settings.json")


def state_dir() -> str:
    return resolve("state")


def log_dir() -> str:
    return resolve("logs")


def log_path() -> str:
    """日志文件。打包成无控制台的 exe 之后，这是唯一能看到运行情况的地方。"""
    return resolve("logs", "app.log")


def history_path() -> str:
    """历史库的**默认**位置（配置里能覆盖）。"""
    return resolve("state", "history.db")


def asset_path(name: str) -> str:
    """取**随程序分发的只读资源**（`wechat_assistant/assets/` 下的某个文件）。

    ⚠️ 一律走 `pkg_dir()`（冻结态是 PyInstaller 的临时解压目录），
        **不要**用 `base_dir()` —— 那个是给用户编辑的配置用的，打包后找不到。
    ⚠️ 加了新资源记得同步 `wechat_ai.spec`：那里整目录收了
        `wechat_assistant/assets`，所以放进这个目录就够了。
    """
    return os.path.join(pkg_dir(), "assets", name)


def icon_path() -> str:
    """程序图标（任务栏 / 标题栏 / Alt-Tab 都用它）。

    ⚠️ 用 `pkg_dir()` 而不是 `base_dir()`：它是**随程序分发的只读资源**，
        打包后跟着进包、放在临时解压目录里。`base_dir()` 是给**用户编辑**的
        配置用的，把图标放那儿在打包后会找不到。

    ⚠️ 返回 `.ico` 而不是 `.png`：Windows 的任务栏/标题栏/Alt-Tab 会按
        **不同尺寸**取图标，`.ico` 里能一次带齐 16~256 七档
        （文件由 `probes/make_icon.py` 生成）。生成脚本里已经自检过档数。
    """
    ico = os.path.join(pkg_dir(), "assets", "icon.ico")
    if os.path.exists(ico):
        return ico
    # 兜底：只有 PNG 也认（Qt 能读，只是小尺寸会由 Qt 自己缩，质量差一点）
    return os.path.join(pkg_dir(), "assets", "icon.png")


# ---------------------------------------------------------------- 目录创建

def ensure_dir(path: str) -> str:
    """确保目录存在，返回它。"""
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def ensure_parent(path: str) -> str:
    """确保某个文件的**父目录**存在（写文件前调用）。"""
    if path:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    return path
