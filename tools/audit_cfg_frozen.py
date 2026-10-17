# -*- coding: utf-8 -*-
"""审计：设置项「改了到底会不会生效」的两条漏法。

本项目栽过 7 次的坑，本质是**两条**：
  ① 调用时读的是**旧 cfg 对象**（`self.cfg.get(...)`）—— 现在 `Config.update_from()`
     会原地换内容，这一条已治本；
  ② **构造时把值读进属性里冻结**（`self.xxx = cfg.get(...)`）—— 这一条 `update_from`
     **治不了**，只能重建那个对象，或者在 SPEC 里标 `restart=True`。

第 ② 条最容易漏：属性名和键名长得完全不一样（`background_read_ok` ← `ocr_read.record`），
靠人眼翻代码基本翻不出来。所以本工具：

  1. 解析 `SPEC`，拿到每个键**有没有标 `restart=True`**；
  2. AST 扫全仓，找出「`self.xxx = ... cfg.get("k") ...`」这种**冻结**；
  3. 再解析 `on_config_changed()`（以及面板自己的 `_on_settings_saved()`），
     看**哪些类被重建/换过配置**；
  4. 交叉出：**键没标 restart + 被冻进属性 + 所属类没人管** = ⚠️ 真漏网。

只读。用法：python tools\\audit_cfg_frozen.py [--verbose]
"""
import ast
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wechat_assistant.settings import SPEC  # noqa: E402

SPEC_KEYS = {s.key: s for s in SPEC}


def _py_files():
    for base, _dirs, names in os.walk(ROOT):
        rel_base = os.path.relpath(base, ROOT)
        if rel_base.split(os.sep)[0] in (".git", "dist", "build", ".tmp", "out", "state"):
            continue
        for n in names:
            if n.endswith(".py"):
                yield os.path.join(base, n)


def _cfg_gets(node):
    """node 底下所有 `xxx.cfg.get("键")` / `cfg.get("键")` 里的键。"""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr == "get" and sub.args:
            recv = sub.func.value
            recv_name = ""
            if isinstance(recv, ast.Name):
                recv_name = recv.id
            elif isinstance(recv, ast.Attribute):
                recv_name = recv.attr
            if "cfg" in recv_name:
                a = sub.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    out.append(a.value)
    return out


def scan_frozen():
    """返回 [(文件, 类名, 属性名, 行号, [键])] —— 构造期冻结的值。"""
    found = []
    for path in _py_files():
        rel = os.path.relpath(path, ROOT)
        if rel.startswith("tests") or rel.startswith("probes"):
            continue
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
                if fn.name not in ("__init__", "__post_init__"):
                    continue
                for stmt in ast.walk(fn):
                    if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                        continue
                    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                    val = stmt.value
                    if val is None:
                        continue
                    keys = _cfg_gets(val)
                    if not keys:
                        continue
                    for t in targets:
                        if isinstance(t, ast.Attribute):
                            found.append((rel, cls.name, t.attr, stmt.lineno, keys))
    return found


def refreshed_classes():
    """`on_config_changed` 里被重建 / 换了配置的类名（近似：看调用与构造）。"""
    out = set()
    # ① 直接 new 出来的
    try:
        src = open(os.path.join(ROOT, "run_watch.py"), encoding="utf-8").read()
        tree = ast.parse(src)
    except OSError:
        return out
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "on_config_changed":
            fn = node
            break
    if fn is None:
        return out
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            # AIClient(cfg, ...) -> AIClient
            if isinstance(f, ast.Name) and f.id[:1].isupper():
                out.add(f.id)
            # reader.use_config(...) -> 那个变量后面用到的类，靠名字粗判
            if isinstance(f, ast.Attribute) and f.attr == "use_config":
                base = getattr(f.value, "id", None) or getattr(f.value, "attr", "")
                out.add("@" + base)
    # ② use_config 的接收者 -> 具体类（写死映射，改动时会显眼）
    alias = {
        "@reader": {"OcrReader", "WeChatReader", "AutoReader"},
        "@follower": {"WindowFollower", "Follower"},
        "@trigger": {"TriggerManager"},
    }
    resolved = set()
    for name in out:
        resolved |= alias.get(name, {name})
    return resolved


def main():
    verbose = "--verbose" in sys.argv
    frozen = scan_frozen()
    refreshed = refreshed_classes()

    live = {k for k, s in SPEC_KEYS.items() if not s.restart}
    print("设置项 %d 个（不用重启 %d）；构造期被冻结的属性 %d 处"
          % (len(SPEC), len(live), len(frozen)))
    print("on_config_changed 会重建/换配置的类：%s\n"
          % ", ".join(sorted(refreshed)) or "（无）")

    gaps, ok_frozen, restart_ok, unknown = [], [], [], []
    for rel, cls, attr, ln, keys in frozen:
        hit_live = [k for k in keys if k in live]
        hit_restart = [k for k in keys if k in SPEC_KEYS and SPEC_KEYS[k].restart]
        not_in_spec = [k for k in keys if k not in SPEC_KEYS]
        row = (rel, cls, attr, ln, keys)
        if hit_live:
            if cls in refreshed:
                ok_frozen.append((row, hit_live))
            else:
                gaps.append((row, hit_live))
        elif hit_restart:
            restart_ok.append((row, hit_restart))
        else:
            unknown.append((row, not_in_spec))

    def dump(title, items, keys_note):
        print("=== %s（%d 处）===" % (title, len(items)))
        for (rel, cls, attr, ln, keys), hit in items:
            print("  · %s:%d  %s.%s  ← %s" % (rel, ln, cls, attr, ",".join(hit)))
        if not items:
            print("  （无）")
        print()

    dump("⚠️ 真漏网：键没标 restart，值被冻结，所属类也没人重建", gaps, "")
    dump("✅ 冻结但类会被重建/换配置（安全）", ok_frozen, "")
    if verbose:
        dump("· 冻结的是 restart=True 的键（安全：本来就要求重启）", restart_ok, "")
        dump("· 冻结的键不在 SPEC 里（界面改不到，靠 config.yaml）", unknown, "")

    if gaps:
        print("处理办法（按优先级）：")
        print("  a) 在 `on_config_changed` 里重建该对象 / 给它加 `use_config()`；")
        print("  b) 或把这个键标成 `restart=True`（界面会显示「需重启」）。")
        print("  ⚠️ 别只改一处：`settings.py` 里标 restart 的项，界面上就不给"
              "「立刻生效」的暗示了。")
        return 1
    print("✅ 没有发现第②类漏网。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
