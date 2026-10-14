# -*- coding: utf-8 -*-
"""找**未定义的名字**（`NameError` 的静态版）。

    python -X utf8 probes/audit_undefined_names.py

⚠️ 为什么需要它（2026-09-27 用户实测踩到）：
    点悬浮窗的【只看对话】报
        NameError: name 'current' is not defined
    那一行是**旧「鼠标锚点」时代的遗留**：改成"回复目标 = 当前屏幕的全部对话"
    之后 `current` 这个变量就不存在了，但漏改了一行。
    它藏在 `if not (auto_generate and want_generate):` 这样一条**很少走到的分支**里，
    于是：
      · `python -m py_compile` **抓不到**（编译期不做名字解析）
      · 所有单测也抓不到（那些用例根本没走这条分支）
      · 本机**没有装** pyflakes / flake8 / pylint / ruff
    → 只有用户点下去才会炸。所以这里自己做一个够用的静态检查。

⚠️ 它是**近似**的，宁可漏报也不要误报（误报会让人开始忽略它）：
    · 名字解析只看"本函数 / 外层函数 / 模块 / 内建"四层（够覆盖真实代码）
    · `global` / `nonlocal` 一律当成"已定义"
    · `from x import *` 一律当成"后面所有的名字都可能来自它"（整个文件跳过）
    · 装饰器 / 默认参数 / 注解里的名字，同时算进内外两层作用域
"""

import ast
import builtins
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

BUILTINS = set(dir(builtins)) | {
    # 模块级隐式全局
    "__file__", "__name__", "__doc__", "__builtins__", "__spec__",
    "__package__", "__loader__", "__debug__",
}

SKIP_DIRS = {".git", "__pycache__", "out", "state", "logs", ".venv", "venv",
             # ⚠️ 打包产物与打包工具：里面全是**第三方**代码（cv2 / PySide6 /
             #    onnxruntime / PyInstaller 自己的 bootstrap），扫它们只会刷屏。
             #    这个脚本的用途是查**本仓库自己写的**代码。
             "build", "dist", ".packtools", ".tmp", ".pipcache"}


class Scope:
    def __init__(self, kind, node, parent):
        self.kind = kind              # module / function / class
        self.node = node
        self.parent = parent
        self.assigned = set()
        self.params = set()
        self.relaxed = False          # 有 global/nonlocal / import * -> 不查这个作用域

    def chain(self):
        s = self
        while s is not None:
            yield s
            s = s.parent


def build(node, scope, out):
    """收集作用域与"赋过值的名字"。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            s = Scope("function", child, scope)
            out.append(s)
            args = child.args
            for a in (list(args.posonlyargs) + list(args.args)
                      + list(args.kwonlyargs)):
                s.params.add(a.arg)
            if args.vararg:
                s.params.add(args.vararg.arg)
            if args.kwarg:
                s.params.add(args.kwarg.arg)
            # 装饰器 / 默认值 / 注解在**外层**求值 —— 两边的名字都算上，避免误报
            for extra in (getattr(child, "decorator_list", []),
                          list(args.defaults) + [d for d in args.kw_defaults if d],
                          [getattr(child, "returns", None)]):
                for e in (extra if isinstance(extra, list) else [extra]):
                    if e is None:
                        continue
                    for n in ast.walk(e):
                        if isinstance(n, ast.Name):
                            s.assigned.add(n.id)
                            scope.assigned.add(n.id)
            build(child, s, out)
        elif isinstance(child, ast.ClassDef):
            s = Scope("class", child, scope)
            out.append(s)
            build(child, s, out)
        else:
            build(child, scope, out)


def collect_names(node, scope, skip_nested=True):
    """把 `node` 里（不跨进嵌套函数/类）的赋值名与声明名收进 scope。"""
    for child in ast.iter_child_nodes(node):
        if skip_nested and isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                        ast.Lambda)):
            # ⚠️ **跳过函数体，但必须把这个名字本身记下来！**
            #    否则 `def section(...)` 后面的 `section(...)` 调用会被报成
            #    "未定义" —— 修完作用域问题之后剩下的 3232 条误报**全是这一类**。
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                scope.assigned.add(child.name)
            continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            scope.assigned.add(child.id)
        if isinstance(child, ast.Import):
            for a in child.names:
                scope.assigned.add((a.asname or a.name).split(".")[0])
        if isinstance(child, ast.ImportFrom):
            if child.module == "*" or any(a.name == "*" for a in child.names):
                scope.relaxed = True
            for a in child.names:
                if a.name != "*":
                    scope.assigned.add(a.asname or a.name)
        if isinstance(child, ast.Global):
            scope.relaxed = True
            for n in child.names:
                scope.assigned.add(n)
        if isinstance(child, ast.Nonlocal):
            scope.relaxed = True
            for n in child.names:
                scope.assigned.add(n)
        if isinstance(child, ast.ExceptHandler) and child.name:
            # ⚠️ `except X as e` 里的 `e` 是**字符串**字段（`ExceptHandler.name`），
            #    不是 `Name` 节点 —— 通用那套"找 Name(Store)"收不到它。
            #    剩的 156 条误报**全是这一类**（`e` / `ex`）。
            scope.assigned.add(child.name)
        if isinstance(child, (ast.arg,)) and child.arg:
            scope.assigned.add(child.arg)
        collect_names(child, scope, skip_nested)


def _outer_parts(fn):
    """函数/类定义里**在外层作用域求值**的部分。

    ⚠️ 装饰器、默认参数、注解（含返回注解）都是在**定义处**求值的，
        不属于函数体 —— 所以它们要算进外层作用域。
    """
    out = []
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        out.extend(getattr(fn, "decorator_list", []))
        a = fn.args
        out.extend(a.defaults)
        out.extend([d for d in a.kw_defaults if d is not None])
        for x in (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
                  + [a.vararg, a.kwarg]):
            if x is not None and x.annotation is not None:
                out.append(x.annotation)
        if getattr(fn, "returns", None) is not None:
            out.append(fn.returns)
    elif isinstance(fn, ast.ClassDef):
        out.extend(fn.decorator_list)
        out.extend(fn.bases)
        out.extend([k.value for k in fn.keywords])
    return [e for e in out if e is not None]


def own_nodes(scope_node):
    """产出**属于这个作用域自己**的节点 —— **不跨进嵌套函数/类/lambda 的函数体**。

    ⚠️⚠️ 这里是这个检查器最容易写错的地方，我前后栽了**三次**：
      ① 第一版直接 `ast.walk(scope.node)` —— 它会**钻进嵌套函数的函数体**，
         于是函数内部用到的名字被算成"外层在用它"，而外层本来就没有 ——
         一次刷出 **21548 条误报**。
      ② 第二版自己写了递归，但**忘了在"传进来的节点本身就是函数定义"时短路**：
         `rec(section_def)` 会 yield 这个定义、然后接着递归进它的**函数体**，
         于是参数名 `title` 被报成"模块作用域里未定义" —— 还是 **20980 条**。
         → 在**语句层**加了判断（见下面那个 `for stmt in ...` 循环）修好了。
      ③ 2026-09-27：**同一个坑还剩一个入口没堵** —— `Lambda` 分支是直接
         `rec(scope_node.body)` 进来的，而 body 本身就可能**又是一个 lambda**
         （`lambda orig: lambda k, d=None: ...`）。那条路没经过语句层的判断，
         于是内层 lambda 的 `k` / `d` 被算成外层 lambda 在用 —— 2 条误报。
         → 现在把判断挪进 `rec()` **本身**：传进来的节点只要是个函数/类/lambda，
         就只取它在外层求值的部分，**无论如何都不进它的函数体**。
    误报比漏报更糟：它会让人开始无视这个工具。
    → 现在：**每个作用域只走自己的语句**；遇到嵌套作用域只取 `_outer_parts()`。
    """
    def rec(node):
        # ⚠️ 这一句是①③两次栽的地方：**传进来的节点本身**是嵌套作用域时也要短路，
        #    不能只在"遍历到的子节点"那层判断。
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.Lambda, ast.ClassDef)):
            for e in _outer_parts(node):
                yield from rec(e)
            return
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda, ast.ClassDef)):
                for e in _outer_parts(child):
                    yield from rec(e)
                continue
            yield from rec(child)

    if isinstance(scope_node, ast.Lambda):
        # lambda 的 body 是**表达式**，而且属于它自己的作用域
        yield from rec(scope_node.body)
        return
    for stmt in scope_node.body:
        # ⚠️ 顶层语句自己就是函数/类定义时，**只取外层求值的部分**（见上）
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.Lambda, ast.ClassDef)):
            for e in _outer_parts(stmt):
                yield from rec(e)
            continue
        yield from rec(stmt)


def check_file(path):
    try:
        src = open(path, encoding="utf-8").read()
    except Exception:
        return []
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        return [(path, getattr(e, "lineno", 0) or 0,
                 "语法错误: %s" % e.msg, "")]
    scopes = []
    mod = Scope("module", tree, None)
    scopes.append(mod)
    build(tree, mod, scopes)
    for s in scopes:
        collect_names(s.node, s)

    # 模块里只要有 import *，整个文件就跳过（没法静态判断）
    if any(s.relaxed for s in scopes if s.kind == "module"):
        return []

    known_module = set(mod.assigned)
    bad = []
    for s in scopes:
        if s.relaxed:
            continue
        local = s.params | s.assigned
        for node in own_nodes(s.node):
            if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
                continue
            name = node.id
            if name in local:
                continue
            # 往外层函数找（闭包）
            ok = False
            p = s.parent
            while p is not None:
                if name in (p.params | p.assigned):
                    ok = True
                    break
                p = p.parent
            if ok or name in known_module or name in BUILTINS:
                continue
            bad.append((path, node.lineno, name, s.kind))
    # 同一个名字同一行只报一次
    seen, out = set(), []
    for item in bad:
        k = (item[0], item[1], item[2])
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out


def main():
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                files.append(os.path.join(dirpath, fn))
    print("扫描 %d 个 .py 文件（近似静态检查：宁可漏报，不误报）\n" % len(files))

    problems = []
    for f in sorted(files):
        problems.extend(check_file(f))

    # 自检：故意造一个未定义的名字，确认**检查器本身是工作的**。
    # ⚠️ 形状要**照着真实那个 bug 来**：参数有、外层函数也有，
    #    未定义的名字藏在一条**很少走到的分支**里 —— 而不只是一个裸的未定义名。
    #    这样自检才有意义：它证明的是"这类藏在分支里的名字会被抓到"。
    probe_src = (
        "def helper(x):\n"
        "    return x\n"
        "\n"
        "\n"
        "def do_generate(sess, auto_generate=True, want_generate=False):\n"
        "    preview = helper(sess)\n"
        "    if not (auto_generate and want_generate):\n"
        "        preview.append(current.who)\n"
        "        return preview\n"
        "    return preview\n"
    )
    # ⚠️ 自检**还要证明"不误报"**，而这一半是 2026-09-27 才补上的。
    #    光验"能抓到未定义名"只卡住了松的方向；这个检查器真正栽过三次的
    #    全是**误报**（21548 → 20980 → 2 条），每次都是"递归钻进了嵌套作用域"。
    #    所以这里专门放一段**合法**的代码，要求它是 **0 条**：
    #      · 嵌套函数 / 闭包 / 注解里的外层名字（②的形态）
    #      · **层层嵌套的 lambda**（③的形态：`lambda a: lambda b: a + b`）
    #    以后谁再把 `rec()` 的短路条件改掉，这里立刻会红。
    clean_src = (
        "import os\n"
        "\n"
        "\n"
        "def outer(sess):\n"
        "    def inner(x, y=os.sep):\n"
        "        return x + y\n"
        "\n"
        "    f = (lambda orig: lambda k, d=None: (\n"
        "        'a' if k else orig(k, d)))(inner)\n"
        "    return f(sess)\n"
        "\n"
        "\n"
        "class K:\n"
        "    def m(self, v: int = 1) -> int:\n"
        "        return v\n"
    )
    # ⚠️ 自检要落两个临时 .py，**不写进 out/** ——
    #    DSH 的文件沙箱下，被拉起的进程**写不进工作区里的子目录**
    #    （工作区根能写、子目录拒写），而这个脚本是"随手跑一下"的诊断工具，
    #    不该因为一个临时文件就整个跑不起来。落系统临时目录，谁都能跑。
    tmpdir = tempfile.mkdtemp(prefix="undef_audit_")
    clean = os.path.join(tmpdir, "_undef_selftest_clean.py")
    open(clean, "w", encoding="utf-8").write(clean_src)
    clean_hits = check_file(clean)

    tmp = os.path.join(tmpdir, "_undef_selftest.py")
    open(tmp, "w", encoding="utf-8").write(probe_src)
    caught = check_file(tmp)
    shutil.rmtree(tmpdir, ignore_errors=True)
    if caught:
        names = sorted(set(c[2] for c in caught))
        print("== 0. 自检 ==")
        print("   ✅ 藏在分支里的未定义名被报出来了：%s（判据有效）" % names)
        # ⚠️ 自检还要求它**只报那一个**：多报说明规则太松（会误报），
        #    少报说明太紧（会漏）。两个方向都要卡。
        if names != ["current"]:
            print("   ❌ 自检期望只报 ['current']，实际 %s —— 规则松紧不对" % names)
            return 1
        # 反方向：合法的嵌套作用域必须一条都不报（见 `clean_src` 的说明）
        if clean_hits:
            print("   ❌ 自检：**合法的嵌套函数/lambda 被误报了** %s —— "
                  "递归又钻进了嵌套作用域" % [(c[1], c[2]) for c in clean_hits])
            return 1
        print("   ✅ 合法的嵌套函数 / 闭包 / 嵌套 lambda 一条都没误报")
    else:
        print("== 0. 自检 ==")
        print("   ❌ **故意写的未定义名没被报出来 —— 这个检查器坏了，"
              "下面所有结果都不可信**")
        return 1

    print("\n== 结果 ==")
    if not problems:
        print("   ✅ 没有发现未定义的名字")
    else:
        print("   ❌ 发现 %d 处：" % len(problems))
        for path, line, name, kind in problems:
            rel = os.path.relpath(path, ROOT)
            print("   %s:%d  `%s`  （在 %s 作用域里）" % (rel, line, name, kind))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
