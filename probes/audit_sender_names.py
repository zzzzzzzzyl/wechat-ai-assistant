# -*- coding: utf-8 -*-
"""查历史库里有没有「昵称被图片里的字污染」的痕迹。

排查思路：群聊里干净的昵称应该**反复出现**（同一个人发多条）。
如果某个 sender 只出现过一次、而且长得像截图里的字（含标点/空格/英文混排/
长度明显偏长），就值得怀疑。反过来，如果同一个昵称出现在**很多不同文本**的消息上，
说明它稳定复用 —— 那是真昵称。

再叠加一条：把每个昵称「首次出现时那条消息的内容」列出来，
如果那条消息是「图片/动画表情」，而昵称又只出现过一次 —— 高度可疑。

只读。用法：python probes/audit_sender_names.py
"""

import sqlite3
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, ".")

from wechat_assistant.paths import resolve as _rp  # noqa: E402


def main():
    import os
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "state", "history.db")
    print("库：%s" % db)
    c = sqlite3.connect(db)
    rows = c.execute(
        "SELECT session_key, content, sender_name, is_self, from_ai, created_at "
        "FROM messages ORDER BY id").fetchall()
    print("共 %d 条" % len(rows))

    PLACEHOLDERS = ("图片", "动画表情", "视频", "文件")

    # 按会话分组
    per = defaultdict(list)
    for sk, content, sender, is_self, from_ai, ts in rows:
        per[sk].append((content or "", sender or "", is_self, ts))

    total_names = 0
    suspect = []
    for sk, items in per.items():
        names = defaultdict(list)
        for content, sender, is_self, ts in items:
            if sender:
                names[sender].append((content, ts))
        if not names:
            continue
        total_names += len(names)
        print()
        print("【%s】%d 条消息，%d 个不同 sender 名" % (sk, len(items), len(names)))
        for n, occ in sorted(names.items(), key=lambda kv: -len(kv[1])):
            first_content = occ[0][0].strip()
            flag = ""
            if len(occ) == 1:
                flag = "  ← 只出现过 1 次"
                if first_content in PLACEHOLDERS:
                    flag += "，且首现是【%s】⚠️ 可疑" % first_content
                    suspect.append((sk, n, first_content))
            print("    %-24r x%-3d  首现: %r%s"
                  % (n, len(occ), first_content[:26], flag))

    print()
    print("=" * 72)
    print("sender 名总数：%d" % total_names)
    if suspect:
        print("⚠️ 可疑（只出现一次 + 首现就是图片占位符）：")
        for sk, n, fc in suspect:
            print("   [%s] %r  <- 【%s】" % (sk, n, fc))
    else:
        print("没有「只出现一次且首现是图片」的 sender —— 目前的库里看不到污染痕迹。")
        print()
        print("⚠️ 但这**不等于没问题**：现在没翻车，只说明「昵称恰好排在图片上方」")
        print("   这个巧合每次都成立了。判据本身还是缺的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
