# -*- coding: utf-8 -*-
"""统计屏快照里「图片 / 动画表情」这类只在库里留一个占位符的消息占多少。

只读。用来判断「要不要给图片做 OCR / 识物」这件事到底值不值。
"""
import json
import sqlite3
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = r"E:\Claude\wechat-ai-assistant\state\screens.db"

c = sqlite3.connect(DB)
rows = c.execute("select session_key, items_json from screens").fetchall()

HARD = {"图片", "动画表情", "视频", "语音", "文件", "转账", "链接", "引用"}
tot = Counter()
per = {}
for sk, js in rows:
    try:
        items = json.loads(js or "[]")
    except Exception:
        continue
    d = per.setdefault(sk, Counter())
    for it in items:
        t = (it.get("text") or "").strip()
        tot["总条目"] += 1
        d["总条目"] += 1
        if t in HARD:
            tot["占位符"] += 1
            d["占位符"] += 1

print("屏快照库：%d 屏" % len(rows))
print("条目总数 %d，其中占位符 %d（%.1f%%）"
      % (tot["总条目"], tot["占位符"],
         100.0 * tot["占位符"] / max(1, tot["总条目"])))

# 逐会话
print()
print("%-28s %6s %6s %7s" % ("会话", "条目", "占位符", "占比"))
for sk, d in sorted(per.items(), key=lambda kv: -kv[1]["占位符"]):
    print("%-28s %6d %6d %6.1f%%"
          % (sk[:28], d["总条目"], d["占位符"],
             100.0 * d["占位符"] / max(1, d["总条目"])))

# 占位符里各是什么
print()
cnt = Counter()
for sk, js in rows:
    try:
        items = json.loads(js or "[]")
    except Exception:
        continue
    for it in items:
        t = (it.get("text") or "").strip()
        if t in HARD:
            cnt[t] += 1
print("占位符明细：", dict(cnt))
