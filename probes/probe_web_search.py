"""探针：联网查证（两段式）—— 判断要不要查 + 真调 Search API（2026-11-09）。

⚠️ 只作取证用：会**真花钱**（搜索 ¥5/1000 次、判断调用几百 token）。
   产品代码走的是同一条路：`AIClient.plan_search()` → `AIClient.fetch_web()`。

⚠️ 为什么不是"服务端联网工具"（`tools: [deepseek_web_search]`）：
    实测在「写微信回复」的任务里**基本不搜**（提示词写"可以"→0 次；写"必须"→仍 0 次
    且会诱发编造；开思考才真搜，但入 11040 token）。详见 `设置项怎么改.md` §4d。

跑法：
    python probes\\probe_web_search.py            # 三句：天气 / 闲聊 / 表情包
    python probes\\probe_web_search.py 杭州明天天气
"""

import sys

sys.path.insert(0, __file__.rsplit("\\", 2)[0] or ".")

from wechat_assistant.ai import AIClient
from wechat_assistant.config import load_config

CASES = ["明天天气怎么样", "你吃了吗", "帮我看看这个表情包啥意思"]


class _Cfg:
    """把真配置包一层，只为在探针里临时打开 `ai.web_search`。"""

    def __init__(self, base):
        self._base = base

    def get(self, key, default=None):
        if key == "ai.web_search":
            return True
        return self._base.get(key, default)


def main() -> int:
    cfg = load_config()
    if not cfg.get("ai.api_key") or not cfg.get("ai.base_url"):
        print("还没配置接口地址 / API Key（去设置里填）")
        return 1
    c = AIClient(_Cfg(cfg), logger=print)
    print("接口 %s · 模型 %s · web_search=%s" % (c.base_url, c.model, c.web_search))

    cases = [sys.argv[1]] if len(sys.argv) > 1 else CASES
    for cur in cases:
        q = c.plan_search(cur, "")
        print("\n【%s】判断 -> %r" % (cur, q))
        if not q:
            print("  不用查（这条路走通了：是模型自己判断的）")
            continue
        blk = c.fetch_web(q)
        print("  %d 条 / %d 字 · last_web_calls=%d"
              % (blk.count("- "), len(blk), c.last_web_calls))
        for line in blk.splitlines()[1:4]:
            print("  ", line[:100])
    return 0


if __name__ == "__main__":
    sys.exit(main())
