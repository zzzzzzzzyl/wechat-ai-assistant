# -*- coding: utf-8 -*-
"""审计：哪些「不用重启」的设置项，是**从某个对象缓存下来的 cfg** 里读的。

为什么需要这个工具（2026-10-17）：本项目栽过 6 次同一个坑 ——
对象在构造时把 cfg 存成 `self.cfg`，设置保存时只做 `cfg = new_cfg`（换变量），
于是那个对象手里还是旧对象 →「设置里显示已保存、实际没生效」，**而且不报错**。

现在的修法是双保险：
    ① `on_config_changed()` 先 `cfg.update_from(new)` —— **原地**换内容，
       所有持有者自动看到新值。**只要这一步在，"从 self.cfg 读"就不再是问题**；
    ② 构造时把**派生值**算好存起来的（`background_read_ok`、`vision_model`…）
       仍要重建 / `use_config()` —— 这一条工具查不出来，得看代码。

所以输出分两段：
    · 第 0 段：`on_config_changed` 到底调没调 `update_from`（不调就有大问题）；
    · 第 1 段：读的是**缓存的 cfg**、所属类**没有 use_config** 的项 ——
      第 0 段成立时它们是安全的，但值得人看一眼（尤其是有派生缓存的类）。

只读。用法：python tools\\audit_cfg_cache.py
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wechat_assistant.settings import SPEC  # noqa: E402


def scan_cfg_reads():
    """返回 [(相对路径, 行号, 宿主类, 变量名, 键)]。"""
    hits = []
    files = []
    for base, _dirs, names in os.walk(os.path.join(ROOT, "wechat_assistant")):
        for n in names:
            if n.endswith(".py"):
                files.append(os.path.join(base, n))
    files.append(os.path.join(ROOT, "run_watch.py"))

    pat = re.compile(r'([\w\.]+)\.cfg\.get\(\s*["\']([\w\.]+)["\']')
    for path in files:
        rel = os.path.relpath(path, ROOT)
        cur = "<模块>"
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            m = re.match(r"^class\s+(\w+)", line)
            if m:
                cur = m.group(1)
            if line.lstrip().startswith("#"):
                continue
            for m in pat.finditer(line):
                hits.append((rel, i, cur, m.group(1), m.group(2)))
    return hits


def classes_with_use_config():
    """哪些类定义了 `use_config()`（说明它有"换配置"的入口）。"""
    out = set()
    for base, _dirs, names in os.walk(os.path.join(ROOT, "wechat_assistant")):
        for n in names:
            if not n.endswith(".py"):
                continue
            cur = None
            for line in open(os.path.join(base, n), encoding="utf-8"):
                m = re.match(r"^class\s+(\w+)", line)
                if m:
                    cur = m.group(1)
                elif re.match(r"^\s+def use_config\(", line) and cur:
                    out.add(cur)
    return out


def on_config_changed_calls():
    import ast

    src = open(os.path.join(ROOT, "run_watch.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "on_config_changed":
            names = {n.func.attr for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            return names
    return set()


def main():
    calls = on_config_changed_calls()
    live = [s for s in SPEC if not s.restart]

    print("=== 第 0 段：on_config_changed 的保险在不在 ===")
    ok_update = "update_from" in calls
    print("   %s cfg.update_from(new_cfg)   治本：持有者自动看到新值"
          % ("[OK]" if ok_update else "[缺] 下面所有项都可能不生效"))
    print("   %s 其它换配置动作：%s"
          % ("[OK]" if len(calls) > 1 else "[!]",
             ", ".join(sorted(calls - {"update_from"})) or "（无）"))
    if not ok_update:
        print("\n   没有 update_from 时，任何 self.cfg.get(...) 读的都是"
              "**启动那一刻**的配置 —— 先补这一步，再看下面。")

    print("\n=== 第 1 段：读「缓存下来的 cfg」的项（%d 个不用重启的项里筛）==="
          % len(live))
    have_use_config = classes_with_use_config()
    reads = scan_cfg_reads()
    live_keys = {s.key for s in live}

    rows = {}
    for rel, ln, cls, var, key in reads:
        if key not in live_keys:
            continue
        rows.setdefault(key, []).append((rel, ln, cls, var))

    if not rows:
        print("  （一项都没有 —— 全是每次用时读最新 cfg，或没被任何类缓存）")
    for key in sorted(rows):
        sites = rows[key]
        classes = sorted({c for _r, _l, c, _v in sites})
        guarded = [c for c in classes if c in have_use_config]
        tag = "有 use_config" if guarded else "[!] 无 use_config"
        print("  · %-28s %-18s %s" % (key, ",".join(classes), tag))
        for rel, ln, cls, var in sites:
            print("      %s:%d  %s.cfg.get('%s')" % (rel, ln, var, key))

    print("\n说明（怎么读这张表）：")
    print("  · 第 0 段 [OK] 时，从 self.cfg 读是**安全**的 —— 内容被原地换过。")
    print("  · 只有「构造时把派生值算好存起来」的类才需要额外重建 / use_config()：")
    print("      AIClient（model / vision_model / max_chars…）")
    print("      OcrReader（background_read_ok）")
    print("    这两处已在 on_config_changed 里处理（AST 守门：§7h / §7i）。")
    print("  · 给某个类**新增**了构造时算好的字段，记得同步加 use_config()。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
