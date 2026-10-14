# -*- coding: utf-8 -*-
"""配色与字号 —— 让悬浮窗看起来像微信的一部分，而不是另一个软件。

色值是哪来的
------------
**两套色板都是实测的，不是凭记忆填的。** 做法：把微信切到对应外观，
`probes/probe_theme.py` 用 `capture.PrintWindow` 抓微信窗口的位图
（**纯只读**：让微信自己渲染成位图，不置顶、不激活、不移动、不发输入），
再按区域统计颜色直方图。

2026-09-26 同一次实测的结果：

    深色（整窗亮度 51.7）
        聊天背景  #1E1E1F   （聊天区 87.6%）
        对方气泡  #2F2F30   （31.1%）
        标题栏    #2A2A2C   （33.2%）★ 原来记的是 #2D2D2E，偏亮两档
        深一档的绿 #0DA869   （标题栏按钮/图标上的绿，5.9~7.3%）
        描边      #3A3A3B

    浅色（整窗亮度 227.9）
        聊天背景  #FAFAFA   （聊天区 75.6%）★ 原来记的是 #EDEDED，灰得多
        会话列表  #EEEEF0                          ← 2026-09-27 起用作 `surface_alt`
        标题栏    #DCDCE1   （98.5%）★⚠️ **已弃用**，见下
        描边      #D3D3D3
        品牌绿    #01C260（1.2%）/ #15AC70（5.8~7.1%）

    ⚠️ **2026-09-27 修正：浅色的那几档灰太深了。**
    用户实测反馈「浅色模式下的灰色太深了，并不是微信配色」。
    原来是拿上面的"标题栏 `#DCDCE1`（98.5%）"当 `surface_alt`，于是设置左栏、
    标题栏、输入框、状态条全是那个偏深的灰。现在改用**同一次实测里也量到的**
    `会话列表 #EEEEF0`（亮度 239.4 vs 220.6），`hover` / `border` / `shell_border`
    跟着一起调。

    ⚠️ 为什么这次没直接重测：`probe_theme.py` 把抓图**固定存成
    `out/theme_probe.png`**，后量的覆盖先量的 —— 浅色那张已经没了，
    只剩深色那张，"标题栏区域当时框到了哪儿"**没法回查**。
    探针已改成按模式分别落盘（`probes/qt/measure_wechat_light.py`），
    **浅色这几档仍是待复核状态**：微信切到浅色后跑那个探针按实测校准。

⚠️ **两条实测结论改变了设计**（不是"填个色号"而已）：
  1. 浅色下"聊天背景"和"对方气泡"**是同一个色**（都是 #FAFAFA）。
     微信靠头像和间距区分气泡；我们的三条建议卡片并排、没有头像，
     同色就完全看不出是一张卡 —— 所以卡片改用**白底 + `card_line` 描边**，
     这是能读出来的最接近等价物。为此新增了 `card_line` 这个语义键。
  2. 两套里的"品牌绿"占比都不高（1.2% / 7.3%），**微信自己用绿很克制**：
     大面积都是灰阶，绿只出现在强调点上。所以想"更像微信"，
     方向是**减少绿的用量**，不是增加。

主题怎么决定（三级，见 `detect()`）
-----------------------------------
    ① `ui.theme` 显式指定      用户钉死，最高优先级
    ② 采样微信窗口的实际渲染    最准：微信设成什么就是什么
    ③ 系统注册表               微信最小化（抓图空白）时的兜底

为什么"跟随系统"不够
--------------------
微信的**外观是独立设置**的（设置 → 通用 → 外观 → 跟随系统/浅色/深色）。
如果系统是深色、微信手动设成浅色，只跟随系统就会让助手面板是黑的、微信是白的，
并排摆着很难看。② 直接看微信渲染出来是什么颜色，绕开这个不一致。
"""

from typing import Dict, Optional

# 主题名
DARK = "dark"
LIGHT = "light"
AUTO = "auto"          # 跟随（采样微信 -> 系统）

# ---------------------------------------------------------------- 色板
#
# 键的语义（做界面时只认这些键，不要直接写死色号）：
#   bg          主背景（面板底色）
#   surface     卡片 / 气泡 / 列表项
#   surface_alt 次级面板：标题栏、输入框、状态条
#   border      分割线
#   text        主文字
#   text_dim    次要文字（说明、状态）
#   text_faint  最弱：占位符、序号线
#   accent      品牌绿：主按钮、强调
#   accent_text 绿底上的文字
#   accent_dim  绿色的弱化版（悬停 / 描边）
#   hover       悬停底色
#   danger      错误

DARK_PALETTE: Dict[str, str] = {
    # 实测：右侧聊天区 87.6%
    "bg":          "#1E1E1F",
    # 实测：对方气泡 31.1%
    "surface":     "#2F2F30",
    # 实测：顶部标题栏（原来写 #2D2D2E，实测是 #2A2A2C —— 偏亮了两档，
    #       深色下这点差别肉眼能看出标题栏"浮"起来一块）
    "surface_alt": "#2A2A2C",
    # 实测：出现在描边位置
    "border":      "#3A3A3B",
    "text":        "#E1E1E5",   # 实测：气泡上的文字色
    "text_dim":    "#979799",   # 实测：微信深色的次要文字
    "text_faint":  "#6B6B6E",
    # ⚠️ `accent` 用**品牌绿** #01C260（用户要的"微信绿"）。
    #    实测里占比更大的是 #0DA869（5.9~7.3%），那是深色模式下
    #    标题栏按钮/图标上的**深一档**的绿，归到 `accent_dim`。
    "accent":      "#01C260",
    "accent_text": "#FFFFFF",
    "accent_dim":  "#0DA869",
    "hover":       "#3A3A3B",
    "danger":      "#FA5151",   # 实测：微信的红点色
    # 无边框窗口的**外框线**。跟 `border`（界面里的分隔线）分开一个键 ——
    # 两者要求相反：分隔线要"几乎看不见"，外框线要"能看清窗口边界"。
    "shell_border": "#3A3A3B",
    # ⚠️ 卡片的描边单独一个键。
    #    浅色下实测"背景 #FAFAFA / 对方气泡 #FAFAFA"**同色** —— 微信是靠
    #    头像和间距区分气泡的，而我们这里三条建议卡片并排、没有头像，
    #    卡片跟背景同色就**完全看不出是一张卡**。所以浅色下卡片用白底
    #    (`surface`) + 这条更深的描边把边界勾出来。
    #    深色下 `surface`(#2F2F30) 本来就比 `bg`(#1E1E1F) 亮，
    #    描边用同一档就行，不需要额外对比。
    "card_line":   "#2F2F30",
    # ============== 开关（胶囊）的两个键（2026-10-14 新增）==============
    # ⚠️⚠️ 为什么会单独有这两个键：用户报「胶囊开关在浅色时显示不明显」。
    #    原来关态的**轨道**填的是 `surface`、**滑块**写死 `#FFFFFF` ——
    #    浅色下 `surface` 就是 `#FFFFFF`，于是**白底上的白滑块**，
    #    整个开关只剩 1px 边框（`border` #E2E2E6，比轨道只深一丁点）能看见。
    #    ⚠️ 这两个键**必须成对看**：轨道要和"页面底"分得开，滑块要和"轨道"分得开
    #    （`probes/qt/audit_light_palette.py` 第 5 节按这两条量）。
    #    深色下原来那套本来就看得清（轨道 #2F2F30 压在 #1E1E1F 上、滑块纯白），
    #    所以这里只是把**已经生效的值**写成语义键，行为不变。
    "switch_off":  "#2F2F30",
    "switch_knob": "#FFFFFF",
    # ============== 选中文字的高亮（2026-09-27 新增）==============
    # ⚠️ 加这两个键的原因：用户报「字体高亮并不是微信设置的颜色」。
    #    实测确认：我们**从来没设过** `selection-background-color`，
    #    于是 Qt 用了**系统默认的蓝**（实测渲染出来是 `#308CC6`）——
    #    深色浅色两套下都是那个蓝，一眼就不是微信。
    #    → 见 `probes/qt/check_selection_color.py`（真的选中一段文字再量像素）。
    # ⚠️ 现在是**按色板推的**，不是从微信实测的：
    #    微信输入框里选中文字到底是什么色，得让用户先在微信输入框里选中一段
    #    再抓图才能测（不能自己模拟键鼠去选 —— 破红线）。
    #    这里先用"accent 的一档浓色"，因为「微信绿」是这个界面唯一的高亮语言。
    #    实测校准之前先按这个走（见 CLAUDELOG 的遗留条目）。
    # ⚠️ 0.55 这个混色比例是**按可读性定的**：白字压上去约 4.98:1。
    #    直接用 `accent`(#01C260) 当高亮的话白字只有 2.38:1，太糊。
    "sel_bg":      "#16804A",
    "sel_fg":      "#FFFFFF",
    # ========== 设置窗口左侧导航的**选中项**（2026-09-27 按微信实测）==========
    # 实测（`probes/qt/analyze_settings_nav.py`，微信设置窗口 655x567）：
    #   选中项底色 **#0DA869**（占该块 93.3% —— 正好就是 `accent_dim`）
    #   选中项文字 **#E7F6F0**（近白带一点绿）
    #   形状 240x60 物理像素的**直角矩形**，铺满导航栏宽度
    # ⚠️ 底色直接复用 `accent_dim`，不新增键 —— 实测值跟它一模一样，
    #    多一个键就等于同一个色写两处，迟早漂开。
    "nav_sel_fg":  "#E7F6F0",
    # 设置窗口左侧导航栏的**底色**（2026-09-27 实测）。
    # ⚠️ 深色是 `#2F2F30`（正好等于 `surface`），浅色是 `#EEEEF0`
    #    （正好等于 `surface_alt`）—— **两套映射到不同的键**，
    #    所以这里必须单独给一个语义键，不能直接借用 `surface`：
    #    浅色下用 `surface`(#FFFFFF) 会让导航栏是白的、跟右栏分不开。
    "nav_bg":      "#2F2F30",
}

# ⚠️ 浅色这套是 **2026-09-26 实测**的（`probes/probe_theme.py`，微信切到浅色后采样）。
#    在此之前它是"按微信标准色凭记忆填的"，实测证明**整体偏灰**：
#    聊天区背景记忆值是 #EDEDED，实测是 **#FAFAFA**（亮一档）；
#    对方气泡记忆值是纯白，实测也是 #FAFAFA（和背景同色，靠**分隔感**而不是色差区分）。
LIGHT_PALETTE: Dict[str, str] = {
    # 实测：右侧聊天区 75.6% 都是它
    "bg":          "#FAFAFA",
    # ⚠️ 卡片用**纯白**而不是实测的那个 #FAFAFA。
    #    实测"背景和对方气泡同色"，微信靠头像+间距区分气泡；我们的三条建议
    #    卡片并排、没有头像，同色就完全看不出是一张卡。
    #    白底 + `card_line` 描边是这里能读得出来的最接近等价物。
    "surface":     "#FFFFFF",
    # ===================== 2026-09-27 修正：这一档原来太深 =====================
    # 原来是 `#DCDCE1`（亮度 220.6），依据是"顶部标题栏 98.5% 都是它"。
    # **用户实测反馈「浅色模式下的灰色太深了，并不是微信配色」** —— 那个灰铺在
    # 设置左栏、标题栏、输入框、状态条上，是浅色下**面积最大的一块灰**。
    #
    # ⚠️ 为什么现在改用 `#EEEEF0`，而不是"再量一遍"：
    #    同一次实测里**也量到了左侧会话列表 = `#EEEEF0`**（亮度 239.4）。
    #    也就是同一次采样给出了两个候选，当时取了深的那个当标题栏。
    #    而"浅色下标题栏比会话列表深一大截"本身就不像微信 —— 浅色下这两块
    #    是同一个亮度的灰。现在取**实测到的那个浅的**。
    # ⚠️ 证据为什么没法复核：`probe_theme.py` 把抓图**固定存成
    #    `out/theme_probe.png`**，先量的深色被后量的浅色覆盖 —— 手上只剩深色那张，
    #    浅色那次的"标题栏区域"到底框到了哪儿，**已经没法回查**。
    #    → 探针已改：`probes/qt/measure_wechat_light.py` **按模式分别落盘**。
    #    → 这一档**待复核**：等微信切到浅色，跑那个探针按实测校准
    #      （见 CLAUDELOG 的遗留条目）。现在这版是按线索调的，不是拍脑袋。
    "surface_alt": "#EEEEF0",
    # 分隔线：细线，比它铺在的底再深一档。
    # ⚠️ 原来是 `#D3D3D3`(211.0)，在新底色 `#EEEEF0`(239.4) 上是一条明显的深灰线；
    #    浅色下的分隔线应该"几乎看不见但看得出分界"。改成 226.7。
    "border":      "#E2E2E6",
    "text":        "#191919",
    "text_dim":    "#888888",
    "text_faint":  "#B2B2B2",
    # 实测：微信品牌绿（#01C260 也出现在采样里，但占比 1.2%；
    # #15AC70 占 5.8~7.1%，是浅色模式下实际用得更多的那个）
    "accent":      "#07C160",
    "accent_text": "#FFFFFF",
    "accent_dim":  "#15AC70",
    # ⚠️ `hover` 必须跟着 `surface_alt` 一起改，**不能只改上面那个**。
    #    原来是 `#F0F0F0`(240.0)，而新的侧栏底色是 `#EEEEF0`(239.4) ——
    #    两者**只差 0.6**，悬停会完全看不出来（改浅的连带影响，容易漏）。
    #    浅色下让悬停**稍微变深**（微信的列表悬停就是变暗一点）：228.6，
    #    跟 `#FAFAFA`(250) 差 21.4、跟 `#EEEEF0`(239.4) 差 10.8，两种底上都看得见。
    "hover":       "#E4E4E9",
    "danger":      "#FA5151",
    # ⚠️ 外框线要比 `border` 深一档 —— 不然和无边框窗口自己的圆角/描边一样浅，
    #    等于没有边框，面板浮在浅色桌面上会"没有边"。
    #    原来是 `#CFCFD4`(207.6)，太深（浅色下像一条深灰硬边）；改成 216.6，
    #    仍明确比 `border`(226.7) 和侧栏(239.4) 深。
    "shell_border": "#D8D8DC",
    # 卡片描边：⚠️ **这个不要再调浅了**。
    #    浅色下卡片是"白底铺在 #FAFAFA 上"，两者只差 5，**全靠这条描边才看得出
    #    是一张卡**（见上面 `surface` 那段）。调浅 = 卡片消失。
    #    实测 #DCDCE1 / #D3D3D3 都出现在"描边"位置，取中间偏浅的。
    "card_line":   "#E3E3E8",
    # ============== 开关（胶囊）的两个键（2026-10-14 新增）==============
    # ⚠️⚠️ 用户报「胶囊开关在浅色时显示不明显」：原来关态的轨道填 `surface`
    #    = `#FFFFFF`，滑块又写死 `#FFFFFF` —— **白底白滑块**，等于没有开关。
    #    浅色下轨道必须是一档**灰**（借 `hover` 那一档，微信的列表悬停也是它）：
    #    滑块(255) - 轨道(228.6) = 26.4，压在正文底 `#FAFAFA` (250) 上也还差 21。
    #    深色那套本来就看得出（轨道 30.6 压在底 30.0 上、滑块纯白），保持原值。
    "switch_off":  "#E4E4E9",
    "switch_knob": "#FFFFFF",
    # ============== 选中文字的高亮（2026-09-27 新增）==============
    # 跟深色那套同一个理由：原来没设，Qt 用了系统默认的蓝（`#308CC6`）。
    # 浅色下用"白 + 一点点绿"(`#B5ECCF`，跟 `selected` 变体同一个色)，
    # 字用近黑 —— 实测对比度约 13.3:1，非常清楚，而且一眼是绿的。
    # ⚠️ 同样**待实测校准**（见深色那段的说明与 CLAUDELOG）。
    "sel_bg":      "#B5ECCF",
    "sel_fg":      "#191919",
    # 设置窗口左侧导航选中项的文字。
    # ⚠️ 2026-09-27 **实测**（微信切到浅色后量的设置窗口）：
    #    浅色下选中项文字是 `#E8F7F1` —— 跟深色的 `#E7F6F0` 基本是同一个色
    #    （都是"近白带一点绿"）。之前按纯白猜的那版也八九不离十，
    #    但既然是量得到的，就用实测值。
    "nav_sel_fg":  "#E8F7F1",
    # 设置窗口左侧导航栏底色：**浅色实测 `#EEEEF0`**。
    # ⚠️ 注意它跟深色**不是同一个语义键**：深色是 `#2F2F30`(= `surface`)，
    #    浅色是 `#EEEEF0`(= `surface_alt`)。所以导航栏**必须用 `nav_bg`**，
    #    直接借 `surface` 的话浅色下导航栏会变成白的、跟右栏糊在一起。
    "nav_bg":      "#EEEEF0",
}

PALETTES = {DARK: DARK_PALETTE, LIGHT: LIGHT_PALETTE}

# 无边框窗口的默认圆角半径（基准像素，按 DPI 缩放）。
# 面板读配置里的 `ui.radius`，设置窗口用这个常量 —— 两边的**默认值要一致**，
# 否则并排看是两个不同圆度。
#
# ⚠️ 曾经为了「圆角更圆润」提到 14，用户看过之后要求**退回 10**。
#    别再单方面调大。
SHELL_RADIUS = 10


# ---------------------------------------------------------------- 设计档位
# ⚠️ 这两张表是 2026-09-27 新增的，目的是**把散落的值收成常量**。
#    动因（用户提的）：
#      「全局统一边距常量，全部复用，禁止写死随机 margin/padding」
#      「统一圆角档位：窗口 10px，按钮/输入框/气泡使用偏小圆角，
#        各处圆角数值不要混乱」
#    改之前实测（`probes/qt/audit_spacing.py`）：
#      `setContentsMargins` **18 种组合 / 34 处**，`setSpacing` **7 种**，
#      圆角 3 / 8 / 25(胶囊) + 窗口 10 **混用**。
#
# ⚠️ **新增界面代码时从这里取值，不要再写 `px(13)` 这种数**。
#    单位都是**基准像素**（96dpi 下的逻辑像素），取值时经 `Style.px()` 缩放。

#: 圆角档位。**只有这四档**，不要再引入第五个数字。
RADIUS: Dict[str, int] = {
    # 无边框窗口的外壳。跟 `SHELL_RADIUS` 同一个值 —— 窗口只有一种圆度。
    "window":   10,
    # 按钮 / 输入框 / 卡片（建议回复卡）/ 气泡 —— 用户要的"偏小圆角"。
    # ⚠️ 原来按钮是**胶囊**（半径 = 高度一半，34 高的按钮就是 17），
    #    那是 Tk 版留下来的形状；微信 PC 的按钮是**小圆角矩形**，
    #    所以这里按用户要求收成 6。
    "control":  6,
    # 更小的东西：滚动条滑块、小标记、分段按钮的外框。
    "chip":     4,
    # 真正的胶囊（半径给足，渲染时被控件高度截住）。**只有确实要胶囊时用**。
    "pill":     999,
    # 完全直角（设置窗口左栏的选中块 —— 实测微信那边就是直角）。
    "none":     0,
}

#: 间距档位。**只有这六档**。
#    数值刻意取 2 的倍数、且不出现 5/7/9/13/15 这种"随手写的"数。
SPACE: Dict[str, int] = {
    "xxs": 2,     # 紧贴（标签与它下面那行小字）
    "xs":  4,     # 图标与文字
    "sm":  6,     # 同一组里的元素之间
    "md":  8,     # 列表行上下内边距
    "lg":  12,    # 分组之间
    "xl":  16,    # 弹窗内容四周（原来散着 10/14/16/18 好几种）
}


def radius(name: str, scale: float = 1.0) -> int:
    """取圆角档位（已按 DPI 缩放）。未知名字 -> `control`。"""
    return px(RADIUS.get(name, RADIUS["control"]), scale)


def space(name: str, scale: float = 1.0) -> int:
    """取间距档位（已按 DPI 缩放）。未知名字 -> `sm`。"""
    return px(SPACE.get(name, SPACE["sm"]), scale)


def shell_border_width(name: Optional[str]) -> int:
    """外框线宽度（基准像素）。

    **两个主题都是 2**：1px 在浅色底上基本看不出来（那套的边框色本来就浅），
    深色下也偏细 —— 用户的要求是「边框粗一点」，不分主题。
    """
    return 2


def palette(theme: str) -> Dict[str, str]:
    """取某个主题的色板（未知主题名 -> 深色）。返回的 dict 不要就地修改。"""
    return PALETTES.get(_norm(theme), DARK_PALETTE)


def _norm(name: Optional[str]) -> str:
    """把各种写法归一成 dark / light。认不出来的落到 dark。

    为什么要归一：这个值可能来自配置文件（用户手写 "Dark"、"黑"）、
    也可能来自采样结果。写错了**不该崩**，落到一个确定的主题上比较好。
    """
    s = (name or "").strip().lower()
    if s in ("light", "l", "day", "浅色", "亮", "白天"):
        return LIGHT
    if s in ("dark", "d", "night", "深色", "暗", "夜间"):
        return DARK
    return DARK


# ---------------------------------------------------------------- 主题判定

def system_theme() -> str:
    """读 Windows 的应用主题设置（深色 / 浅色）。

    `AppsUseLightTheme`：0 = 深色，1 = 浅色。
    读不到就返回深色 —— 本项目实际使用场景实测就是这个，落到它最不容易出意外。
    """
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        try:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        return LIGHT if int(val) == 1 else DARK
    except Exception:
        return DARK


def sample_wechat_theme(hwnd: int) -> Optional[str]:
    """直接看微信**渲染出来**是深色还是浅色。

    做法：PrintWindow 抓图 -> 取聊天区一块区域 -> 看中位亮度。
    聊天区在窗口右侧，取 x 55%~95% / y 15%~75% 一定落在里面（左侧是图标条和
    会话列表，那些栏位的颜色跟聊天区不同，避开它们更稳）。

    返回 "dark" / "light"；**抓不到就返回 None**（窗口最小化时 PrintWindow
    只得到空白），由调用方决定怎么兜底。

    这是纯只读操作：不置顶、不激活、不改变微信任何状态。
    """
    if not hwnd:
        return None
    try:
        from .capture import capture_window, is_blank

        img = capture_window(hwnd)
        if img is None or is_blank(img):
            return None
        W, H = img.size
        box = (int(W * 0.55), int(H * 0.15), int(W * 0.95), int(H * 0.75))
        region = img.crop(box).convert("L")
        # 缩到很小再取中位数：既快又能避开文字/气泡的干扰
        region = region.resize((32, 32))
        px = sorted(region.getdata())
        median = px[len(px) // 2]
        # 实测深色聊天区平均亮度 48、背景 #1E1E1F 亮度约 30；
        # 浅色背景 #EDEDED 亮度约 237。110 落在两簇中间，很宽松。
        return DARK if median < 110 else LIGHT
    except Exception:
        return None


# `ui.theme` 写这些值都表示"不要钉死，去自动判断"
AUTO_ALIASES = {"", "auto", "system", "follow", "跟随", "跟随系统", "自动"}


def is_auto(want) -> bool:
    """`ui.theme` 的值是否是"自动跟随"。"""
    return str(want or "").strip().lower() in AUTO_ALIASES


def detect(cfg=None, hwnd: int = 0, logger=None) -> str:
    """决定用哪套色板。返回 "dark" 或 "light"。

    顺序（前面的赢）：
        ① config 里 `ui.theme` 明确写了 dark / light
        ② 采样微信窗口的实际渲染
        ③ 系统注册表
    `ui.theme: auto`（默认）表示"走 ②③"。
    """
    log = logger or (lambda m: None)

    want = AUTO
    if cfg is not None:
        try:
            want = cfg.get("ui.theme", AUTO) or AUTO
        except Exception:
            want = AUTO

    if not is_auto(want):
        forced = _norm(want)
        log("主题：配置指定 -> %s" % forced)
        return forced

    sampled = sample_wechat_theme(hwnd)
    if sampled:
        log("主题：采样微信窗口 -> %s" % sampled)
        return sampled

    sys_theme = system_theme()
    log("主题：采样不到微信窗口（可能最小化），退到系统设置 -> %s" % sys_theme)
    return sys_theme


# ---------------------------------------------------------------- 字号
#
# ⚠️ 为什么全部用**负数字号**（tkinter 里负数 = 像素，正数 = 磅）
#
# 正数（磅）要由 tkinter 自己按 DPI 换算，而本进程为了对齐截图坐标**自己设过
# DPI 感知**（见 reader.py），两者对 DPI 的认知不一定一致 —— 实测症状是
# "字体放大了、但窗口尺寸按物理像素算"，于是底部按钮被挤出可视区
# （用户反馈过"拉开窗口后才看得见按钮"）。
#
# 用负数字号 = 我自己算像素，跟窗口尺寸用**同一套单位**，这类错位从根上消失。

# 基准像素尺寸（在 96 DPI / 100% 缩放下）
BASE_SIZES = {
    "title":   15,   # 会话名
    "body":    14,   # 正文：回复内容
    "label":   12,   # 标签：状态、来源
    "small":   11,   # 最弱的提示
    "button":  14,   # 按钮文字
    "mono":    12,   # 提示词弹窗
}

FONT_FAMILY = "Microsoft YaHei UI"
FONT_FALLBACK = "Microsoft YaHei"


# ⚠️ 这里原来有个 `ui_scale(root)`，用 Tk 的 `winfo_fpixels("1i")` 量缩放。
#    2026-09-26 删掉 Tk 版之后它成了死代码 ——
#    Qt 侧的缩放由 `qt_ui/base._detect_scale()` 负责
#    （`QScreen.logicalDotsPerInch()` + `GetDpiForSystem` 兜底）。
#    **不要**再往这里加第二处缩放来源：两处各算各的迟早对不上。


def fonts(scale: float = 1.0) -> Dict[str, tuple]:
    """按缩放算出一整套字体元组。

    返回示例：{"body": ("Microsoft YaHei UI", -21), ...}   # scale=1.5
    """
    out = {}
    for name, px in BASE_SIZES.items():
        out[name] = (FONT_FAMILY, -max(8, int(round(px * scale))))
    out["body_bold"] = (FONT_FAMILY, -max(8, int(round(BASE_SIZES["body"] * scale))), "bold")
    out["title_bold"] = (FONT_FAMILY, -max(8, int(round(BASE_SIZES["title"] * scale))), "bold")
    return out


def px(v: float, scale: float = 1.0) -> int:
    """把基准像素值按缩放换算成实际像素。布局里的间距都用它。"""
    return int(round(v * scale))
