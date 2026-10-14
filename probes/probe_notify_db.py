# -*- coding: utf-8 -*-
"""
验证：微信 PC 版是否推送 Windows 系统 Toast 通知？

为什么要先验证这个：
    需求 2.1 计划用 WinRT UserNotificationListener 监听系统通知。
    但该 API **只能拿到走 Windows 通知中心（Toast）的通知**。
    如果微信是自己画一个悬浮提示窗口（很多国产 IM 都这么做），
    那 UserNotificationListener 永远收不到任何东西，方案要整个换掉。

做法：
    Windows 通知中心把所有 Toast 存在 SQLite 数据库里：
        %LOCALAPPDATA%\\Microsoft\\Windows\\Notifications\\wpndatabase.db
    把它复制出来读（原文件被系统占用，不能直接打开），
    列出所有注册的通知来源（AUMID）和历史通知内容，看有没有微信。

只读：仅复制并读取该数据库文件。
"""

import os
import shutil
import sqlite3
import sys
import datetime as dt

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    src = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft", "Windows", "Notifications", "wpndatabase.db",
    )
    print("=" * 82)
    print("微信是否使用 Windows 系统 Toast 通知 —— 通知数据库检查")
    print("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 82)
    print("数据库: %s" % src)
    if not os.path.exists(src):
        print("!! 数据库不存在，无法判断")
        return
    st = os.stat(src)
    print("大小: %d 字节   最后写入: %s" % (
        st.st_size, dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")))

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    os.makedirs(outdir, exist_ok=True)
    dst = os.path.join(outdir, "wpndatabase_copy.db")
    try:
        shutil.copy2(src, dst)
    except Exception as e:
        print("!! 复制失败（文件被占用）: %s" % e)
        return
    print("已复制到: %s" % dst)

    con = sqlite3.connect(dst)
    cur = con.cursor()

    print("")
    print("-" * 82)
    print("【表结构】")
    print("-" * 82)
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    print("表: %s" % tables)

    print("")
    print("-" * 82)
    print("【所有注册的通知来源 AppUserModelId (NotificationHandler 表)】")
    print("-" * 82)
    handlers = {}
    try:
        cur.execute("PRAGMA table_info(NotificationHandler)")
        cols = [c[1] for c in cur.fetchall()]
        print("字段: %s" % cols)
        cur.execute("SELECT * FROM NotificationHandler")
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            aid = d.get("PrimaryId") or ""
            handlers[d.get("RecordId")] = aid
            nm = d.get("DisplayName") or ""
            print("  RecordId=%-4s AUMID=%r  DisplayName=%r" % (d.get("RecordId"), aid, nm))
    except Exception as e:
        print("读取失败: %s" % e)

    print("")
    print("-" * 82)
    print("【历史通知记录（按时间倒序，最多 40 条）】")
    print("-" * 82)
    try:
        cur.execute("PRAGMA table_info(Notification)")
        ncols = [c[1] for c in cur.fetchall()]
        print("Notification 字段: %s" % ncols)
        order = "ArrivalTime" if "ArrivalTime" in ncols else ("Id" if "Id" in ncols else ncols[0])
        cur.execute("SELECT * FROM Notification ORDER BY %s DESC LIMIT 40" % order)
        rows = cur.fetchall()
        if not rows:
            print("  （通知表为空）")
        for row in rows:
            d = dict(zip(ncols, row))
            note = handlers.get(d.get("HandlerId"), "?")
            t = d.get("ArrivalTime")
            ts = ""
            if t:
                try:
                    ts = (dt.datetime(1601, 1, 1) + dt.timedelta(microseconds=int(t) / 10)).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    ts = str(t)
            payload = d.get("Payload")
            pv = ""
            if payload:
                raw = payload if isinstance(payload, bytes) else str(payload).encode("utf-8", "ignore")
                # Toast payload 实际是 UTF-8 的 XML；先试 utf-8 再退 utf-16
                for enc in ("utf-8", "utf-16-le", "utf-16-be", "latin-1"):
                    try:
                        head = raw.decode(enc)
                        if head.count("<") >= 2:
                            break
                    except Exception:
                        continue
                pv = " ".join(head.split())[:160]
            print("  [%s] AUMID=%s" % (ts, note))
            if pv:
                print("        %s" % pv)
    except Exception as e:
        print("读取 Notification 失败: %s" % e)

    print("")
    print("-" * 82)
    print("【结论判读】")
    print("-" * 82)
    aumids = " ".join(str(v).lower() for v in handlers.values())
    has_wx = any(k in aumids for k in ("wechat", "weixin", "tencent"))
    print("  通知来源里是否出现微信/腾讯相关 AUMID: %s" % ("是" if has_wx else "否"))
    print("  若为『否』 => 微信没有走 Windows 通知中心，")
    print("     那么 WinRT UserNotificationListener 方案（需求 2.1）拿不到任何东西，必须换方案。")
    con.close()


if __name__ == "__main__":
    main()
