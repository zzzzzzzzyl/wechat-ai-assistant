"""检查更新 / 自动更新的单元测试（2026-11-09）。

⚠️ **不联网**：`latest()` 那条用打桩 `urlopen` 验"能解析/能报错"，
    真实网络请求在 `probes/` 里手动跑，别放进单测。
"""

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s" % name)
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def test_version():
    from wechat_assistant import updater

    print("\n1. 版本号怎么比")
    check("v0.2.3 -> (0,2,3)", updater.parse_version("v0.2.3") == (0, 2, 3),
          str(updater.parse_version("v0.2.3")))
    check("0.3 -> (0,3)（少一段也行）", updater.parse_version("0.3") == (0, 3))
    check("带后缀也能解析", updater.parse_version("v0.2.3-beta") == (0, 2, 3),
          str(updater.parse_version("v0.2.3-beta")))
    check("乱七八糟的解析出空元组", updater.parse_version("latest") == ())
    check("空串不崩", updater.parse_version("") == ())

    check("0.2.4 比 0.2.3 新", updater.is_newer("0.2.4", "0.2.3"))
    check("0.2.3 不比 0.2.3 新", not updater.is_newer("0.2.3", "0.2.3"))
    check("0.2.3 不比 0.2.4 新", not updater.is_newer("0.2.3", "0.2.4"))
    check("0.10 比 0.9 新（按数字比，不按字符串）",
          updater.is_newer("0.10", "0.9"))
    check("解析不出来时**不更新**（宁可不升）",
          not updater.is_newer("latest", "0.2.3"))


def test_apply_script():
    from wechat_assistant import updater

    print("\n2. 更新脚本（等退出 -> 覆盖 -> 重启）")
    s = updater.apply_script(1234, r"C:\tmp\stage\微信AI助手",
                             r"D:\app\微信AI助手", r"D:\app\微信AI助手\微信AI助手.exe")
    check("会等指定 pid 退出", "Get-Process -Id 1234" in s)
    check("等不到就放弃（有 60 秒上限）", "AddSeconds(60)" in s)
    check("用 Copy-Item 覆盖（**不是** mirror，不会删用户数据）",
          "Copy-Item" in s and "-Recurse -Force" in s and "Remove-Item" not in s)
    check("最后会重启 exe", "Start-Process -FilePath" in s)
    check("路径都加了引号（中文/空格安全）", s.count("'") >= 6)

    # 单引号要转义成两个，否则 PowerShell 会解析坏
    s2 = updater.apply_script(1, r"C:\a'b", r"C:\c", r"C:\c\e.exe")
    check("路径里的单引号被转义", "C:\\a''b" in s2, s2.splitlines()[4][:40])

    enc = base64.b64encode(s.encode("utf-16-le")).decode("ascii")
    check("能编码成 -EncodedCommand 用的纯 ASCII（避免 GBK 把中文路径搞坏）",
          all(c.isascii() for c in enc) and len(enc) > 100)


def test_latest_stub():
    import json
    import urllib.error
    import urllib.request

    from wechat_assistant import updater

    print("\n3. 查最新版本（打桩 urlopen，不联网）")

    class Resp:
        def __init__(self, payload):
            self._p = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = urllib.request.urlopen
    try:
        urllib.request.urlopen = lambda req, timeout=None: Resp({
            "tag_name": "v0.2.5",
            "published_at": "2026-11-09T12:00:00Z",
            "assets": [{"name": "wechat-ai-assistant-win64.zip",
                        "size": 134298275,
                        "browser_download_url": "https://example.com/a.zip"}],
        })
        info = updater.latest()
        check("读出了版本号", info["version"] == "0.2.5", str(info))
        check("读出了 zip 地址", info["url"] == "https://example.com/a.zip")
        check("发布日只留日期", info["published"] == "2026-11-09", str(info["published"]))

        urllib.request.urlopen = lambda req, timeout=None: Resp({
            "tag_name": "v0.2.5", "assets": []})
        try:
            updater.latest()
            err = ""
        except Exception as e:
            err = str(e)
        check("没有 zip 资产时**报错**（不是静默当成最新）", "zip" in err, err)

        def boom(req, timeout=None):
            raise urllib.error.URLError("连不上")

        urllib.request.urlopen = boom
        try:
            updater.latest()
            err = ""
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)
        check("网络失败会抛（界面据此提示连不上）", "URLError" in err, err)
    finally:
        urllib.request.urlopen = real


def test_install_dir():
    from wechat_assistant import updater

    print("\n4. 装在哪 / 是不是打包版")
    check("源码版 is_frozen 为假", updater.is_frozen() is False)
    check("源码版的安装目录 = 仓库根（不是 wechat_assistant 里面）",
          os.path.isfile(os.path.join(updater.install_dir(), "run_watch.py")),
          updater.install_dir())
    check("临时工作目录建得出来", os.path.isdir(updater.work_dir()))


def main():
    print("=" * 70)
    print("检查更新 / 自动更新测试")
    print("=" * 70)
    test_version()
    test_apply_script()
    test_latest_stub()
    test_install_dir()
    print("\n" + "=" * 70)
    print("通过 %d，失败 %d" % (PASS, FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
