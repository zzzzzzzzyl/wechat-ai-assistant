# -*- coding: utf-8 -*-
"""把 `dist/<名字>/` 压成一个可以直接发给别人的 zip。

    python packaging/make_zip.py

为什么不用 `Compress-Archive` / 右键"压缩"
----------------------------------------
压缩包里含有中文文件名（`微信AI助手.exe`、`使用说明.txt`）。
`Compress-Archive` 在中文 Windows 上会按系统代码页写文件名，
对方解压出来就是乱码；Python 的 `zipfile` 默认按 UTF-8 写并置好标志位，
WinRAR / 7-Zip / 资源管理器都能正确还原。

另一件事：**压缩包里要带上一层目录**（`微信AI助手/...`），
否则对方解压时会把 200 多个文件直接倒进他当前的文件夹里 —— 非常难收拾。
"""

import hashlib
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(ROOT, "dist")

# 和 wechat_ai.spec 里的 APP_NAME 保持一致
APP_NAME = "微信AI助手"

#: 发 GitHub Release 用（2026-10-15 加）。⚠️ **资源名固定不变**是故意的：
#:  站点上那个下载按钮指向 `releases/latest/download/<资源名>`，
#:  名字里带版本号的话，每发一版都得回去改页面。
GITHUB_REPO = "zzzzzzzzyl/wechat-ai-assistant"
ASSET_NAME = "wechat-ai-assistant-win64.zip"


def _version() -> str:
    """程序自己的版本号（`wechat_assistant.__version__`）—— 别在这里再写一份。"""
    try:
        sys.path.insert(0, ROOT)
        from wechat_assistant import __version__

        return __version__
    except Exception:
        return "?"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()



def main() -> int:
    src = os.path.join(DIST, APP_NAME)
    if not os.path.isdir(src):
        print("[zip] 找不到 %s，先跑一次 build_exe.bat" % src)
        return 1

    # ⚠️ 打包前**删掉运行过才会有的东西** —— 否则会把本机的会话名、
    #    聊天记录、日志一起发给别人（`.db` 里是真的聊天内容）。
    for junk in ("logs", "state", "secrets.yaml", "secrets.yml"):
        p = os.path.join(src, junk)
        if os.path.isdir(p):
            print("[zip] 清掉运行数据目录 %s（里面是本机的聊天记录）" % junk)
            import shutil

            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            print("[zip] 清掉 %s" % junk)
            os.remove(p)

    # 兜底再扫一遍：任何 .db / .log 都不该出现在发给别人的包里
    leftovers = []
    for dirpath, _dirs, files in os.walk(src):
        for f in files:
            if f.endswith((".db", ".db-wal", ".db-shm", ".bak")) or f.endswith(".log"):
                leftovers.append(os.path.join(dirpath, f))
    for p in leftovers:
        print("[zip] 清掉残余数据 %s" % os.path.relpath(p, src))
        os.remove(p)

    out = os.path.join(DIST, "%s.zip" % APP_NAME)
    n = 0
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dirpath, _dirs, files in os.walk(src):
            for f in files:
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, src)
                # 带上一层目录：对方解压出来是一个干净的文件夹
                arc = os.path.join(APP_NAME, rel)
                z.write(full, arc)
                n += 1
                total += os.path.getsize(full)

    size = os.path.getsize(out)
    print("[zip] %d 个文件  %.1f MB  ->  %.1f MB" % (n, total / 1048576, size / 1048576))
    print("[zip] 产物：%s" % out)
    print("[zip] 校验：")
    with zipfile.ZipFile(out) as z:
        bad = z.testzip()
        print("      CRC 全部正确" if bad is None else "      **坏文件：%s**" % bad)
        names = z.namelist()
        for must in ("%s/%s.exe" % (APP_NAME, APP_NAME),
                     "%s/config.yaml" % APP_NAME,
                     "%s/使用说明.txt" % APP_NAME):
            print("      %-40s %s" % (must, "在" if must in names else "**不在**"))

    # ------------------------------------------------------------ 发布信息
    # ⚠️ 发版时**照抄这一段**，别再手算 sha256（手抄三处必然漂）：
    #    GitHub Release 那个资源名 + 站点上 `projects/wxai.md` 的三行。
    digest = _sha256(out)
    ver = _version()
    print("")
    print("[zip] 发布信息（照抄，别手算）")
    print("      版本   : v%s" % ver)
    print("      资源名 : %s   ← 上传到 Release 时改成这个名字" % ASSET_NAME)
    print("      大小   : %.1f MiB（%d 字节）" % (size / 1048576, size))
    print("      SHA256 : %s" % digest)
    print("      直链   : https://github.com/%s/releases/latest/download/%s"
          % (GITHUB_REPO, ASSET_NAME))
    print("      页面   : portfolio/src/content/projects/wxai.md"
          " 的 version / fileSize / sha256 三行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
