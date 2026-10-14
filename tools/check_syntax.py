# -*- coding: utf-8 -*-
"""编译体检 —— 所有 .py 都必须能通过 `ast.parse`。

    python tools/check_syntax.py            # 整个项目
    python tools/check_syntax.py --staged   # 只查 git 暂存区里的 .py（给 pre-commit 用）

⚠️ 为什么需要它（2026-10-11）

    我在这个项目里**六次**写出过语法错误，全都是同一类：
    在双引号字符串里又套了双引号（中文引号习惯），例如

        print("  就说过"比 UIA 还省" —— 那是错的。")

    这类错误的问题不是难查，而是**藏得住**：

      · 语法错误要到那个文件**被 import** 时才炸，而 `probes/` 下的脚本
        永远没人 import（单测也不会去碰它们）—— 于是它能安安静静提交上去，
        等你哪天手动跑那个探针才发现。
      · 我自己"写完顺手看一眼"的习惯靠不住：已经证明六次了。

    所以把它变成**提交的硬门槛**（`.git/hooks/pre-commit` 调这个脚本）。
    代价约 1 秒，只查语法、不跑测试（钩子要快，否则会被 --no-verify 绕过）。

⚠️ 钩子是**每个克隆各自的**（`.git/hooks/` 不进版本库）。
   换机器后要重新装：`python tools/check_syntax.py --install-hook`
"""

import argparse
import ast
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: 这些目录里的 .py 不用查（打包产物、缓存、第三方）
SKIP_PARTS = ("build", "dist", "_internal", ".packtools", ".tmp", ".pipcache",
              "__pycache__", ".git", "node_modules")

HOOK_BODY = """#!/bin/sh
# 提交前守门：所有 .py 都要能编译。装载者见 tools/check_syntax.py 的说明。
# 纯 ASCII —— Windows 上 git 用 sh.exe 跑它，中文会有编码问题。
exec python "$(git rev-parse --show-toplevel)/tools/check_syntax.py" --staged
"""


def iter_py(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_PARTS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def staged_files(root):
    out = subprocess.run(["git", "diff", "--cached", "--name-only",
                          "--diff-filter=ACM"],
                         cwd=root, capture_output=True, text=True).stdout
    return [os.path.join(root, line.strip()) for line in out.splitlines()
            if line.strip().endswith(".py")]


def check(files, root):
    bad = []
    for p in files:
        rel = os.path.relpath(p, root)
        try:
            src = open(p, encoding="utf-8").read()
        except Exception as e:
            bad.append((rel, 0, "读不出来: %s" % e))
            continue
        try:
            ast.parse(src)
        except SyntaxError as e:
            bad.append((rel, e.lineno or 0, e.msg or ""))
    return bad


def install_hook(root):
    hooks = os.path.join(root, ".git", "hooks")
    if not os.path.isdir(hooks):
        print("找不到 .git/hooks —— 这里不是 git 仓库？")
        return 1
    dst = os.path.join(hooks, "pre-commit")
    if os.path.exists(dst):
        print("⚠️ %s 已存在，没有覆盖。想装就先自己备份/删掉它。" % dst)
        return 1
    with open(dst, "w", encoding="ascii", newline="\n") as f:
        f.write(HOOK_BODY)
    try:
        os.chmod(dst, 0o755)
    except Exception:
        pass
    print("已装 pre-commit 钩子: %s" % dst)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true",
                    help="只查 git 暂存区里的 .py（pre-commit 用）")
    ap.add_argument("--install-hook", action="store_true",
                    help="把 pre-commit 钩子装到这个仓库")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if args.install_hook:
        return install_hook(root)

    files = staged_files(root) if args.staged else list(iter_py(root))
    bad = check(files, root)

    if bad:
        print("✗ 有 %d 个 .py 编译不过 —— **提交被拦下**：\n" % len(bad))
        for rel, line, msg in bad:
            print("   %s:%s  %s" % (rel, line, msg))
        print("\n⚠️ 最常见的原因：**双引号字符串里又套了双引号**。")
        print("   中文里想引用就写「」或者单引号，别在 \"...\" 里再写 \"...\"。")
        return 1

    print("✓ %d 个 .py 全部编译通过" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
