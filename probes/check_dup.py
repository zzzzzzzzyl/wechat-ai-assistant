# -*- coding: utf-8 -*-
"""只读排查：库里的消息顺序为什么和真实对话顺序对不上。

背景
----
`make_fingerprint()` 用的是 md5(is_self | sender_name | content)，
配合 UNIQUE(session_key, fingerprint) + INSERT OR IGNORE 去重。
副作用：**同一句话重复出现时，只保留第一次那条**，而它在表里的位置
被钉死在「第一次出现」的地方。

如果有人在一段对话里前后两次发了「图片」，第二次不会新增一行，
于是按 id 排出来的顺序就不再等于真实的对话顺序 ——
而 `get_recent()` 正是按 id 排序取最近 N 条的。

⚠️ 只读。
用法： python probes/check_dup.py [会话名]
"""

import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "state", "history.db")


def main():
    if not os.path.exists(DB):
        print("没有历史库: %s" % DB)
        return 1
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    sess = sys.argv[1] if len(sys.argv) > 1 else None
    if not sess:
        r = conn.execute("select session_key, count(*) n from messages "
                         "group by session_key order by n desc limit 1").fetchone()
        if not r:
            print("库是空的")
            return 0
        sess = r["session_key"]
    print("会话: %r" % sess)

    # ---------------------------------------------------------- 1. 指纹与重复
    print("\n" + "=" * 74)
    print("1. 同一内容存了几行（指纹含 is_self，所以判定不同会各存一行）")
    print("=" * 74)
    rows = list(conn.execute(
        "select id, is_self, sender_name, content from messages "
        "where session_key = ? order by id", (sess,)))
    by_content = Counter(r["content"] for r in rows)
    repeated = [(c, n) for c, n in by_content.items() if n > 1]
    if not repeated:
        print("  没有任何内容重复 —— 这个会话不受去重影响")
    else:
        for c, n in sorted(repeated, key=lambda x: -x[1]):
            ids = [r["id"] for r in rows if r["content"] == c]
            selves = [r["is_self"] for r in rows if r["content"] == c]
            print("  %-26r x%d   id=%s   is_self=%s" % (c[:24], n, ids, selves))

    # ---------------------------------------------------------- 2. 未判定
    print("\n" + "=" * 74)
    print("2. is_self 未判定的条目（这些进 prompt 时显示成「?」）")
    print("=" * 74)
    unk = [r for r in rows if r["is_self"] is None]
    print("  共 %d 条，占 %.0f%%" % (len(unk), 100.0 * len(unk) / max(1, len(rows))))
    for r in unk[:15]:
        print("    id=%-4d %r" % (r["id"], (r["content"] or "")[:40]))

    # ---------------------------------------------------------- 3. 占位符
    print("\n" + "=" * 74)
    print("3. 无信息量的占位/超长内容（进了 prompt 也只是噪音）")
    print("=" * 74)
    noise = [r for r in rows if (r["content"] or "").strip() in ("图片", "表情")
             or len(r["content"] or "") > 80]
    print("  共 %d 条" % len(noise))
    for r in noise[:12]:
        print("    id=%-4d %r" % (r["id"], (r["content"] or "")[:60]))

    # ---------------------------------------------------------- 4. 顺序自检
    print("\n" + "=" * 74)
    print("4. 顺序自检：库里相邻两条，是否真的在对话里相邻")
    print("=" * 74)
    print("  （没法直接验证，但可以从「同一批写入」看：同 created_at 的算一批）")
    batches = Counter(r["created_at"] for r in rows) if "created_at" in rows[0].keys() \
        else Counter()
    full = list(conn.execute(
        "select created_at, count(*) n from messages where session_key = ? "
        "group by created_at order by created_at", (sess,)))
    print("  %d 条消息落在 %d 个时间戳上：" % (len(rows), len(full)))
    for f in full[-10:]:
        print("    %s  -> %d 条" % (f["created_at"], f["n"]))
    print("  → 同一时间戳的多条是「一次读取里看到的」，它们之间顺序可信；")
    print("    跨时间戳的顺序 = 触发器读取顺序，中间漏读的消息会直接被跳过。")

    # ---------------------------------------------------------- 5. 最近有没有新重复
    print("\n" + "=" * 74)
    print("5. 最近 30 条里还有没有重复（判断「升级而非新增」是否生效）")
    print("=" * 74)
    recent = list(conn.execute(
        "select id, is_self, content from messages where session_key = ? "
        "order by id desc limit 30", (sess,)))
    cnt = Counter(r["content"] for r in recent)
    dup = {c: n for c, n in cnt.items() if n > 1}
    if recent:
        print("  这 30 条的 id 范围: %d ~ %d" % (recent[-1]["id"], recent[0]["id"]))
    if dup:
        print("  仍有 %d 组重复：" % len(dup))
        for c, n in dup.items():
            ids = [r["id"] for r in recent if r["content"] == c]
            print("    %-30r x%d  id=%s" % (c[:28], n, ids))
        print("  → 若这些 id 都早于本次修复，属历史遗留；不放心可重新观察几分钟")
    else:
        print("  无重复 ✓（说明 ?->对方 时走的是升级，没有再存一份）")

    conn.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
