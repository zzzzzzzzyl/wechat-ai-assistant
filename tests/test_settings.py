# -*- coding: utf-8 -*-
"""分层配置与路径解析的测试。

为什么值得单独测
----------------
`settings.py` 要做三件很容易做错、错了又很难发现的事：

  1. **分层覆盖的优先级** —— 用户设置要压过 config.yaml，密钥最后压过一切。
     弄反了不会报错，只会"改了没反应"。

  2. **「恢复默认」是真的能恢复** —— 删掉覆盖项之后，config.yaml 的值必须重新
     透出来。如果删得不干净（留个 `{"ui": {}}` 的空壳），界面会显示"已修改"
     但值还是旧的，非常难查。

  3. **写 secrets.yaml 不能抹掉注释** —— 用 `yaml.dump` 会把用户的注释和键序
     全冲掉。所以走了行级替换，而**行级文本处理最容易在边界情况上出错**
     （节不存在、键不存在、节在文件末尾、值里有特殊字符），必须钉住。

另外 `paths.py` 的冻结态分支在开发时永远走不到（`sys.frozen` 是假），
只能靠测试手动模拟，否则打包时才发现就太晚了。

用法：python tests/test_settings.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant import paths  # noqa: E402
from wechat_assistant.settings import (SettingsStore, Setting,  # noqa: E402
                                       dotted_del, dotted_get, dotted_set)

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


CONFIG = """# 这是出厂默认配置
ai:
  model: deepseek-flash
  temperature: 1.0
  n_replies: 3
ui:
  width: 340
"""


def make_store(tmp, secrets="", settings=None):
    c = os.path.join(tmp, "config.yaml")
    s = os.path.join(tmp, "secrets.yaml")
    u = os.path.join(tmp, "settings.json")
    write(c, CONFIG)
    if secrets:
        write(s, secrets)
    if settings is not None:
        write(u, settings)
    return SettingsStore(config_path=c, secrets_path=s, settings_path=u,
                         logger=lambda m: None)


# ============================================================ 1. 点号路径

def test_dotted(tmp):
    print("\n1. 点号路径读写")
    d = {}
    dotted_set(d, "a.b.c", 1)
    check("设置三级路径", dotted_get(d, "a.b.c") == 1, repr(d))
    check("缺路径取默认值", dotted_get(d, "a.b.x", "无") == "无")

    dotted_set(d, "a.b.c", 2)
    check("覆盖已有值", dotted_get(d, "a.b.c") == 2)

    check("删除叶子", dotted_del(d, "a.b.c") is True)
    check("父层一起清干净", d == {}, repr(d))

    d2 = {"a": {"b": 1, "c": 2}}
    dotted_del(d2, "a.b")
    check("父层还有别的键就保留", d2 == {"a": {"c": 2}}, repr(d2))

    check("删不存在的返回 False", dotted_del(d2, "x.y.z") is False)


# ============================================================ 2. 取值类型转换

def test_coerce(tmp):
    print("\n2. 设置项的值转换与范围校验")

    s_int = Setting("k", "g", "整数", "int", lo=1, hi=10)
    check("整数转换", s_int.coerce("7") == 7)
    try:
        s_int.coerce("11")
        check("超上限要报错", False)
    except ValueError:
        check("超上限要报错", True)
    try:
        s_int.coerce("0")
        check("低于下限要报错", False)
    except ValueError:
        check("低于下限要报错", True)
    try:
        s_int.coerce("abc")
        check("非数字要报错", False)
    except ValueError:
        check("非数字要报错", True)

    s_bool = Setting("k", "g", "开关", "bool")
    for raw, want in [("开", True), ("on", True), ("true", True), ("1", True),
                      (True, True), ("关", False), ("off", False), ("0", False)]:
        check("开关识别 %r" % raw, s_bool.coerce(raw) is want)

    s_list = Setting("k", "g", "名单", "list")
    got = s_list.coerce("张三\n\n  李四  \n张三\n")
    check("名单去空行去重保序", got == ["张三", "李四"], repr(got))
    check("名单接受已经是列表的值", s_list.coerce(["a", "b"]) == ["a", "b"])

    s_f = Setting("k", "g", "小数", "float", lo=0.0, hi=2.0)
    check("小数转换", abs(s_f.coerce("1.5") - 1.5) < 1e-9)
    check("整数也能当小数", abs(s_f.coerce("1") - 1.0) < 1e-9)


# ============================================================ 3. 三层合并

def test_layers(tmp):
    print("\n3. 三层配置的优先级")
    st = make_store(tmp, secrets='ai:\n  api_key: "sk-SECRET"\n',
                    settings=json.dumps({"ai": {"temperature": 1.7}, "ui": {"width": 400}}))
    cfg = st.load()

    check("用户设置压过 config.yaml",
          abs(float(cfg.get("ai.temperature")) - 1.7) < 1e-9,
          "得到 %r" % cfg.get("ai.temperature"))
    check("config.yaml 里没被覆盖的仍在",
          cfg.get("ai.model") == "deepseek-flash", repr(cfg.get("ai.model")))
    check("用户设置里新增的键生效", cfg.get("ui.width") == 400)
    check("secrets.yaml 合并进来", cfg.get("ai.api_key") == "sk-SECRET")
    check("config 里的 ui.width 被覆盖", cfg.get("ui.width") == 400)

    check("来源判定：默认", st.source("ai.model") == "default")
    check("来源判定：用户改过", st.source("ai.temperature") == "user")
    check("来源判定：密钥", st.source("ai.api_key") == "secret")
    check("is_overridden", st.is_overridden("ui.width") and not st.is_overridden("ai.n_replies"))


def test_secret_wins_over_everything(tmp):
    print("\n3b. 密钥层压过 config.yaml 和 settings.json")
    st = make_store(tmp, secrets='ai:\n  api_key: "sk-FROM_SECRETS"\n')
    st.set("ai.api_key", "sk-FROM_SETTINGS")
    cfg = st.load()
    check("secrets.yaml 里的密钥赢", cfg.get("ai.api_key") == "sk-FROM_SECRETS",
          repr(cfg.get("ai.api_key")))


# ============================================================ 4. 恢复默认

def test_reset(tmp):
    print("\n4. 恢复默认")
    st = make_store(tmp, settings=json.dumps({"ui": {"width": 500, "opacity": 0.7}}))
    check("初始是覆盖值", st.load().get("ui.width") == 500)

    st.unset("ui.width")
    check("恢复默认后 config 的值透出来", st.load().get("ui.width") == 340,
          repr(st.load().get("ui.width")))
    check("同节其它覆盖不受影响", st.load().get("ui.opacity") == 0.7)
    check("来源变回 default", st.source("ui.width") == "default")

    st.unset("ui.opacity")
    check("同节覆盖清空后不留空壳", st.overrides() == {}, repr(st.overrides()))

    st2 = make_store(os.path.join(tmp, "b"),
                     settings=json.dumps({"ai": {"temperature": 0.5}, "ui": {"width": 400}}))
    st2.reset_all()
    check("reset_all 清空全部覆盖", st2.overrides() == {})
    check("reset_all 后回到默认", abs(float(st2.load().get("ai.temperature")) - 1.0) < 1e-9)

    # ⚠️ 2026-10-14 用户要求：「恢复默认**接口地址与模型**不要改」。
    #    它们是"用哪家服务"的选择，不是调参 —— 清掉就得重新翻文档、重新填。
    #    这条同时守三件东西（`RESET_KEEP` 一处改、三处生效）：
    #    `reset_all()` 留着它们 / 单行那颗按钮不画在它们那行 / 确认框文案同源。
    from wechat_assistant.settings import RESET_KEEP, RESET_KEEP_LABEL
    st3 = make_store(os.path.join(tmp, "c"),
                     settings=json.dumps({"ai": {"base_url": "https://api.example.com",
                                                 "model": "my-model",
                                                 "temperature": 0.5},
                                          "ui": {"width": 400}}))
    check("（前置）这几项确实被覆盖了",
          st3.raw_override("ai.base_url") == "https://api.example.com"
          and st3.raw_override("ai.model") == "my-model",
          repr(st3.overrides()))
    st3.reset_all()
    check("reset_all **不动**接口地址", st3.raw_override("ai.base_url")
          == "https://api.example.com", repr(st3.overrides()))
    check("reset_all **不动**模型", st3.raw_override("ai.model") == "my-model",
          repr(st3.overrides()))
    check("  （同一份配置里的调参仍然被清掉 —— 别把整层都留下）",
          st3.raw_override("ai.temperature") is None
          and st3.raw_override("ui.width") is None, repr(st3.overrides()))
    check("RESET_KEEP 覆盖了地址/模型/Key/知识库开关",
          set(RESET_KEEP) == {"ai.base_url", "ai.model", "ai.api_key",
                              "kb.enabled"},
          repr(RESET_KEEP))
    # ⚠️ 2026-10-17 用户问「恢复默认不删库有吗」：
    #    库本身**从来不会被删**（`state/kb.db` 是独立文件，`reset_all()` 只清覆盖层），
    #    但原来会把 `kb.enabled` 关掉 → 表现成"资料还在、回复里没它了"。
    #    现在开关也进 RESET_KEEP，所以这两条一起守。
    st4 = make_store(os.path.join(tmp, "d"),
                     settings=json.dumps({"kb": {"enabled": True},
                                          "kb.top_k": 5}))
    st4.reset_all()
    check("reset_all **不动**知识库开关（库还在，就不该把它关掉）",
          st4.raw_override("kb.enabled") is True, repr(st4.overrides()))
    check("  （知识库的调参照常清掉）", st4.raw_override("kb.top_k") is None,
          repr(st4.overrides()))
    check("确认框文案里点名了这几样（用户得知道什么不会动）",
          "接口地址" in RESET_KEEP_LABEL and "模型" in RESET_KEEP_LABEL
          and "API Key" in RESET_KEEP_LABEL, RESET_KEEP_LABEL)


# ============================================================ 5. 落盘

def test_save_reload(tmp):
    print("\n5. 保存与重新加载")
    st = make_store(tmp)
    st.set("ui.width", 380)
    st.set("ui.opacity", 0.85)
    st.save()

    check("settings.json 已生成", os.path.exists(st.settings_path))
    with open(st.settings_path, "r", encoding="utf-8") as f:
        saved = json.load(f)
    check("落盘内容正确", saved["ui"]["width"] == 380, repr(saved))
    check("中文不转义（ensure_ascii=False）", "\\u" not in read(st.settings_path))
    check("没有留下 .tmp 临时文件",
          not os.path.exists(st.settings_path + ".tmp"))

    st2 = SettingsStore(config_path=st.config_path, secrets_path=st.secrets_path,
                        settings_path=st.settings_path, logger=lambda m: None)
    check("新实例读到已保存的值", st2.load().get("ui.width") == 380)

    # 表单式提交
    # ⚠️ 用 SPEC 里有的键（`apply_form` 会跳过非 SPEC 键 —— `ui.width` 现在由
    #    面板拖动用 `set()` 写，见 9b 那两条）
    st2.apply_form({"ui.opacity": "0.6", "ai.n_replies": "5"})
    check("表单提交后内存已更新",
          abs(st2.load().get("ui.opacity") - 0.6) < 1e-9
          and st2.load().get("ai.n_replies") == 5,
          "%r / %r" % (st2.load().get("ui.opacity"), st2.load().get("ai.n_replies")))


def test_apply_form_is_all_or_nothing(tmp):
    print("\n5b. 表单有一项不合法就整张不写")
    st = make_store(tmp)
    st.set("ui.opacity", 0.8)
    before = st.overrides()
    errs = st.apply_form({"ui.opacity": "0.6", "ai.n_replies": "99"})   # 99 超上限 8
    check("返回了错误说明", len(errs) == 1, repr(errs))
    check("合法的那个也没被写进去", st.overrides() == before, repr(st.overrides()))


# ============================================================ 6. secrets 行级写入

def test_patch_yaml(tmp):
    print("\n6. secrets.yaml 行级替换（保住注释）")

    # 6.1 替换已有的键
    src = ['# 本地密钥，不要提交', '', 'ai:', '  api_key: "sk-OLD"']
    got = SettingsStore._patch_yaml_scalar(src, "ai", "api_key", "sk-NEW")
    check("替换已有键", '  api_key: "sk-NEW"' in got, repr(got))
    check("注释还在", got[0] == '# 本地密钥，不要提交')
    check("旧值没了", not any("sk-OLD" in line for line in got))

    # 6.2 节后面还有别的顶级键
    src = ['ai:', '  api_key: "sk-OLD"', 'ui:', '  width: 1']
    got = SettingsStore._patch_yaml_scalar(src, "ai", "api_key", "sk-NEW")
    check("后面有别的节也能替换", got[1] == '  api_key: "sk-NEW"', repr(got))
    check("别的节原样保留", got[2:] == ['ui:', '  width: 1'], repr(got))

    # 6.3 节在、键不在 -> 插进节里
    src = ['ai:', '  model: x', 'ui:', '  width: 1']
    got = SettingsStore._patch_yaml_scalar(src, "ai", "api_key", "sk-NEW")
    check("插入节的末尾", got.index('  api_key: "sk-NEW"') < got.index('ui:'), repr(got))
    check("原有键保留", '  model: x' in got)

    # 6.4 整个节都没有 -> 新建
    src = ['# 头部注释']
    got = SettingsStore._patch_yaml_scalar(src, "ai", "api_key", "sk-NEW")
    check("新建节", got == ['# 头部注释', '', 'ai:', '  api_key: "sk-NEW"'], repr(got))

    # 6.5 值里有特殊字符要加引号
    got = SettingsStore._patch_yaml_scalar(['ai:'], "ai", "api_key", 'sk-a:b #c"d')
    check("特殊字符被引号包住并转义", got[-1] == '  api_key: "sk-a:b #c\\"d"', repr(got[-1]))

    # 6.6 空文件
    got = SettingsStore._patch_yaml_scalar([], "ai", "api_key", "sk-NEW")
    check("空文件也能处理", got == ['ai:', '  api_key: "sk-NEW"'], repr(got))


def test_set_secret_end_to_end(tmp):
    print("\n6b. set_secret 端到端")
    st = make_store(tmp, secrets='# 别提交这个文件\nai:\n  api_key: "sk-OLD"\n  model: keep\n')
    st.set_secret("ai.api_key", "sk-NEW")

    txt = read(st.secrets_path)
    check("值被换掉", "sk-NEW" in txt and "sk-OLD" not in txt, repr(txt))
    check("注释保留", "# 别提交这个文件" in txt)
    check("同节其它键保留", "model: keep" in txt)
    check("备份文件生成了", os.path.exists(st.secrets_path + ".bak"))
    check("删掉 .tmp", not os.path.exists(st.secrets_path + ".tmp"))
    check("内存里的密钥也更新了", st.load().get("ai.api_key") == "sk-NEW")

    # 写出来的必须还是合法 YAML（引号转义没搞坏的话）
    import yaml
    parsed = yaml.safe_load(txt)
    check("写出来仍是合法 YAML", parsed.get("ai", {}).get("api_key") == "sk-NEW", repr(parsed))

    # 值里有冒号和井号时也要能读回来
    st.set_secret("ai.api_key", 'sk-x:y #z')
    parsed = yaml.safe_load(read(st.secrets_path))
    check("特殊字符往返不走样", parsed["ai"]["api_key"] == "sk-x:y #z", repr(parsed))


# ============================================================ 7. 路径解析

def test_paths(tmp):
    print("\n7. 路径解析")
    check("源码运行时不是冻结态", paths.is_frozen() is False)
    # ⚠️ 判据是"基准目录就是本仓库根"，**别写死目录名**（2026-10-15）：
    #    原来写的是 `basename(...) == "wechat-ai-assistant"`，于是仓库目录
    #    换一个名字这条就**假红** —— 功能一点问题都没有，纯粹是断言写死了路径。
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    check("基准目录 = 项目根",
          os.path.normcase(os.path.abspath(paths.base_dir())) == os.path.normcase(_root),
          paths.base_dir())
    check("配置在基准目录下",
          paths.config_path() == os.path.join(paths.base_dir(), "config.yaml"))

    abs_path = os.path.join(tmp, "x.db")
    check("绝对路径原样返回", paths.resolve(abs_path) == os.path.abspath(abs_path))
    check("相对路径按基准目录解析",
          paths.resolve("state/a.db") == os.path.join(paths.base_dir(), "state", "a.db"))

    d = os.path.join(tmp, "deep", "nested")
    paths.ensure_dir(d)
    check("ensure_dir 建出目录", os.path.isdir(d))
    f = os.path.join(tmp, "only", "file.txt")
    paths.ensure_parent(f)
    check("ensure_parent 建出父目录", os.path.isdir(os.path.dirname(f)))


def test_frozen_base_dir(tmp):
    """模拟打包后的情况：基准目录要变成 exe 所在目录，而不是临时解压目录。

    开发时永远走不到这个分支（sys.frozen 是假），所以只能手动模拟。
    这一条要是坏的，打包出来的 exe 会：
        - 读不到 exe 旁边的 config.yaml（用的是打进包里的那份）
        - 把 state/ 写进临时目录，关掉程序就没了
    """
    print("\n7b. 冻结态（模拟打包后）")
    fake_exe = os.path.join(tmp, "dist", "WeChatAI.exe")
    os.makedirs(os.path.dirname(fake_exe), exist_ok=True)
    old_frozen = getattr(sys, "frozen", None)
    old_exe = sys.executable
    try:
        sys.frozen = True
        sys.executable = fake_exe
        check("识别为冻结态", paths.is_frozen() is True)
        check("基准目录 = exe 所在目录",
              paths.base_dir() == os.path.dirname(os.path.abspath(fake_exe)),
              paths.base_dir())
        check("基准目录不再是项目根", paths.base_dir() != paths.pkg_dir())
        check("配置跟着 exe 走",
              paths.config_path() == os.path.join(os.path.dirname(os.path.abspath(fake_exe)),
                                                  "config.yaml"))
    finally:
        if old_frozen is None:
            try:
                del sys.frozen
            except Exception:
                pass
        else:
            sys.frozen = old_frozen
        sys.executable = old_exe


# ============================================================ 8. 声明表自检

def test_spec(tmp):
    print("\n8. 设置声明表自检")
    from wechat_assistant.settings import GROUPS, SPEC, SPEC_BY_KEY

    check("键不重复", len(SPEC_BY_KEY) == len(SPEC), "%d vs %d" % (len(SPEC_BY_KEY), len(SPEC)))
    gids = {g[0] for g in GROUPS}
    bad = [s.key for s in SPEC if s.group not in gids]
    check("每项的 group 都在 GROUPS 里", not bad, repr(bad))
    bad2 = [s.key for s in SPEC if s.kind == "choice" and not s.choices]
    check("choice 型必须有候选项", not bad2, repr(bad2))
    bad3 = [s.key for s in SPEC if s.kind in ("int", "float") and (s.lo is None or s.hi is None)]
    check("数值型必须给了范围", not bad3, repr(bad3))
    check("密钥项标了 secret", SPEC_BY_KEY["ai.api_key"].secret is True)
    check("热键项标了需要重启", SPEC_BY_KEY["trigger.on_hotkey"].restart is True)

    # ⚠️⚠️ **接口地址与模型出厂必须是空的**（2026-10-13 用户要求「默认为空吧」）。
    #    预填（原来是 DeepSeek 的地址和模型）等于**替用户选好了服务商** ——
    #    换一家时只填 Key，请求会静默打到 DeepSeek 上、报错也说不清。
    #    两处都要空：SPEC 的 `default=` **和** 出厂 `config.yaml` 的值。
    check("SPEC：接口地址默认是空的",
          not (SPEC_BY_KEY["ai.base_url"].default or "").strip(),
          repr(SPEC_BY_KEY["ai.base_url"].default))
    check("SPEC：模型默认是空的",
          not (SPEC_BY_KEY["ai.model"].default or "").strip(),
          repr(SPEC_BY_KEY["ai.model"].default))
    try:
        import yaml

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config.yaml"), encoding="utf-8") as f:
            shipped = yaml.safe_load(f) or {}
        ship_ai = shipped.get("ai") or {}
        check("出厂 config.yaml：接口地址是空的",
              not str(ship_ai.get("base_url") or "").strip(),
              repr(ship_ai.get("base_url")))
        check("出厂 config.yaml：模型是空的",
              not str(ship_ai.get("model") or "").strip(),
              repr(ship_ai.get("model")))
    except Exception as e:
        check("读出厂 config.yaml 检查这两项", False, "%s: %s" % (type(e).__name__, e))

    # ⚠️⚠️ **常用分组不许再涨回去**（2026-10-11 用户要求「适当简化一下设置」）。
    #
    # 起因：`AI 接口` 那一组涨到了 **10 项**（最长输出 / 超时 / 输入单价 /
    # 输出单价 挤在常用项中间），用户的反馈是「下面很多都不用出现吧」。
    # 同理 `外观` 到过 14 项、`会话与记录` 到过 13 项。
    #
    # 判据：**"调完就不动"的项（数值调参、诊断、只在某个开关打开时才有用的）
    #       一律放「高级」**，常用组保持短。
    # ⚠️ `advanced` **不设上限** —— 它就是专门堆杂项的地方，塞多少都不影响读数。
    # ⚠️ `session` 是 0 项：那一组用的是自定义容器（会话列表），不是 SPEC 表单。
    LIMIT = 12
    over = [(g[0], g[1], len([s for s in SPEC if s.group == g[0]]))
            for g in GROUPS
            if g[0] != "advanced"
            and len([s for s in SPEC if s.group == g[0]]) > LIMIT]
    check("常用分组不超过 %d 项（超了就挪到「高级」）" % LIMIT, not over,
          "超了: %s" % over if over else "")
    # ⚠️ 2026-10-14 用户要求「把分析图片放在 AI 栏里面」——
    #    那一项是 `ocr_read.attach_image`。
    #    ⚠️ 它决定的是"发给模型的请求里带不带图"，是 AI 侧的开关，
    #    跟"记录"没关系；键名还叫 `ocr_read.*`（**别去改键名**，那要动配置迁移）。
    _att = SPEC_BY_KEY["ocr_read.attach_image"]
    check("「识别图片」在「AI 接口」组里（用户要求）", _att.group == "ai", _att.group)
    # ⚠️ 同一天又要求：「改到视觉模型前面，并且名称叫识别图片」。
    #    SPEC 里的**先后就是界面的先后**（`settings_ui` 按 SPEC 顺序铺），
    #    所以这里比的是**列表下标** —— 光看 group 相同是验不出来的。
    check("「识别图片」的名字是用户要的那个", _att.label == "识别图片", _att.label)
    _keys = [s.key for s in SPEC if s.group == "ai"]
    check("  （并且排在「视觉模型」**前面**）",
          _keys.index("ocr_read.attach_image") < _keys.index("ai.vision_model"),
          " -> ".join(_keys))
    # 这几项是"调完就不动"的，必须待在「高级」里（别被挪回常用组）
    # ⚠️ `reply.use_cursor_anchor` 原来在这个清单里，2026-10-14 用户点名
    #    「把鼠标定位放在行为里面」→ 它现在**故意在常用组**，所以从这里移走。
    MOVED = ["ai.max_tokens", "ai.timeout", "ai.temperature",
             "style.max_samples", "style.max_length",
             "ui.min_width", "ui.width_max", "ui.follow_interval",
             "trigger.min_interval", "trigger.cooldown_after_copy",
             "runtime.max_chars", "runtime.max_msg_chars",
             "record.interval", "record.fast_interval", "record.fast_hold",
             "ocr_read.probe_interval", "ocr_read.probe_fast",
             "history.max_per_session"]
    wrong = [k for k in MOVED if SPEC_BY_KEY[k].group != "advanced"]
    check("%d 项调参类都在「高级」里" % len(MOVED), not wrong, repr(wrong))

    # ⚠️⚠️ **被删掉的设置项不许加回来**（2026-10-13 用户要求「去掉部分选项」）。
    #
    # 判据是"**产品代码里有没有消费者**"（`.tmp/audit_settings2.py` 那种扫描）：
    #   ① 前三项产品代码**一处都没读** -> 设置了没有任何效果（假开关）；
    #   ② `ui.width` 有消费者，但**面板上拖左右边缘就能改、松手自动写回配置**
    #      （`qt_ui/panel.py:_save_width`）-> 摆在设置里是同一个东西两处可改；
    #   ③ 两个单价只管日志里那行花费估算（用户明确说"不用设置"）。
    # ⚠️ 想加回来先回答："谁读它？"和"界面上已经有别的地方改它吗？"
    #
    # ⚠️ 2026-10-14 又加了三项（用户要求「先把界面删掉吧这几个」）：
    #    `ui.radius`             —— 只在悬浮窗生效（别的窗口固定用常量），
    #                               而且 `int(v or 10)` 让 **0 等于没填**；
    #                               默认 10 = 常量，删掉行为不变。
    #    `trigger.on_window_focus` / `trigger.on_session_switch`
    #                            —— 两个"自动触发源"，默认关、文档写着"别开"，
    #                               开着会在切窗口/切会话时**自己调 AI 花钱**。
    #    ⚠️ 三个的**代码路径全部保留**（`panel.py` / `trigger.py` / `run_watch.py`
    #       照旧 `.get(..., 默认)`），所以是"删的是设置项，不是功能"。
    #    `ocr_read.probe`（按需读屏，默认 true）/ `ocr_read.record`（一直读屏，默认 false）
    #      也在同一天被删（用户「按照默认的来就可」）—— 前者是**推荐档**、
    #      后者实测贵 5 倍，两个都只该保持默认；读取在 `recorder.py` / `ocr_reader.py`。
    REMOVED = ["ui.rounded", "logging.level", "ocr_read.record_interval",
               "ui.width", "ai.price_in", "ai.price_out",
               "ui.radius", "trigger.on_window_focus", "trigger.on_session_switch",
               "ocr_read.probe", "ocr_read.record",
               # ⚠️ 2026-10-14：`ui.frameless`（无边框窗口）也删了 —— 用户要求
               #    「删除无边框窗口吧」。关掉它只是退回系统标题栏（多一条丑边），
               #    而且它是**建窗口时读的**（`panel.py:770` 设 `FramelessWindowHint`）→
               #    运行期改会销毁重建原生窗口（"闪一下"），所以它其实**要重启才生效**
               #    却没标 `restart`（界面在骗人）。键和读取代码都留着，值恒为 `True`。
               "ui.frameless"]
    back = [k for k in REMOVED if k in SPEC_BY_KEY]
    check("被删的 %d 项没有偷偷回到设置界面里" % len(REMOVED), not back, repr(back))
    # 配置键本身可以留着（面板/跟随器/日志还要读），"删的是设置项，不是功能"
    check("删的是设置项、不是配置键（ui.width 仍在 config.yaml 里）",
          "width:" in open(os.path.join(
              os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
              "config.yaml"), encoding="utf-8").read())


def test_spec_covers_config():
    """SPEC 的范围必须**覆盖 config.yaml 里实际用的值**。

    ⚠️ 这个坑踩过，而且后果很严重：`ui.follow_interval` 配成了 0.016，
    而 SPEC 里写 `lo=0.02` —— 于是**整张表单校验失败，设置窗口一个都保存不下去**，
    报的还是"跟随检查间隔：不能小于 0.02"这种看起来无关的话。

    所以：**config.yaml 里出现的每个值都必须落在 SPEC 声明的范围/候选项内。**
    """
    print("\n9. SPEC 的范围要覆盖 config.yaml 的实际值")
    import os as _os

    from wechat_assistant.config import Config, load_config
    from wechat_assistant.settings import SPEC, _deep_merge

    # 只看出厂 config.yaml，**不带用户覆盖**（否则用户填的非法值也会算进来）
    cfg_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             "config.yaml")
    if not _os.path.exists(cfg_path):
        check("（没有 config.yaml，跳过）", True)
        return
    import yaml

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    base = Config(raw, cfg_path)

    bad = []
    for spec in SPEC:
        v = base.get(spec.key, None)
        if v is None:
            continue                       # 没配就用 SPEC 默认，不用管
        try:
            spec.coerce(v)
        except Exception as e:
            bad.append("%s=%r -> %s" % (spec.key, v, e))
    check("config.yaml 的每个值都在 SPEC 范围内", not bad, "; ".join(bad))

    # 反向也查一遍：SPEC 的默认值自己得合法
    bad2 = []
    for spec in SPEC:
        if spec.default is None:
            continue
        try:
            spec.coerce(spec.default)
        except Exception as e:
            bad2.append("%s 默认 %r -> %s" % (spec.key, spec.default, e))
    check("SPEC 的默认值自己合法", not bad2, "; ".join(bad2))

    # choice 型的默认值必须在候选项里
    bad3 = [s.key for s in SPEC
            if s.kind == "choice" and s.default is not None
            and s.default not in [c[0] for c in s.choices]]
    check("choice 的默认值在候选项里", not bad3, repr(bad3))


def test_only_changes():
    """提交整张表单时，**没改过的项不该被写成覆盖**。

    分层配置的意义就是"没改过的跟着 config.yaml 走"。如果一提交就全写，
    settings.json 会堆满用户没碰过的项，以后 config.yaml 改默认值也透不出来。
    """
    print("\n9b. 只写真正改过的项")
    tmp2 = tempfile.mkdtemp(prefix="dsh_only_")
    try:
        cfg_p = os.path.join(tmp2, "config.yaml")
        write(cfg_p, "ui:\n  width: 340\n  opacity: 1.0\nai:\n  n_replies: 3\n")
        st = SettingsStore(config_path=cfg_p,
                           secrets_path=os.path.join(tmp2, "secrets.yaml"),
                           settings_path=os.path.join(tmp2, "settings.json"),
                           logger=lambda m: None)

        # 提交一张"什么都没动"的表单
        st.apply_form({"ui.opacity": "1.0", "ai.n_replies": "3"})
        check("没改过的项不建覆盖", st.overrides() == {}, repr(st.overrides()))

        # 改一个 -> 只写那一个
        st.apply_form({"ui.opacity": "0.7", "ai.n_replies": "3"})
        ov = st.overrides()
        check("只写改过的那一项",
              ov == {"ui": {"opacity": 0.7}}, repr(ov))
        check("生效值跟着变", abs(st.load().get("ui.opacity") - 0.7) < 1e-9,
              repr(st.load().get("ui.opacity")))
        check("没改的项仍然来自 config.yaml",
              st.load().get("ai.n_replies") == 3, repr(st.load().get("ai.n_replies")))

        # ⚠️ 2026-10-13：**不在 SPEC 里的键，表单写不进去**（`apply_form` 会跳过）。
        #    这不是缺点 —— `ui.width` 就是这种键：它由**面板拖动**用
        #    `SettingsStore.set()` 直接写（`qt_ui/panel.py:_save_width`），
        #    设置界面里没有它，所以也不该有任何表单能改它。
        st.apply_form({"ui.width": "420"})
        check("表单里的非 SPEC 键被忽略（ui.width 归拖动管）",
              st.overrides() == {"ui": {"opacity": 0.7}}, repr(st.overrides()))
        # 而 set() 这条路照旧能写（拖动保存走的就是它）
        st.set("ui.width", 420)
        check("set() 不校验 SPEC，拖动保存照常生效",
              st.load().get("ui.width") == 420, repr(st.load().get("ui.width")))
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)


def test_update_from_in_place(tmp):
    """9c. `Config.update_from`：**旧对象**也要看到新值（"改了不生效"的根治）。

    2026-10-17：设置保存时原来是 `cfg = new_cfg`（换变量），而进程里一堆对象
    在构造时就把 cfg 存成了 `self.cfg` —— 它们手里还是旧对象，于是
    "显示已保存、实际没生效"，而且不报错。同一类坑本项目栽过 6 次。
    """
    print("\n9c. 原地更新配置（持有旧 cfg 的对象必须看到新值）")
    from wechat_assistant.config import Config

    old = Config({"ai": {"model": "旧的", "n_replies": 3}, "ui": {"opacity": 1.0}},
                 "c.yaml")
    new = Config({"ai": {"model": "新的", "n_replies": 5}, "ui": {"opacity": 0.6}},
                 "c.yaml")

    # 模拟"某个对象在构造时抓走了 cfg"
    holder = old
    old.update_from(new)
    check("持有者看到新值（不是旧对象）", holder.get("ai.model") == "新的",
          repr(holder.get("ai.model")))
    check("  改过的数字项也换了", holder.get("ai.n_replies") == 5,
          repr(holder.get("ai.n_replies")))
    check("  嵌套层的值也换了", abs(holder.get("ui.opacity") - 0.6) < 1e-9,
          repr(holder.get("ui.opacity")))
    check("  ⚠️ 对象本身没被换掉（`is` 不变，持有者才有效）", holder is old)

    # ⚠️ 不能共享嵌套 dict：否则改一边脏另一边（下一拍读到的是别人的中间状态）
    new.get("ai")["model"] = "又改了"
    check("  ⚠️ 两份配置不共享嵌套 dict（deepcopy）",
          holder.get("ai.model") == "新的", repr(holder.get("ai.model")))

    # 键被删掉时也要真的消失（不是"旧值残留")
    new2 = Config({"ai": {}}, "c.yaml")
    holder.update_from(new2)
    check("  新配置里删掉的键，旧对象也读不到了",
          holder.get("ai.model", "缺省") == "缺省", repr(holder.get("ai.model")))


def main():
    tmp = tempfile.mkdtemp(prefix="dsh_settings_")
    try:
        test_dotted(tmp)
        test_coerce(tmp)
        test_layers(tmp)
        test_secret_wins_over_everything(tmp)
        test_reset(tmp)
        test_save_reload(tmp)
        test_apply_form_is_all_or_nothing(tmp)
        test_patch_yaml(tmp)
        test_set_secret_end_to_end(tmp)
        test_paths(tmp)
        test_frozen_base_dir(tmp)
        test_spec(tmp)
        test_spec_covers_config()
        test_only_changes()
        test_update_from_in_place(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print("通过 %d，失败 %d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
