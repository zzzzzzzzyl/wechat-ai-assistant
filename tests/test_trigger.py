# -*- coding: utf-8 -*-
"""触发器的离线测试（不碰微信、不碰热键、不开界面）。

为什么单独一个文件：`TriggerManager` 是唯一**把配置值冻结进属性**、
又要立刻响应设置改动的地方（`min_interval`）。2026-10-17 的审计发现它漏了
`use_config()` —— 「高级 → 两次触发最小间隔」改了完全没反应，要重启才认。

用法：python tests\\test_trigger.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.trigger import TriggerManager  # noqa: E402

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


class FakeCfg:
    """最小的配置替身：只支持点号取值（和 `Config.get` 同语义）。"""

    def __init__(self, **kw):
        self._d = kw

    def get(self, dotted, default=None):
        if dotted in self._d:
            return self._d[dotted]
        cur = self._d
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return default if cur is None else cur


def test_min_interval_frozen():
    """1. `min_interval` 是构造时冻结的，`fire()` 靠它防抖。"""
    print("\n1. 构造时读进属性的 min_interval")
    cfg = FakeCfg(**{"trigger.min_interval": 3.0})
    t = TriggerManager(cfg, reader=None, logger=lambda m: None)
    check("构造时读到 3.0", t.min_interval == 3.0, repr(t.min_interval))

    fired = []
    t._on_trigger = lambda reason: fired.append(reason)
    check("第一次触发成功", t.fire("manual") is True)
    check("  ⚠️ 紧接着第二次被防抖忽略（同一个属性的作用）",
          t.fire("manual") is False)
    check("  回调只跑了一次", fired == ["manual"], repr(fired))


def test_use_config_refreshes():
    """2. `use_config()` 之后**同一个实例**立刻用新值（不重建对象）。"""
    print("\n2. use_config 立刻刷新（不重建实例）")
    cfg = FakeCfg(**{"trigger.min_interval": 3.0})
    t = TriggerManager(cfg, reader=None, logger=lambda m: None)
    fired = []
    t._on_trigger = lambda reason: fired.append(reason)
    t.fire("a")
    check("旧值 3.0 时第二次被忽略", t.fire("b") is False)

    new = FakeCfg(**{"trigger.min_interval": 0.0})
    t.use_config(new)
    check("use_config 换上了新配置对象", t.cfg is new)
    check("  ⚠️ min_interval 也刷新了（这一条漏了就是「改了没反应」）",
          t.min_interval == 0.0, repr(t.min_interval))
    check("  新值 0.0 时**同一个实例**立刻能再触发", t.fire("c") is True,
          repr(fired))
    check("  回调收到了新那次", fired[-1] == "c", repr(fired))


def test_use_config_is_safe():
    """3. 配置坏了 / 值不合法时不能把整条触发链弄崩。"""
    print("\n3. use_config 的容错")
    cfg = FakeCfg(**{"trigger.min_interval": 5.0})
    t = TriggerManager(cfg, reader=None, logger=lambda m: None)

    class BadCfg:
        def get(self, dotted, default=None):
            if dotted == "trigger.min_interval":
                return "不是数字"
            return default

    t.use_config(BadCfg())
    check("值不合法时沿用旧值（不抛异常）", t.min_interval == 5.0,
          repr(t.min_interval))

    class BoomCfg:
        def get(self, dotted, default=None):
            raise RuntimeError("爆炸")

    t.use_config(BoomCfg())
    check("取配置抛异常也只降级，不往上炸", t.min_interval == 5.0,
          repr(t.min_interval))


def main():
    print("=" * 62)
    print("触发器测试（配置刷新 / 防抖）")
    print("=" * 62)
    test_min_interval_frozen()
    test_use_config_refreshes()
    test_use_config_is_safe()
    print("\n" + "=" * 62)
    print("通过 %d，失败 %d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
