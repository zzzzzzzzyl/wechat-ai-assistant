# -*- coding: utf-8 -*-
"""文本噪声判据：「这段文字值不值得当**口吻样本**」。

⚠️ 零依赖（不 import 本项目任何东西）—— 因为 `store.py`（从库里取）
和 `ai.py`（从当前屏取）**两条路都要用**，放任何一边都会造成重复或循环。

它**不是**"这段文字是不是垃圾消息"的通用判据（那个做不到，见下），
只回答一个窄问题：**拿它去教模型"我平时怎么说话"，会不会把模型带坏。**
"""

#: OCR 把界面/卡片上的字读成正文时最常见的词（2026-10-12 用户实测贴出来的那批）。
#: ⚠️ 白名单式，必然漏 —— 它的作用是**兜住最常见的几个**，不是全覆盖。
UI_WORDS = (
    "小程序", "职位详情", "活动规则", "可领取", "优惠券", "优惠卷",
    "代金", "发送", "返回", "朋友圈", "公众号", "扫一扫", "收付款",
    "微信电脑版", "文件传输助手", "查看详情", "立即领取",
)

#: 汉字占比低于这个数就认为是碎片（`社路79代比10代金势 21元` 这种）。
MIN_HAN_RATIO = 0.4

#: 短于这个长度**不判**（一个字的样本没信息量，但也不该算噪声去报日志）。
MIN_LEN = 1


def han_ratio(s: str) -> float:
    s = s or ""
    if not s:
        return 0.0
    return sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff") / float(len(s))


def is_noise_text(text: str) -> bool:
    """这段文字是不是**明显不该当口吻样本**。

    ⚠️ 判据刻意**保守**（宁可放过，也不误杀）：
       误杀一条真样本 = 少一点风格信号（无所谓）；
       放过一条 `KI`   = **AI 开始模仿它**（很糟）。
       所以这里的取舍是**偏向多丢**的 —— 这一点和"消息正文一条都不能丢"**正好相反**，
       别把这个函数拿去过滤正文。
    """
    s = (text or "").strip()
    if not s:
        return True
    # ① 一个汉字都没有
    if han_ratio(s) <= 0.0:
        return True
    # ② 界面/卡片关键词
    for w in UI_WORDS:
        if w in s:
            return True
    # ③ 汉字占比过低（碎片）
    #    ⚠️ 门槛是 **3**（不是 4）—— 实测漏过一个 `21元`（优惠券碎片，3 个字，
    #       汉字占比 1/3）。代价是 `OK啦` 这类也会被丢：**可接受**
    #       （样本少一条无所谓，这函数的取舍本来就更偏向多丢）。
    if len(s) >= 3 and han_ratio(s) < MIN_HAN_RATIO:
        return True
    return False


def clean_style_samples(texts):
    """把一串候选口吻样本过一遍筛子（保持原顺序、去重）。"""
    out = []
    for t in texts or []:
        s = (t or "").strip()
        if not s or s in out:
            continue
        if is_noise_text(s):
            continue
        out.append(s)
    return out
