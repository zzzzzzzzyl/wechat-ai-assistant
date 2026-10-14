# -*- coding: utf-8 -*-
"""清理「设置 → 会话」里的历史噪声 —— **先看后做，默认只 dry-run**。

    python tools/clean_sessions.py            # 只打印会做什么（默认）
    python tools/clean_sessions.py --write    # 真的做（**先备份**）

起因（2026-10-11，用户问「设置会话列表里看起来有很多噪声？」）：

    列表 = 注册表 ∪ 屏快照库。并库是**故意**的（注册表只在正常退出时落盘，
    不并的话强杀一次列表就空了）—— 但副作用是**历史垃圾也露出来了**。

    噪声的来源是**已经修好的**那个左边界 bug（标题掉了第一个字）：

        '众号'                <- '公众号'        掉「公」
        'iao (4)'             <- 'giao (4)'      掉「g」
        '(4)'                 <- 'iao (4)'       再掉「i」
        '财信息学院23级级群(499)'  <- '广财信息学院23级级群(499)'  掉「广」

    ⚠️ 那 bug 已经修了，**新的会记对** —— 但老行还在库里，得清。

处理顺序（**顺序要紧**）：

    ① **先把碎片并到完整名**（`B.endswith(A)` 且 A 够长）——
       这样 '众号' 的那 40 屏会归到 '公众号'，
    ② **再删系统会话 / 垃圾** —— 上一步并过去的那 40 屏**跟着一起删**
       （它们本来就是公众号的文章页，不是聊天，删掉才对）。
       反过来先删的话，'众号' 那 40 屏就白留在库里了。

⚠️ 判据刻意保守：
    · 合并只认「**丢了开头**」这一种（`B.endswith(A)`）—— 那正是掉字的形态。
    · 「多了个后缀」那类（`示例代购群` vs `...(4)`、
      `吃喝玩乐福利君` vs `...@美团吃喝玩乐`）**不自动合并** ——
      那可能是微信自己换的显示名，也可能是两个不同的会话，**列出来让人决定**。
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant.config import load_config, resolve_state_path  # noqa: E402
from wechat_assistant.sessions import SessionRegistry  # noqa: E402

#: 碎片至少要有这么长才认（太短的（比如 '(4)'）会到处当子串，误伤）
MIN_FRAGMENT = 3


def resolve_chain(name: str, rename: dict) -> str:
    """把合并链解到底（`(4)` -> `iao (4)` -> `giao (4)`）。

    ⚠️ 必须显式解链，**别指望字典的迭代顺序**：`(4)` 的行先被搬到 `iao (4)`，
        而 `iao (4)` 自己也要往 `giao (4)` 搬 —— 顺序反了就只搬一层，
        `(4)` 那批行会**留在原地变成孤儿**（第一版就是这样，靠 sorted 的顺序
        侥幸对了，但那是巧合）。
    """
    seen = set()
    cur = name
    while cur in rename and cur not in seen:
        seen.add(cur)
        cur = rename[cur]
    return cur


def is_garbage(name: str) -> str:
    """一眼就知道不是会话名的：返回原因，否则空串。"""
    n = (name or "").strip()
    if not n:
        return "空名字"
    if len(n) == 1:
        return "只有一个字（OCR 垃圾）"
    if re.fullmatch(r"[\d\s()（）\[\]·．.,，、:：;；!！?？\-—_]+", n):
        return "全是数字/标点"
    return ""


def fragment_of(name: str, others, min_len: int = MIN_FRAGMENT) -> str:
    """`name` 是不是别人**掉了开头**的版本？返回那个更完整的名字。

    ⚠️ `min_len` 有两档（见调用处）：
        · **合并**用 3 —— 太短的（`(4)`）到处当子串，容易误并；
        · **认系统会话**用 2 —— `众号`（2 字）就是 `公众号` 掉了「公」，
          它那 40 条记录全是公众号文章页，必须一起删掉。
    """
    if not name or len(name) < min_len:
        return ""
    cands = [o for o in others
             if o != name and len(o) > len(name) and o.endswith(name)]
    if not cands:
        return ""
    # 有多个就取**最短的那个完整名**（最保守：少并一点）
    return min(cands, key=len)


def normalize_for_compare(name: str) -> str:
    """比"长得像不像"时用的归一化：去掉空格和 emoji。

    ⚠️ 只用于**提示**，不用于自动合并 —— `示例群名(13)` 和
        `示例群名 (13)`（多个空格）显然是同一个，
        但那要人来确认，自动并两个真会话的代价更大。
    """
    from wechat_assistant.sessions import norm_title
    return norm_title(name).replace(" ", "")


def suffix_variant(name: str, others) -> str:
    """「多了个后缀 / 只差空格」那类 —— 只**提示**，不自动并。"""
    for o in others:
        if o == name or len(o) <= len(name):
            continue
        if o.startswith(name) and not o.endswith(name):
            return o
    n = normalize_for_compare(name)
    for o in others:
        if o != name and normalize_for_compare(o) == n:
            return o
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="真的改（默认只打印；会先备份）")
    ap.add_argument("--keep", action="append", default=[],
                    help="要**保留**的会话名（可多次给）—— 免得误删你还想要的")
    ap.add_argument("--drop", action="append", default=[],
                    help="要**删掉**的会话名（可多次给）—— 给 `is_garbage` 认不出的"
                         "脏名字用（OCR 抓错画面的产物，比如 '简历'）")
    args = ap.parse_args()

    cfg = load_config()
    sp = resolve_state_path(cfg)
    state_dir = os.path.dirname(sp) or "."
    sdb = os.path.join(state_dir, "screens.db")
    hdb = os.path.join(state_dir, "history.db")
    disabled = [d for d in (cfg.get("sessions.disabled_contains") or []) if d]

    reg = SessionRegistry(sp, default_mode=cfg.get("sessions.default_mode", "on"))

    # ---- 收集：库里的会话 + 每个的条数
    counts, last = {}, {}
    for path, table, col in ((sdb, "screens", "seen_at"),
                             (hdb, "messages", "rowid")):
        if not os.path.exists(path):
            continue
        try:
            c = sqlite3.connect(path)
            for k, t, n in c.execute(
                    "SELECT session_key, MAX(%s), COUNT(*) FROM %s "
                    "GROUP BY session_key" % (col, table)):
                if k:
                    counts[k] = counts.get(k, 0) + int(n or 0)
                    if (t or "") and str(t) > last.get(k, ""):
                        last[k] = str(t)
            c.close()
        except Exception as e:
            print("  ⚠️ 读 %s 失败: %s: %s" % (os.path.basename(path),
                                             type(e).__name__, e))

    names = set(reg.sessions) | set(counts)
    keep = set(args.keep)

    # ---- ① 碎片合并（先做，顺序要紧）
    rename = {}
    for n in sorted(names):
        if n in keep:
            continue
        full = fragment_of(n, names - keep)          # 合并档：min_len=3
        if full:
            rename[n] = full
    # ⚠️ **解链到底**（`(4)` -> `iao (4)` -> `giao (4)`），别依赖字典顺序
    rename = {a: resolve_chain(a, rename) for a in rename}
    rename = {a: b for a, b in rename.items() if a != b}

    # ---- ② 要删的
    drop = {}
    for n in sorted(names):
        if n in keep:
            continue
        g = is_garbage(n)
        if g:
            drop[n] = g
            continue
        # 系统会话：① 直接命中模式；② 它是某个模式的**碎片**（掉了开头）
        #   ⚠️ 第 ② 条用 min_len=**2**：`众号` 就是 `公众号` 掉「公」，
        #      抓不到的话它那 40 条文章页会白留在库里。
        hit = [d for d in disabled if d and d in n]
        if not hit:
            for d in disabled:
                if not d:
                    continue
                if fragment_of(n, [d], min_len=2) == d:
                    hit = [d]
                    break
        if hit:
            drop[n] = "系统会话（命中「%s」）" % hit[0]
    # 合并链上被搬空的名字也要从注册表里去掉（不然列表还留着空壳）
    for a in rename:
        drop.setdefault(a, "已并入「%s」" % rename[a])

    # ---- ②b **手工点名**的（`is_garbage` 认不出来的）
    #
    # ⚠️ 为什么需要这个开关（2026-10-13）：有些脏名字**形态上完全正常**，
    #    自动判据不该猜 —— 它们是 **OCR 抓错了画面**（读到别的窗口）的产物：
    #
    #        '简历'                  -> 资源管理器 + PyCharm + 我们自己的面板的字
    #        '示例脏标题'    -> 任务栏上两个窗口标题拼起来的
    #        '①·G'                   -> 链接卡片 + 输入框 + 「发送」按钮
    #
    #    这些要人来认；认出来之后**能复现地删**（而不是手写 SQL 打库）。
    for n in args.drop:
        if n in keep:
            continue
        drop.setdefault(n, "手工指定删除")

    # ---- 报告
    # ⚠️ 剩下的 = names - (rename ∪ drop)。**别写成 `len - len(rename) - len(drop)`**
    #    —— 被合并的那几个名字也在 `drop` 里（要从注册表去掉），那样会**重复减**，
    #    报出来的数字偏小（第一版就是这样）。
    left = len(set(names) - set(rename) - set(drop))
    print("=" * 78)
    print("库里 %d 个会话 / 注册表 %d 个 / 设置里现在显示 %d 个"
          % (len(counts), len(reg.sessions), len(names)))
    print("清理后剩 %d 个（合并 %d、删除 %d）"
          % (left, len(rename), len(drop)))
    print("=" * 78)

    print("\n① 碎片合并（掉开头那类）—— %d 条" % len(rename))
    if not rename:
        print("   （没有）")
    for a, b in sorted(rename.items()):
        print("   %-30s -> %-30s  （%d 条记录跟着过去）"
              % (repr(a)[:30], repr(b)[:30], counts.get(a, 0)))

    print("\n② 删除 —— %d 条" % len(drop))
    if not drop:
        print("   （没有）")
    for a, why in sorted(drop.items()):
        tag = ""
        if a in rename.values():
            tag = "（含合并过来的）"
        print("   %-30s %-28s %d 条 %s"
              % (repr(a)[:30], why[:28], counts.get(a, 0), tag))

    # ---- ③ 只提示、不动的
    print("\n③ **不自动处理**、留给你决定的（可能是别名，也可能是两个真会话）")
    amb = []
    for n in sorted(names - set(drop) - set(rename) - keep):
        v = suffix_variant(n, names)
        if v:
            amb.append((n, v))
    if not amb:
        print("   （没有）")
    for a, b in amb:
        print("   %-30s 和 %-30s 都以它开头" % (repr(a)[:30], repr(b)[:30]))
    print("   -> 想保留某个就加 --keep \"名字\"；确实是同一个的话手动告诉我，"
          "别让它自动猜。")

    if not args.write:
        print("\n（这是 dry-run。加 --write 才会真的改 —— 会先备份。）")
        return 0

    # ---- 执行 ----------------------------------------------------------
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bdir = os.path.join(state_dir, "_backup_" + stamp)
    os.makedirs(bdir, exist_ok=True)
    for p in (sdb, hdb, sp):
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(bdir, os.path.basename(p)))
    print("\n已备份到 %s" % bdir)

    moved = deleted = 0
    for path, table in ((sdb, "screens"), (hdb, "messages")):
        if not os.path.exists(path):
            continue
        c = sqlite3.connect(path)
        try:
            for a, b in rename.items():
                cur = c.execute("UPDATE %s SET session_key=? WHERE session_key=?"
                                % table, (b, a))
                moved += cur.rowcount
            for a in drop:
                cur = c.execute("DELETE FROM %s WHERE session_key=?" % table, (a,))
                deleted += cur.rowcount
            c.commit()
        finally:
            c.close()
        print("  %s: 移动 %d 行、删除 %d 行" % (os.path.basename(path), moved, deleted))

    # 注册表：删掉那些条目（合并过去的那些由主名承载）
    before = len(reg.sessions)
    for a in list(drop) + list(rename):
        reg.sessions.pop(a, None)
    reg.save()
    print("  注册表: %d -> %d 个" % (before, len(reg.sessions)))
    print("\n完成。重启程序后设置里的列表就是干净的。")
    print("⚠️ 想回退：把 %s 里的文件拷回 state/ 覆盖。" % bdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
