# -*- coding: utf-8 -*-
"""「设置 → 会话」那个列表里到底有什么、哪些是噪声。

    python probes/diag_session_list.py

背景（2026-10-11，用户问「设置会话列表里看起来有很多噪声？」）：

    列表现在是 **注册表 ∪ 屏快照库**（`sessions.merge_known_sessions`）。
    ⚠️ 并库是**故意**的 —— 注册表只在正常退出时落盘，不并库的话
       强杀一次列表就回到上次干净退出的样子。但副作用是：
       **库里攒下的历史垃圾也一起露出来了。**

    噪声有三类，处理方式完全不同：

      ① **分裂变体** —— 同一个会话被存成好几个名字。这是**左边界 bug**
         （标题掉了第一个字）留下的：`公众号` / `众号`、
         `giao (4)` / `iao (4)` / `(4)`。
         -> 只能靠**一次性合并**（把变体的行并到主 key），或者干脆清掉。
      ② **系统会话** —— 公众号 / 微信支付 / 服务通知 之类。
         它们**本来就不该被记录**，现在靠 `sessions.disabled_contains` 挡新的；
         -> 但**老的还在库里**，得清。
      ③ **认不出来的碎片** —— 比如只有 `(4)` 这种。多半是 ① 的极端情况。

⚠️ 纯只读：只查库和注册表，不改任何东西。
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant.config import load_config, resolve_state_path  # noqa: E402
from wechat_assistant.sessions import SessionRegistry  # noqa: E402


def looks_like_fragment(short, long_):
    """`short` 是不是 `long_` 的**碎片**（丢了开头几个字）。

    判据：short 是 long 的子串，而且 short 比 long 短 —— 这正是掉字的形态。
    ⚠️ 只用来**提示**，不自动合并：`A` 是 `AB` 的子串也可能是两个真会话
        （比如「张三」和「张三丰」）。所以这里只列出来让人确认。
    """
    if not short or not long_ or short == long_:
        return False
    if len(short) >= len(long_):
        return False
    return short in long_


def main():
    cfg = load_config()
    sp = resolve_state_path(cfg)
    state_dir = os.path.dirname(sp) or "."
    sdb = os.path.join(state_dir, "screens.db")
    hdb = os.path.join(state_dir, "history.db")

    reg = SessionRegistry(sp, default_mode=cfg.get("sessions.default_mode", "on"))
    disabled_contains = [d for d in (cfg.get("sessions.disabled_contains") or []) if d]

    db_names = {}
    # ⚠️ `messages` 表**没有 `ts` 列**（第一版写的就是 ts，于是整张表静默跳过，
    #    只报了 screens 那边的会话 —— 看起来"少了一些"但不知道为什么）。
    #    用 `rowid`（一定存在）拿条数就够了，时间那份以 screens 为准。
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
                    prev = db_names.get(k)
                    if prev is None or (t or "") > prev[0]:
                        db_names[k] = (t or "", n)
            c.close()
        except Exception as e:
            print("读 %s 失败: %s: %s" % (os.path.basename(path),
                                        type(e).__name__, e))

    print("注册表（state.json）: %d 个" % len(reg.sessions))
    print("屏快照/消息库    : %d 个" % len(db_names))
    print()

    # 设置里会显示的那份名单 = 注册表 ∪ 库
    names = set(reg.sessions) | set(db_names)
    print("设置里会显示       : %d 个" % len(names))
    print("=" * 74)

    sys_hits, frags, normal = [], [], []
    for n in sorted(names, key=lambda x: db_names.get(x, ("", 0))[0], reverse=True):
        t, cnt = db_names.get(n, ("", 0))
        is_sys = reg.is_system_session(n)
        # 找它的"碎片版"：别名字典里有没有谁把它当子串
        frag_of = [o for o in names if o != n and looks_like_fragment(n, o)]
        # 反过来：它自己是不是某个更长的名字的碎片
        is_frag = [o for o in names if o != n and looks_like_fragment(o, n)]
        tag = []
        if is_sys:
            tag.append("系统会话")
        if is_frag:
            tag.append("疑似碎片(是 %s 掉字)" % is_frag[0])
        if frag_of:
            tag.append("有碎片版: %s" % ", ".join(frag_of[:3]))
        row = ("  %-32s %s %4d 条/屏  %s"
               % (repr(n)[:32], t or "(无时间)", cnt, " ".join(tag)))
        if is_sys:
            sys_hits.append(row)
        elif is_frag:
            frags.append(row)
        else:
            normal.append(row)

    print("\n== ① 系统会话（本来就不该记）——%d 个 ==" % len(sys_hits))
    for r in sys_hits:
        print(r)
    print("\n== ② 疑似碎片（掉字留下的分裂）——%d 个 ==" % len(frags))
    for r in frags:
        print(r)
    print("\n== ③ 看起来正常的 ——%d 个 ==" % len(normal))
    for r in normal:
        print(r)
    print()
    print("挡住新系统会话的规则 sessions.disabled_contains:")
    for d in disabled_contains:
        print("    %s" % d)
    print()
    print("⚠️ 这些**都还在库里** —— 挡规则只拦新的，老的得单独清/合并。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
