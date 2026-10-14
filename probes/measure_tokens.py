# -*- coding: utf-8 -*-
"""只读实测：一次生成到底花多少 token、多少钱。

不估算 —— 直接把当前会话的真实上下数组装出来，**真调一次 API**，
读它返回的 `usage` 字段（那是模型侧统计的准确值）。

成本按 DeepSeek 深色（deepseek-flash）价格算：
    输入  闲时 ¥1 / 百万 token，忙时 ¥2 / 百万
    输出  闲时 ¥4 / 百万 token，忙时 ¥8 / 百万
    命中缓存 输入只要 ¥0.02 / ¥0.04 每百万
（价格会变，以官网为准；这里只用来给个数量级）

⚠️ 会真实调用一次 API（约 ¥0.002）。不写库、不改任何状态。
用法： python probes/measure_tokens.py
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

# 每百万 token 的价格（闲时）
P_IN, P_OUT = 1.0, 4.0


def price(pin, pout):
    return pin / 1e6 * P_IN + pout / 1e6 * P_OUT


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    cfg = load_config()
    ai = AIClient(cfg)
    db = cfg.get("history.path", "state/history.db")
    if not os.path.isabs(db):
        db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), db)
    store = MessageStore(db, logger=lambda m: None)

    # ---- 复现真实的上下文组装（同 run_watch.py）
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
    SenderResolver(cfg, ocr, logger=lambda m: None).resolve(win["hwnd"], win["pid"], snap)
    title = reader.read_chat_title(win["hwnd"])
    worker.close()

    key = title
    visible = history_to_dicts(snap.messages)
    stored = [{"who": ("我" if r.get("is_self") == 1 else
                       (r.get("sender_name") or ("对方" if r.get("is_self") == 0 else "?"))),
               "text": r.get("content") or ""}
              for r in store.get_recent(key, int(cfg.get("reply.history_messages", 40)))]
    history = merge_with_store(visible, stored, ai.max_messages)

    persona = cfg.get("persona.default", "")
    relationship = cfg.get("persona.default_relationship", "")
    samples = [h["text"] for h in history if h.get("who") == "我"][-12:]

    # 拿最后一条当「待回复」
    current = history[-1]["text"] if history else "（没有内容）"
    hist_for_ai = history[:-1] if len(history) > 1 else []

    system, user = ai.build_prompts(persona, hist_for_ai, current, 3,
                                    style_samples=samples or None,
                                    relationship=relationship)

    print("=" * 74)
    print("提示词规模（当前会话 %r）" % title)
    print("=" * 74)
    print("  system : %5d 字" % len(system))
    print("  user   : %5d 字" % len(user))
    print("  合计   : %5d 字" % (len(system) + len(user)))
    print("  其中历史 %d 条 / 口吻样本 %d 条 / 人设 %d 字"
          % (len(hist_for_ai), len(samples), len(persona)))
    print()

    print("=" * 74)
    print("实际调用一次 API，看模型侧统计的准确用量")
    print("=" * 74)
    try:
        replies = ai.generate_replies(persona, hist_for_ai, current, n_replies=3,
                                      style_samples=samples or None,
                                      relationship=relationship)
    except Exception as e:
        print("  调用失败: %s: %s" % (type(e).__name__, e))
        store.close()
        return 1

    u = ai.last_usage
    print("  输入 prompt_tokens     : %d" % u["prompt_tokens"])
    print("  输出 completion_tokens : %d" % u["completion_tokens"])
    print("  合计 total_tokens      : %d" % u["total_tokens"])
    print()
    print("  --- 这一条消息回复的上下文构成（估算占比）---")
    n_hist_msg = len(hist_for_ai)
    print("  · 历史 %d 条          ≈ %d token（每条约 %d）"
          % (n_hist_msg, n_hist_msg * 8, 8))
    print("  · 固定开销（人设+反模板+判断规则+输出格式+口吻样本）")
    print("                        ≈ %d token  <- 这部分与聊多少无关"
          % max(0, u["prompt_tokens"] - n_hist_msg * 8 - 8))
    print()
    print("  --- 成本 ---")
    c1 = price(u["prompt_tokens"], u["completion_tokens"])
    print("  这一次            : ¥%.4f" % c1)
    print("  每天 20 次        : ¥%.3f   （每月约 ¥%.2f）" % (c1 * 20, c1 * 20 * 30))
    print("  每天 50 次        : ¥%.3f   （每月约 ¥%.2f）" % (c1 * 50, c1 * 50 * 30))
    print("  每天 200 次       : ¥%.2f   （每月约 ¥%.2f）" % (c1 * 200, c1 * 200 * 30))
    print()
    print("  参考：历史从 20 条加到 50 条，输入只多约 %d token ≈ ¥%.4f/次"
          % (30 * 8, price(30 * 8, 0)))
    print()
    print("  生成的 3 条建议：")
    for i, r in enumerate(replies, 1):
        print("    %d. %s" % (i, r))

    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
