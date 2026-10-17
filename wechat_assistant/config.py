# -*- coding: utf-8 -*-
"""配置加载。

配置分三层（详见 `settings.py`）：
    config.yaml          出厂默认 + 中文注释   【程序永不写入】
          ↓ 被覆盖
    state/settings.json  设置界面里改的值
          ↓ 被覆盖
    secrets.yaml         API Key

本模块负责把三层合并成一个 `Config` 对象。
微信的控件类名也放在配置里，微信升级后只改配置、不改代码。
"""

import os
from typing import Any, Optional

import copy

import yaml

from . import paths

# 基准目录（打包后 = exe 所在目录，源码运行 = 项目根）。
# ⚠️ 以前这里是 `os.path.dirname(os.path.dirname(__file__))`，**打包后会指向
#    PyInstaller 的临时解压目录**，导致读不到 exe 旁边的配置、state 写进临时目录
#    （关掉程序就没了）。现在统一走 paths，理由见 paths.py 的模块说明。
ROOT = paths.base_dir()
DEFAULT_CONFIG_PATH = os.path.join(ROOT, "config.yaml")


class ConfigError(RuntimeError):
    """配置读取失败。"""


class Config:
    """支持点号路径访问的配置对象。"""

    def __init__(self, data: dict, path: str):
        self._data = data or {}
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        """按点号路径取值，例如 cfg.get("wechat.node.chat_title")。"""
        cur: Any = self._data
        for key in dotted.split("."):
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
        return default if cur is None else cur

    def section(self, dotted: str) -> dict:
        v = self.get(dotted, {})
        return v if isinstance(v, dict) else {}

    def update_from(self, other) -> None:
        """把另一份配置的内容**原地**换进本对象（**对象本身不换**）。

        ⚠️⚠️ 为什么非要"原地"（2026-10-17 的血案）：本进程里**一堆对象在构造时
        就把 cfg 存成了 `self.cfg`**（读取器 / 跟随器 / 记录器 / 触发器 / 界面…）。
        设置保存时如果只是 `cfg = new_cfg` 换个变量，那些对象手里**还是旧对象**
        —— 于是"设置里显示已保存、实际完全没生效"，**而且不报错**。
        这个坑在本项目至少栽过 6 次：`AIClient`（模型/上下文长度）、跟随器
        （`ui.attach`）、不透明度、置顶、知识库开关、图片开关（2026-10-17）。

        现在两类都要做，缺一不可：
            ① `cfg.update_from(new)` —— 让**所有持有者**自动看到新值（治本）；
            ② 派生值缓存过的对象仍要重建 / `use_config()`（`AIClient`、
               `reader` 的 `background_read_ok` 这类"构造时算好存起来"的）。

        ⚠️ 用 `deepcopy` 接过来：否则两份配置会共享嵌套 dict，改一边脏另一边。
        """
        data = getattr(other, "_data", other) or {}
        self._data = copy.deepcopy(data)

    def __repr__(self) -> str:
        return "<Config path=%s>" % self.path


def _deep_merge(base: dict, overlay: dict) -> dict:
    """把 overlay 递归合并进 base（后者覆盖前者）。"""
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: Optional[str] = None) -> Config:
    """加载配置。

    - 不传 `path`（正常使用）：走三层合并，用户设置优先。
    - 传了 `path`（探测脚本/测试用自定义配置）：退回**两层**行为
      （该文件 + 它旁边的 secrets.yaml），不动全局的用户设置 ——
      否则脚本改了全局 settings.json 会反过来影响主程序，很难查。
    """
    if path is None:
        # 延迟导入：settings 依赖本模块的 Config / _deep_merge，
        # 放在模块顶部会形成循环导入。
        from .settings import SettingsStore

        return SettingsStore(logger=lambda m: None).load()

    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise ConfigError("配置文件不存在: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        raise ConfigError("读取配置失败 %s: %s" % (path, e)) from e
    if not isinstance(data, dict):
        raise ConfigError("配置文件根节点必须是映射（键值对）")

    # 合并本地密钥文件：secrets.yaml 不进 Git，用来放 api_key 这类敏感值
    secrets_path = os.path.join(os.path.dirname(path), "secrets.yaml")
    if os.path.exists(secrets_path):
        try:
            with open(secrets_path, "r", encoding="utf-8") as f:
                sec = yaml.safe_load(f) or {}
            if isinstance(sec, dict):
                _deep_merge(data, sec)
        except Exception as e:
            raise ConfigError("读取 secrets.yaml 失败: %s" % e) from e

    return Config(data, path)


def settings_store(logger=None):
    """拿到操作三层配置的入口（设置界面用它读写）。"""
    from .settings import SettingsStore

    return SettingsStore(logger=logger)


def resolve_state_path(cfg: Config) -> str:
    """把 state.path 解析成绝对路径（相对路径以基准目录为基准）。"""
    return paths.resolve(cfg.get("state.path", "state/state.json"))


def resolve_history_path(cfg: Config) -> str:
    """把 history.path 解析成绝对路径。"""
    return paths.resolve(cfg.get("history.path", "state/history.db"))
