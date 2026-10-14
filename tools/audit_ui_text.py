# -*- coding: utf-8 -*-
"""体检「设置界面给用户看的文案」—— 只读，只打印。

    python tools\\audit_ui_text.py            # 全部三段报告
    python tools\\audit_ui_text.py --dump     # 只把 label / help 全列出来（通读用）

为什么要它（2026-10-13，用户原话）：

    设置里面多余的提示也可以删掉，产品要面向用户的。

`settings.py` 的 `label` / `help` 和 `qt_ui/` 里的字面量**是直接显示给用户的**，
但它们长期是**按维护者的思路写的** —— 文件名、配置键名、毫秒数、CPU 百分比、
"实测踩过"的八卦，以及 label 和 help 说同一件事。这些东西**不会报错**，
只会让界面读起来像"开发者的备忘"。所以拿一个脚本定期扫一遍。

三段报告：

  ① **开发者词汇**：字符串字面量里出现 `settings.json` / `config.yaml` /
     `sessions.disabled` / `毫秒` / `一个核` / `实测` / `踩` … 的，列出行号。
     ⚠️ **注释和 docstring 不算** —— 那些地方本来就该写这些（脚本会跳过）。
  ② **label 和 help 重复**：help 把 label 已经说过的意思再说一遍（用户看不懂为什么
     要说两遍）。判据：去掉括号和标点后，help 里出现了 label 的全部实词。
  ③ **过长的 help**：超过 `LONG`（默认 90 字）的列出来 —— 不是硬错误，
     但界面上一行说明超过两三行就该想想是不是把"理由"写太多了。

⚠️ 这是**报告**，不是守门：硬守门在 `tests/test_qt_settings_ui.py` 第 5 节
（label / help 不许有 `**` 或反引号 —— 界面不渲染 markdown，会原样漏出去）。
"""

import argparse
import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 只扫这几个文件：它们的字符串字面量**会被画到界面上**。
FILES = [
    "wechat_assistant/settings.py",
    "wechat_assistant/qt_ui/settings_ui.py",
    "wechat_assistant/qt_ui/session_ui.py",
    "wechat_assistant/prompts.py",
]

#: ④ 悬浮窗**底部状态行**的文案来源（2026-10-13 用户要求「悬浮窗下面的提示精简一点」）。
#: 面板自己赋的 `self._status`、`run_watch` 推过去的 `emit(...)`、
#: 跟随器提示的 `post_status(...)` —— 三处都会显示在同一行里。
STATUS_SOURCES = [
    ("wechat_assistant/qt_ui/panel.py", "_status"),
    ("run_watch.py", "emit"),
    ("wechat_assistant/follow.py", "post_status"),
]

#: 状态行一行大概能放多少字（面板 340px、小字号）。
#: ⚠️ 超过就会折成两行 —— 不是错误，但状态行是**辅助信息**，
#:    折行会把面板底部垫高。判据来自那次"状态文字把面板顶宽"的血案（见 panel.lbl_status）。
STATUS_LINE = 20

#: 开发者词汇 -> 为什么不该出现在给用户看的文字里
MARKERS = {
    "settings.json": "文件名（分层配置是实现细节）",
    "state.json": "文件名",
    "config.yaml": "文件名",
    "secrets.yaml": "文件名",
    "sessions.disabled": "配置键名",
    "ocr_read": "配置键名",
    "实测": "维护者的取证过程",
    "踩": "维护者的血案",
    "一个核": "CPU 百分比是开发指标",
    "毫秒": "毫秒数是开发指标",
    "ms（": "毫秒数是开发指标",
    "落盘": "实现动作",
    "覆盖层": "实现机制",
    "正则": "实现手段",
    "UIA": "内部术语（用户只关心「读不到就换一种」）",
}

#: 超过这么多字就提醒（界面上一行说明）。
LONG = 90

#: 明确的例外：这些字面量虽然带"开发者词汇"，但**不是设置界面上的文案**
#: （比如配置坏了时的报错，那是给维护者定位问题用的）。
ALLOW = {
    "config.yaml 根节点必须是映射": "配置文件坏了时的报错，不进设置界面",
}


def _is_docstring(src_lines, node):
    """这个字符串节点是不是 docstring / 注释块 —— 那些地方写开发者词汇是对的。"""
    if node.value.count("\n") >= 2:
        return True                       # 多行长文本基本都是 docstring/模板
    line = src_lines[node.lineno - 1]
    head = line.lstrip()
    if head.startswith("#"):
        return True
    # 三引号包起来的单行 docstring
    if head.startswith('"""') or head.startswith("'''"):
        return True
    return False


def scan_literals(path, dump=False):
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines()
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        s = node.value
        if _is_docstring(lines, node) or len(s) < 4:
            continue
        if not re.search(r"[\u4e00-\u9fff]", s):
            continue
        out.append((node.lineno, s))
    return out


def report_markers():
    print("=" * 96)
    print("① 开发者词汇（这些字面量会被画到界面上）")
    bad = 0
    for rel in FILES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        hits = []
        for ln, s in scan_literals(path):
            if s.strip() in ALLOW:
                continue
            for m, why in MARKERS.items():
                if m in s:
                    hits.append((ln, m, why, s.replace("\n", " / ")[:96]))
                    break
        if hits:
            print("\n  %s" % rel)
            for ln, m, why, s in sorted(hits):
                print("    L%-5d [%s] %s" % (ln, m, why))
                print("           %s" % s)
                bad += 1
    print("\n  合计 %d 处" % bad)
    if not bad:
        print("  ✅ 干净")
    return bad


def report_redundant():
    from wechat_assistant.settings import SPEC

    print("=" * 96)
    print("② label 和 help 说同一件事（用户会想「为什么要说两遍」）")
    bad = 0
    for s in SPEC:
        if not s.help:
            continue
        core = re.sub(r"[（）()「」【】、，。：；·—\s]", "", s.label)
        if len(core) < 3:
            continue
        if core in re.sub(r"[（）()「」【】、，。：；·—\s]", "", s.help):
            print("    %-36s label=%r" % (s.key, s.label))
            print("         help=%s" % s.help.replace("\n", " ")[:96])
            bad += 1
    print("\n  合计 %d 处" % bad)
    if not bad:
        print("  ✅ 没有明显重复")
    return bad


def report_long():
    from wechat_assistant.settings import SPEC

    print("=" * 96)
    print("③ 过长的说明（超过 %d 字：界面上一行说明别超过两三行）" % LONG)
    bad = 0
    for s in SPEC:
        if s.help and len(s.help) > LONG:
            print("    %-36s %d 字" % (s.key, len(s.help)))
            print("         %s" % s.help.replace("\n", " ")[:150])
            bad += 1
    print("\n  合计 %d 处" % bad)
    return bad


def report_status_lines():
    """④ 悬浮窗底部状态行的文案：列出来 + 量长短（用户要求"精简一点"）。"""
    print("=" * 96)
    print("④ 悬浮窗底部状态行的文案（一行 ≤ %d 字最舒服；超过会折行）" % STATUS_LINE)
    long_ones, marked = 0, 0
    for rel, attr in STATUS_SOURCES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        rows = []

        def _take(node, v):
            """把"这一处给状态行赋的文案"取出来（字符串、`模板 % x`、三元都认）。"""
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                rows.append((node.lineno, v.value))
            elif (isinstance(v, ast.BinOp)
                  and isinstance(v.left, ast.Constant) and isinstance(v.left.value, str)):
                rows.append((node.lineno, v.left.value))   # 模板本身（`%d` 也算字）
            elif isinstance(v, ast.IfExp):
                for sub in (v.body, v.orelse):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        rows.append((node.lineno, sub.value))

        for node in ast.walk(tree):
            v = None
            if attr == "_status":
                if not isinstance(node, ast.Assign):
                    continue
                tgt = node.targets[0]
                if not (isinstance(tgt, ast.Attribute) and tgt.attr == "_status"):
                    continue
                v = node.value
            else:
                if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
                    continue
                c = node.value
                if not (isinstance(c.func, ast.Name) and c.func.id == attr and c.args):
                    continue
                v = c.args[0]
            if v is not None:
                _take(node, v)
        if not rows:
            continue
        print("\n  %s" % rel)
        for ln, s in sorted(set(rows)):
            n = len(s)
            flags = []
            if n > STATUS_LINE:
                flags.append("长 %d 字" % n)
                long_ones += 1
            for m in MARKERS:
                if m in s:
                    flags.append("开发者词[%s]" % m)
                    marked += 1
                    break
            print("    L%-5d %-3s %s%s" % (ln, "%d字" % n, s,
                                           ("   ← " + "、".join(flags)) if flags else ""))
    print("\n  偏长的 %d 条、含开发者词的 %d 条" % (long_ones, marked))
    if not long_ones and not marked:
        print("  ✅ 都够短")
    return long_ones + marked


def dump_all():
    from wechat_assistant.settings import GROUPS, SPEC

    print("=" * 96)
    print("分组")
    for gid, name, desc in GROUPS:
        print("  %-9s %-10s %s" % (gid, name, desc))
    print()
    print("=" * 96)
    print("每一项（label / help）")
    for s in SPEC:
        print("  [%s] %s" % (s.group, s.label))
        if s.help:
            print("        %s" % s.help.replace("\n", " "))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="只把全部文案列出来")
    args = ap.parse_args()
    if args.dump:
        dump_all()
        return 0
    n = (report_markers() + report_redundant() + report_long()
         + report_status_lines())
    print("=" * 96)
    print("合计 %d 处值得看一眼（都不报错，只是读起来像开发者的备忘 / 太长）" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
