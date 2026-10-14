# -*- coding: utf-8 -*-
"""看 OCR 记录里**具体错在哪**，并分类"还能救"和"OCR 固有"。

    python probes/diag_ocr_errors.py

背景：用户问「识别的会话记录现在存在着一些问题？看起来是 ocr 无法避免的出错」。
要回答这个问题，光看几条错的没用 —— 必须**分类统计**，否则分不清
"这是 OCR 的物理极限"和"这是我们自己代码的问题"。

⚠️ 纯只读：只查 `screens.db` / `history.db`。
"""

import os
import re
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant.config import load_config, resolve_state_path  # noqa: E402

#: 中文正文里**不该出现**的字符 —— 出现了基本就是 OCR 把图形/emoji 认成了字。
#: ⚠️ 这不是"错字"，是**图形被当字读**：实测 ♥️ -> 口口、😝😜🤪 -> 00000。
SUSPECT_CHARS = "口囗〇○●■□▢0Oo□"
#: 连续这样的字符达到几个就认为是"一团图形被认成字"
RUN_MIN = 2

#: 一行里出现这些，多半是 OCR 把两行/两列粘在一起（行切分出错）
def looks_garbled(text: str) -> str:
    """给一条正文打个"可疑"标签，返回原因（空串 = 正常）。"""
    t = (text or "").strip()
    if not t:
        return "空"
    # ① 图形被认成字：成串的 口 / 0 / O
    for ch in "口囗0O":
        if re.search(re.escape(ch) * RUN_MIN, t):
            return "图形被认成字（%s×%d+）" % (ch, RUN_MIN)
    # ② 中英混排里夹着孤立大写字母（OCR 把偏旁认成了字母）
    odd = re.findall(r"(?<=[\u4e00-\u9fff])[A-Za-z](?=[\u4e00-\u9fff])", t)
    if len(odd) >= 2:
        return "夹了孤立字母 %s" % "".join(odd[:4])
    # ③ 生僻字扎堆：OCR 认不出来时会输出形近的怪字
    rare = [c for c in t if c in "肿潘薯甲驷昂喱"]
    if len(rare) >= 2:
        return "生僻/形近字 %s" % "".join(rare[:5])
    # ④ 标点异常：整条没有标点且很短，或全是标点
    if len(t) >= 4 and not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", t):
        return "没有实义字符"
    return ""


def main():
    cfg = load_config()
    sp = resolve_state_path(cfg)
    sd = os.path.dirname(sp) or "."
    sdb, hdb = os.path.join(sd, "screens.db"), os.path.join(sd, "history.db")

    if not os.path.exists(hdb):
        print("没有 history.db（还没记录过）")
        return 0

    c = sqlite3.connect(hdb)
    cols = [r[1] for r in c.execute("PRAGMA table_info(messages)")]
    print("history.db 的列:", cols)
    # ⚠️ 正文列叫 **`content`**，不叫 `text`（第一版写的就是 text，直接 OperationalError）。
    #    所以这里按实际列名取，别再猜。
    col = "content" if "content" in cols else "text"
    q = "SELECT session_key, %s FROM messages WHERE %s IS NOT NULL" % (col, col)
    rows = list(c.execute(q))
    c.close()

    print("\n消息总数 = %d" % len(rows))
    print("=" * 74)

    reasons = Counter()
    samples = {}
    for sk, text in rows:
        why = looks_garbled(text)
        if why:
            key = why.split("（")[0].split(" ")[0]
            reasons[key] += 1
            samples.setdefault(key, []).append((sk, text))
    bad = sum(reasons.values())
    print("\n== 可疑消息分类 ==  %d / %d（%.1f%%）"
          % (bad, len(rows), 100.0 * bad / max(1, len(rows))))
    for k, n in reasons.most_common():
        print("   %-22s %4d 条" % (k, n))
    if not reasons:
        print("   （没有可疑的）")

    for k, items in samples.items():
        print("\n-- 样例：%s --" % k)
        for sk, text in items[:4]:
            print("   [%s] %s" % ((sk or "")[:14], text[:60]))

    print("\n" + "=" * 74)
    print("== 会话名可疑的 ==")
    c = sqlite3.connect(sdb) if os.path.exists(sdb) else None
    names = []
    if c is not None:
        names = [r[0] for r in c.execute(
            "SELECT DISTINCT session_key FROM screens WHERE session_key IS NOT NULL")]
        c.close()
    for n in sorted(names):
        flags = []
        if len(n) <= 1:
            flags.append("只有一个字")
        if re.match(r"^\d+$", n):
            flags.append("纯数字")
        if re.search(r"^(口|0|O)", n):
            flags.append("开头是图形字")
        if (n or "").count("口") >= 2:
            flags.append("含成串 口")
        if flags:
            print("   %-34s %s" % (repr(n)[:34], "、".join(flags)))
    print("   （共 %d 个会话名，%d 个可疑）"
          % (len(names), sum(1 for n in names if looks_garbled(n))))

    print("\n" + "=" * 74)
    print("结论怎么看：")
    print("  · 「图形被认成字」= **OCR 固有**：emoji / 头像 / 图片里的字被当正文读了，")
    print("     这是像素后端的天花板（UIA 那边拿得到真实文本就没有这个问题）。")
    print("  · 「夹了孤立字母」「生僻字」= 也基本是 OCR 固有（形近字）。")
    print("  · **掉第一个字 / 名字被切一半** = 是我们的**几何**问题，不是 OCR ——")
    print("     那条已经修了（标题和正文要用同一个左边界）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
