# -*- coding: utf-8 -*-
"""只读验证：到底有没有「正确存储并思考 20 条对话」。

怀疑是合理的，所以这个脚本不复述结论，而是**复现真实的组装路径**
（run_watch.py:307-317 那几行），把最终真正发给 AI 的 prompt 原文打出来。

它依次验证：
    1. 库里到底存了多少条、内容对不对、顺序对不对
    2. 窗口里可见多少条、发送者判定对不对
    3. 两者合并后实际取了多少条（是不是 20）
    4. 合并结果有没有**重复**（重叠点没匹配上就会重复）
    5. 最终 user prompt 的原文和 token 量

⚠️ 只读：不写库、不调 AI（只组装 prompt 不发请求）、不碰微信内容之外的任何东西。
用法： python probes/verify_context.py [会话名]
"""

import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.ai import AIClient  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.context import (history_to_dicts,  # noqa: E402
                                      merge_with_store)
from wechat_assistant.reader import WeChatReader  # noqa: E402
from wechat_assistant.sender import SenderResolver  # noqa: E402
from wechat_assistant.store import MessageStore  # noqa: E402
from wechat_assistant.uia_worker import UiaWorker  # noqa: E402


def section(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def estimate_tokens(s: str) -> int:
    """粗估 token：中文约 1 字 ≈ 1 token，英文按 4 字符 ≈ 1 token。"""
    cn = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    other = len(s) - cn
    return int(cn + other / 4)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    cfg = load_config()
    ai = AIClient(cfg)
    store = MessageStore(cfg.get("history.path", "state/history.db"),
                         int(cfg.get("history.max_per_session", 500)))

    max_ctx = ai.max_messages
    hist_limit = int(cfg.get("reply.history_messages", 40))
    print("配置：runtime.max_messages = %d（最终喂给 AI 的条数上限）" % max_ctx)
    print("      reply.history_messages = %d（从库里最多取多少条当候选）" % hist_limit)

    # ================================================ 1. 库里存了什么
    section("1. 历史库里实际存了什么")
    stats = store.stats()
    if not stats:
        print("  库是空的 —— 先去微信里按几次 Ctrl+Alt+Q")
        return 1
    for s in stats:
        print("  %-22s %4d 条   最后写入 %s"
              % (s["session_key"], s["n"], s["last_at"]))
    sess = stats[0]["session_key"]

    rows = store.get_recent(sess, hist_limit)
    print("\n  【%s】库里最近 %d 条（按时间正序）：" % (sess, len(rows)))
    for i, r in enumerate(rows, 1):
        who = "我" if r["is_self"] == 1 else ("对方" if r["is_self"] == 0 else "?")
        print("   %3d. [%-3s] %r" % (i, who, (r["content"] or "")[:44]))

    # ================================================ 2. 窗口可见
    section("2. 窗口里现在可见什么（本次触发的另一路来源）")
    worker = UiaWorker(20.0)
    reader = WeChatReader(cfg, worker)
    win = reader.find_window()
    snap = reader.read_chat(win["hwnd"])
    ocr = None
    if cfg.get("sender.enabled", True):
        try:
            from wechat_assistant.ocr import get_engine
            ocr = get_engine(cfg.get("ocr"))
        except Exception:
            pass
    sender = SenderResolver(cfg, ocr, logger=lambda m: None)
    n_res = sender.resolve(win["hwnd"], win["pid"], snap)
    print("  当前会话 %r，窗口可见 %d 条，发送者判定成功 %d 条"
          % (snap.title, len(snap.messages), n_res))
    for i, m in enumerate(snap.messages, 1):
        print("   %3d. [%-3s] %r" % (i, m.who, (m.text or "")[:44]))
    worker.close()

    # ================================================ 3. 复现真实组装
    section("3. 复现 run_watch.py:307-317 的组装")

    # —— 以下三行与 run_watch.py 完全一致 ——
    visible_history = history_to_dicts(snap.messages)
    stored = [{"who": ("我" if r.get("is_self") == 1 else
                       (r.get("sender_name") or ("对方" if r.get("is_self") == 0 else "?"))),
               "text": r.get("content") or ""}
              for r in store.get_recent(sess, hist_limit)]
    history = merge_with_store(visible_history, stored, max_ctx)

    print("  窗口可见 -> %d 条" % len(visible_history))
    print("  库里取出 -> %d 条" % len(stored))
    print("  合并后取最近 %d 条 -> 实际 %d 条" % (max_ctx, len(history)))

    # ================================================ 4. 检查重复
    section("4. ⚠️ 合并结果有没有重复（重叠点没匹配上就会重复）")
    seen = {}
    dupes = []
    for i, h in enumerate(history):
        k = (h.get("who"), h.get("text"))
        if k in seen:
            dupes.append((seen[k], i, h))
        else:
            seen[k] = i
    if dupes:
        print("  发现 %d 处重复：" % len(dupes))
        for a, b, h in dupes:
            print("    第 %d 条与第 %d 条相同: [%s] %r" % (a + 1, b + 1, h.get("who"),
                                                          (h.get("text") or "")[:36]))
        print("  → 说明窗口和库的 who 标注不一致，重叠点没匹配上")
        who_win = set(h.get("who") for h in visible_history)
        who_db = set(h.get("who") for h in stored)
        print("    窗口里的 who 取值: %s" % who_win)
        print("    库里的   who 取值: %s" % who_db)
    else:
        print("  没有重复 ✓")

    # ================================================ 5. 最终 prompt
    section("5. 最终真正发给 AI 的 prompt（原文照打）")
    persona = cfg.get("persona.default", "")
    relationship = cfg.get("persona.default_relationship", "")
    samples = [h["text"] for h in history if h.get("who") == "我"][-12:]
    system, user = ai.build_prompts(persona, history, "（这里放本次要回复的内容）",
                                    3, style_samples=samples or None,
                                    relationship=relationship)
    print("------------ system (%d 字, 约 %d token) ------------"
          % (len(system), estimate_tokens(system)))
    print(system)
    print("\n------------ user (%d 字, 约 %d token) ------------"
          % (len(user), estimate_tokens(user)))
    print(user)

    total = estimate_tokens(system) + estimate_tokens(user)
    print("\n------------ 合计约 %d token ------------" % total)

    # ================================================ 6. 结论
    section("6. 结论")
    n_lines = sum(1 for ln in user.splitlines() if ": " in ln)
    print("  · 历史库：%d 条（本次实际取出 %d 条参与组装）" % (stats[0]["n"], len(rows)))
    print("  · 合并后进入 prompt 的历史：**%d 条**（上限 %d）" % (len(history), max_ctx))
    print("  · prompt 里历史部分的实际行数：约 %d 行" % n_lines)
    print("  · 是否达到配置的 %d 条：%s" % (max_ctx, "是" if len(history) >= max_ctx else
                                     "否（可见+库里的总数不够 %d 条）" % max_ctx))
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
