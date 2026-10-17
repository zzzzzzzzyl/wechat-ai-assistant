# -*- coding: utf-8 -*-
"""只读查询本地库：`python tools\\db_query.py state/history.db "select ..."`

⚠️ 只读打开（`mode=ro`），绝不写库 —— 程序可能正开着。
"""
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path = sys.argv[1]
    if not os.path.isabs(path):
        path = os.path.join(ROOT, path)
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    cur = con.cursor()
    for sql in sys.argv[2:]:
        print("SQL> %s" % sql)
        try:
            cur.execute(sql)
        except Exception as e:
            print("  ❌ %s: %s" % (type(e).__name__, e))
            continue
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchall()
        print("  cols:", ", ".join(cols), "| rows:", len(rows))
        for r in rows:
            print("  ·", " | ".join("" if v is None else str(v)[:120] for v in r))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
