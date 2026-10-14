# -*- coding: utf-8 -*-
"""清库 —— 把「聊天记录」和「配置」分开处理。

    python probes/clear_stores.py            # 只看有什么（**不动手**）
    python probes/clear_stores.py --yes      # 删聊天记录（history.db / screens.db）
    python probes/clear_stores.py --all --yes # 连配置一起删（要重新设一遍！）

⚠️⚠️ **为什么不能一句"全删了"**
    这个目录里混着两类完全不同的东西，删错的代价差很远：

    | 文件 | 是什么 | 删了会怎样 |
    |---|---|---|
    | `history.db` / `screens.db` | **聊天记录**（最近上下文窗口） | 没了就没了，下次自己攒。**不重要** |
    | `state.json` | **19 个会话的配置**：开/关、人设、知识库绑定、游标 | ⚠️ **要重新一个个设** |
    | `settings.json` | **关系设定** + 读取方式（`backend`）等界面设置 | ⚠️ **要重新设** |
    | `secrets.yaml`（在项目根） | **API Key** | ⚠️ 要重新填 |

    所以默认**只删聊天记录**；要连配置一起删必须显式加 `--all`。

⚠️ 删之前确认**没有实例在跑**（SQLite 占着文件时删掉，进程一写又回来了）。
   脚本会自己检查一次。

⚠️ 这个脚本**不碰微信**，只删本项目 `state/` 下自己生成的文件。
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import paths  # noqa: E402

#: 一行是「一次读取看到的一屏」/「一条消息」—— 真正的聊天内容
CHAT_FILES = ("history.db", "screens.db")
#: 配置性质：删了要重设
CONFIG_FILES = ("state.json", "settings.json")


def count_rows(path):
    """返回 [(表名, 行数, 会话数)]；顺手确认它是个能打开的库。"""
    out = []
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    except Exception as e:
        return [("(打不开: %s)" % e, 0, 0)]
    try:
        tabs = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tabs:
            try:
                n = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            except Exception:
                n = -1
            sess = -1
            if n > 0:
                try:
                    sess = c.execute(
                        "SELECT COUNT(DISTINCT session_key) FROM %s" % t
                    ).fetchone()[0]
                except Exception:
                    pass
            out.append((t, n, sess))
    finally:
        c.close()
    return out


def running_instances():
    """有几处 `run_watch.py` 在跑（用 wmic 拿不到就退化成"不知道"）。"""
    try:
        import subprocess
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get",
             "ProcessId,CommandLine"],
            capture_output=True, text=True, timeout=15).stdout or ""
        return sum(1 for line in out.splitlines() if "run_watch" in line)
    except Exception:
        return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="真的删（不加就只看看）")
    ap.add_argument("--state", action="store_true",
                    help="连 state.json 一起删（会话开/关·人设·知识库绑定·游标）")
    ap.add_argument("--all", action="store_true",
                    help="连 settings.json 也删（关系设定、读取方式）—— 要重新设")
    ap.add_argument("--keep-backup", action="store_true",
                    help="删之前先改名成 .deleted-<时间> 备份，而不是直接删")
    args = ap.parse_args()

    # ⚠️ 分三档，**不要做成一个布尔** —— 这三类东西删错的代价完全不同：
    #      默认        只删聊天记录（没了就没了，本来也是自动淘汰的窗口）
    #      --state     再加 state.json（全默认时删了不丢什么，但要是你逐个
    #                  会话设过开/关或绑过知识库，那就得重设）
    #      --all       再加 settings.json（关系设定 + 读取方式）
    drop = list(CHAT_FILES)
    if args.state or args.all:
        drop.append("state.json")
    if args.all:
        drop.append("settings.json")

    base = str(paths.resolve("state/history.db"))
    statedir = os.path.dirname(base)
    print("state 目录: %s" % statedir)

    n = running_instances()
    if n > 0:
        print("\n⚠️ 还有 %d 个 run_watch.py 在跑 —— **先关掉**，否则删了它又会写回来。" % n)
        if not args.yes:
            return 1
    elif n == 0:
        print("没有实例在跑 ✅")

    targets = drop

    print("\n== 聊天记录（要删）==")
    total = 0
    for name in CHAT_FILES:
        p = os.path.join(statedir, name)
        if not os.path.exists(p):
            print("   %-16s 不存在" % name)
            continue
        size = os.path.getsize(p)
        total += size
        print("   %-16s %10s B" % (name, format(size, ",")))
        for t, rows, sess in count_rows(p):
            extra = "（%d 个会话）" % sess if sess and sess > 0 else ""
            print("        %-18s %6s 行 %s" % (t, format(rows, ","), extra))

    # 旧备份（历史上手工留的）
    for f in sorted(os.listdir(statedir)):
        if ".bak-" in f or ".deleted-" in f:
            print("   %-16s %10s B  ← 旧备份，也算数据"
                  % (f, format(os.path.getsize(os.path.join(statedir, f)), ",")))

    print("\n== 配置 ==")
    for name in CONFIG_FILES:
        p = os.path.join(statedir, name)
        if not os.path.exists(p):
            continue
        mark = "**要删**" if name in drop else "**保留**"
        print("   %-16s %10s B   %s" % (name, format(os.path.getsize(p), ","), mark))
    kept = [n for n in CONFIG_FILES if n not in drop]
    if kept:
        print("   ⚠️ 保留 %s：" % "、".join(kept))
        if "state.json" in kept:
            print("        state.json   = 每个会话的开/关、人设、知识库绑定、"
                  "「最近读到的尾巴」游标")
        if "settings.json" in kept:
            print("        settings.json = 关系设定（谁是谁）、读取方式 backend 等界面设置")
        print("      要一起删：--state（只删 state.json）/ --all（两个都删）")

    if not args.yes:
        print("\n（这是**只看不动手**。要真删加 --yes）")
        return 0

    import time
    stamp = time.strftime("%Y%m%d-%H%M%S")
    print("\n== 开始删 ==")
    # ⚠️ 旧备份也一起清掉：它们同样是聊天记录，留着等于没删干净
    extra = [f for f in sorted(os.listdir(statedir))
             if ".bak-" in f and f.startswith("history.db")]
    for name in list(targets) + extra:
        p = os.path.join(statedir, name)
        if not os.path.exists(p):
            continue
        if args.keep_backup:
            dst = p + ".deleted-" + stamp
            os.replace(p, dst)
            print("   改名备份 %s -> %s" % (name, os.path.basename(dst)))
        else:
            os.remove(p)
            print("   已删 %s" % name)

    print("\n== 删完还剩什么 ==")
    for f in sorted(os.listdir(statedir)):
        print("   %-34s %10s B"
              % (f, format(os.path.getsize(os.path.join(statedir, f)), ",")))
    print("\n⚠️ 程序下次启动会自己重建空库（表结构在代码里建，不依赖旧文件）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
