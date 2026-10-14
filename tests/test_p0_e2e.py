# -*- coding: utf-8 -*-
"""P0 自动化验证：不依赖真实消息，端到端跑一遍核心链路。

覆盖：
  1. 配置加载
  2. 会话表与白名单（新会话默认关闭、白名单自动启用）
  3. 微信窗口定位
  4. 轻量读取（会话列表）与标题读取
  5. 全量读取（当前聊天消息）
  6. 增量识别（基线建立 / 无变化不误报 / 模拟新消息）
  7. 性能（轻量 vs 全量读取耗时）

用法：python tests/test_p0_e2e.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.config import load_config, resolve_state_path
from wechat_assistant.reader import WeChatReader
from wechat_assistant.sessions import MODE_OFF, MODE_ON, SessionRegistry
from wechat_assistant.uia_worker import UiaError, UiaWorker
from wechat_assistant.watcher import find_new_messages, to_keys

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def main():
    print("=" * 74)
    print("P0 端到端验证（只读，不发送任何消息）")
    print("=" * 74)

    # ---------- 1. 配置
    print("\n1. 配置加载")
    cfg = load_config()
    check("config.yaml 可读", True, cfg.path)
    check("控件类名已配置",
          bool(cfg.get("wechat.cls.session_cell")) and bool(cfg.get("wechat.node.chat_title")))
    state_path = resolve_state_path(cfg)
    print("     状态文件: %s" % state_path)

    # ---------- 2. 会话表与白名单
    print("\n2. 会话表与白名单")
    registry = SessionRegistry(state_path, bootstrap_whitelist=["__不存在的会话__"],
                               default_mode=MODE_OFF)
    check("默认模式是 off", registry.default_mode == MODE_OFF)
    if "__不存在的会话__" in registry.sessions:
        check("白名单会话被置为 on", registry.mode_of("__不存在的会话__") == MODE_ON)
    else:
        check("白名单会话被置为 on", False, "未写入会话表")
    # 清掉测试痕迹
    registry.sessions.pop("__不存在的会话__", None)
    check("未知会话默认被忽略", registry.mode_of("某个没配置过的会话") == MODE_OFF)

    # ---------- 3. 窗口定位
    print("\n3. 微信窗口定位")
    log = lambda m: None
    try:
        worker = UiaWorker(timeout=20.0, logger=log)
    except UiaError as e:
        check("UIA 初始化", False, str(e))
        return 1
    reader = WeChatReader(cfg, worker, logger=log)
    win = reader.find_window()
    check("找到微信主窗口", win is not None,
          "hwnd=%s pid=%s" % (hex(win["hwnd"]), win["pid"]) if win else "未找到")
    if not win:
        worker.close()
        return 1
    hwnd = win["hwnd"]

    # ---------- 4. 轻量读取
    print("\n4. 轻量读取")
    t0 = time.perf_counter()
    sessions = reader.read_sessions(hwnd)
    ms_sessions = (time.perf_counter() - t0) * 1000
    check("读到会话列表", len(sessions) > 0, "%d 个会话，耗时 %.1fms" % (len(sessions), ms_sessions))
    for s in sessions[:3]:
        print("       - %s | %s | %s" % (s.raw_title, s.preview[:20], s.time_text))

    t0 = time.perf_counter()
    title = reader.read_chat_title(hwnd)
    ms_title = (time.perf_counter() - t0) * 1000
    check("读到当前聊天标题", bool(title), "%r，耗时 %.1fms" % (title, ms_title))

    # ---------- 5. 全量读取
    print("\n5. 全量读取当前聊天")
    t0 = time.perf_counter()
    snap = reader.read_chat(hwnd)
    ms_full = (time.perf_counter() - t0) * 1000
    check("读到消息", snap is not None and len(snap.messages) > 0,
          "%d 条消息，耗时 %.1fms" % (len(snap.messages) if snap else 0, ms_full))
    check("标题一致", (snap.title == title) if snap else False, "%r" % (snap.title if snap else None))
    for m in (snap.messages if snap else [])[:4]:
        print("       - [%s] %s" % (m.kind, m.text[:34]))

    # ---------- 6. 增量识别
    print("\n6. 增量识别")
    cur = to_keys(snap.messages)
    check("相同输入不误报", find_new_messages(cur, cur) == [])
    check("追加一条能识别", find_new_messages(cur, cur + [(None, None, "测试消息")]) ==
          [(None, None, "测试消息")])
    check("基线为空时返回全部", find_new_messages([], cur) == cur)

    # ---------- 7. 性能
    print("\n7. 读取性能")
    check("轻量读取更快", ms_sessions < ms_full,
          "轻量 %.1fms < 全量 %.1fms" % (ms_sessions, ms_full))
    # 绝对耗时随「会话条数 / 微信当前状态 / 机器负载」明显波动：
    # 实测同一台机器上 46~73ms 都出现过（把 app 停掉也照样超过 30ms），
    # 所以这里只留一个宽松上限。真正的判据是上面那条「轻量必须快于全量」，
    # 它才反映「轻量读取只取会话列表、不读消息正文」这个设计意图。
    check("轻量读取不拖慢轮询 (< 150ms)", ms_sessions < 150, "%.1fms" % ms_sessions)

    worker.close()

    print("\n" + "=" * 74)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
