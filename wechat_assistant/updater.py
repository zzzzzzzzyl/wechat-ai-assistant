"""检查更新 / 自动更新（2026-11-09 加）。

⚠️ 只对**打包版**有意义：源码版更新就是 `git pull`，这里只做版本检查并提示。
⚠️ 更新源是 GitHub Release：`releases/latest` 的 zip 资产。国内网络可能连不上 ——
   所以**每一个失败都要能说人话**，并且给"手动去 releases 页"的退路。

## 为什么不能"自己覆盖自己"

Windows 上**正在运行的 exe 是锁住的**，没法把自己覆盖掉。所以流程是：

1. 下载 zip 到 `%TEMP%\\wxai_update\\`；
2. 解压到 `%TEMP%\\wxai_update\\stage\\`；
3. 跑一次 **新 exe `--version`**，确认它就是我们要的那个版本（防止下到坏包/半个包）；
4. 用 **PowerShell 的 `-EncodedCommand`**（UTF-16LE + base64，**纯 ASCII**）拉起一个
   后台脚本：等旧进程退出 → `Copy-Item` 覆盖安装目录 → 启动新 exe；
5. 本进程退出。

⚠️ 为什么用 `-EncodedCommand` 而不是写 `.bat`：安装目录里有中文（`微信AI助手`），
   `.bat` 在 GBK 代码页下会把中文路径解析坏（CLAUDE.md 里也写了"`.bat` 必须纯 ASCII"）。
   `-EncodedCommand` 传的是 base64，**跟代码页无关**。
⚠️ 覆盖用 `Copy-Item -Recurse -Force`（只覆盖、**不删除**)：`state/` `logs/` `secrets.yaml`
   这些用户数据不在 zip 里，绝不能被"同步"掉。
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

#: GitHub 仓库（公开仓库）。
REPO = "zzzzzzzzyl/wechat-ai-assistant"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases" % REPO

#: 打包产物里 exe 的文件名（`packaging/make_zip.py` 用的也是它）。
EXE_NAME = "微信AI助手.exe"

#: Release 里那个 zip 资产的**约定名**。
#:
#: ⚠️ 它就是 `packaging/make_zip.py` 提示"上传时改成这个名字"的那个名字。
#:    改这里的唯一理由：发布时资产名也一起改 —— 否则 `_latest_redirect()`
#:    拼出来的下载地址会 404（API 那条路不受影响，因为 API 直接给地址）。
ZIP_NAME = "wechat-ai-assistant-win64.zip"

#: 下载/查询的超时（秒）。查询要短，下载要长（128MB）。
API_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 60

#: 速度闸：开了 `STALL_AFTER_S` 秒之后，平均速度还低于 `STALL_MIN_BYTES_PER_S`
#: 就掐掉换下一个源。
#:
#: ⚠️ 为什么要有它：直连 GitHub 实测 0.23 MB/s，128MB 要 9 分钟；
#:    而一条"几乎不动"的进度条用户只会以为卡死了。宁可换镜像重下。
#: ⚠️ 200KB/s 是宽松值（ghfast.top 实测 0.69MB/s 能过、ghproxy.net 0.03MB/s 会被掐）。
STALL_AFTER_S = 8.0
STALL_MIN_BYTES_PER_S = 200 * 1024


def parse_version(text: str):
    """"v0.2.3" / "0.2.3" / "v0.2.3-beta" -> (0, 2, 3)。解析不出返回 ()。"""
    s = (text or "").strip().lstrip("vV")
    for sep in ("-", "+", " "):
        s = s.split(sep)[0]
    parts = []
    for p in s.split("."):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts)


def is_newer(remote: str, local: str) -> bool:
    """远端的版本是不是比本地新。解析不出来一律判否（宁可不更新）。"""
    r, l = parse_version(remote), parse_version(local)
    if not r or not l:
        return False
    n = max(len(r), len(l))
    return r + (0,) * (n - len(r)) > l + (0,) * (n - len(l))


#: 查版本时的**多源兜底**（2026-11-09 用户追问「检查更新是不是没有加速」——原来确实没有）。
#:
#: 每项是 `(前缀, 方式)`：`api` = 调 `api.github.com`；`redirect` = 读
#: `github.com/<repo>/releases/latest` 的跳转。前缀加在地址前面（镜像）。
#:
#: ⚠️ 实测（本机，Python urllib 口径，2026-11-09）：
#:    · 直连 API 1.3s ✓；`gh-proxy.com` 代理 API 1.1s ✓（**最快**，且能拿到 size）；
#:    · 直连跳转 1.9s ✓；`ghproxy.net` 代理跳转 2.4s ✓；
#:    · `ghfast.top` 只有**下载**能用，查版本会 "Remote end closed" ✗
#:      （curl 能过、urllib 不行 —— 所以它只留在下载镜像里，不进这张表）；
#:    · `gh-proxy.com` 代理**跳转**拿不到 tag（它不重定向）✗。
#:    顺序就是按这张表排的。
LATEST_SOURCES = [
    ("", "api"),
    ("https://gh-proxy.com/", "api"),
    ("", "redirect"),
    ("https://ghproxy.net/", "redirect"),
]


def latest_sources(custom: str = "") -> list:
    """查版本的源顺序（`custom` 非空就排最前面）。"""
    out = list(LATEST_SOURCES)
    c = (custom or "").strip()
    if c:
        if not c.endswith("/"):
            c += "/"
        out.insert(0, (c, "api"))
    return out


def latest(timeout: float = API_TIMEOUT, mirror: str = "") -> dict:
    """查最新 Release（**多源依次试**）。返回 dict；全失败才抛异常。

    字段：`version`（tag 去掉 v）、`tag`、`url`（zip 下载地址）、`size`、`published`。

    ⚠️ 每个源给 `timeout` 秒；一个不行就换下一个 —— 顺序见 `LATEST_SOURCES`。
       `gh-proxy.com` 那条能代理 API（拿到完整信息、含 size）；
       跳转那几条拿不到 `size`（填 0），下载照样能下。
    """
    errs = []
    for prefix, kind in latest_sources(mirror):
        try:
            if kind == "api":
                info = _latest_api(timeout, prefix=prefix)
            else:
                info = _latest_redirect(timeout, prefix=prefix)
            if prefix:
                _log_note("查版本走的镜像：%s（%s）" % (prefix, kind))
            return info
        except Exception as e:
            errs.append("%s%s: %s" % (prefix or "直连", kind, str(e)[:60]))
            _log_note("这个源查不到，换下一个：%s" % errs[-1])
    raise RuntimeError("所有更新源都查不到：" + " | ".join(errs[-2:]))


def _latest_api(timeout: float, prefix: str = "") -> dict:
    req = urllib.request.Request(prefix + API_URL, headers={
        "User-Agent": "wechat-ai-assistant-updater",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    assets = data.get("assets") or []
    zip_asset = None
    for a in assets:
        if str(a.get("name", "")).lower().endswith(".zip"):
            zip_asset = a
            break
    if zip_asset is None:
        raise RuntimeError("这个版本没有 zip 包")
    return {
        "version": (data.get("tag_name") or "").lstrip("vV"),
        "tag": data.get("tag_name") or "",
        "url": zip_asset.get("browser_download_url") or "",
        "size": int(zip_asset.get("size") or 0),
        "published": (data.get("published_at") or "")[:10],
    }


def _latest_redirect(timeout: float, prefix: str = "") -> dict:
    """不调 API，用 `github.com/<repo>/releases/latest` 的 **302 跳转**拿 tag。

    ⚠️ 资产名是**约定**的（`wechat-ai-assistant-win64.zip`，见 `ZIP_NAME`），
       所以能直接拼出下载地址 —— 这也是为什么发布时那个名字不能改。
    ⚠️ 走镜像时最终 URL 会带前缀（`https://ghfast.top/https://github.com/.../tag/v0.2.2`），
       取**最后一段**照样是 tag —— 所以前缀在开头不影响解析。
    """
    url = prefix + "https://github.com/%s/releases/latest" % REPO
    req = urllib.request.Request(url, headers={
        "User-Agent": "wechat-ai-assistant-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        final = r.geturl() or ""
    # ⚠️ 必须**看到 `/releases/tag/`** 才认 —— 否则（比如被重定向到 releases 首页、
    #    或者被运营商劫持到一个普通页面）会把路径最后一段当版本号，那是瞎猜。
    if "/releases/tag/" not in final:
        raise RuntimeError("拿不到最新版本号")
    tag = final.rstrip("/").rsplit("/", 1)[-1]
    if not tag or tag == "latest":
        raise RuntimeError("拿不到最新版本号")
    return {
        "version": tag.lstrip("vV"),
        "tag": tag,
        "url": "https://github.com/%s/releases/download/%s/%s" % (REPO, tag, ZIP_NAME),
        "size": 0,
        "published": "",
    }


def download(url: str, dest: str, on_progress=None,
             timeout: float = DOWNLOAD_TIMEOUT, mirrors=None) -> str:
    """下载到 `dest`，**失败了自动换镜像**。返回 dest。

    ⚠️ 2026-11-09 用户问「是否有用 github 加速」—— 原来**没有**，直连。
       本机实测 128MB 那个包：
         · 直连 GitHub      0.23 MB/s  ≈ 9 分钟
         · ghfast.top 镜像  0.69 MB/s  ≈ 3 分钟   ← 快 3 倍
         · ghproxy.net      0.03 MB/s  ≈ 70 分钟（等于不可用）
       所以默认顺序是「直连 → 镜像」，并且带**速度闸**：
       开下 8 秒还没到 200KB/s 就掐掉换下一个（否则用户会盯着一条几乎不动的进度条）。
    ⚠️ 镜像只是**在 URL 前面加一段前缀**（`https://ghfast.top/https://github.com/...`），
       不改 URL 本身 —— 所以任何一个前缀都能用，用户也可以填自己的（`update.mirror`）。
    ⚠️ 换镜像时**从头下**（不能接着下：不同镜像的分段不保证一致）。
    """
    order = list(mirrors) if mirrors is not None else default_mirrors()
    if "" not in order:
        order.insert(0, "")          # 永远先试直连（直连最快时别绕）
    # ⚠️ 目录在这里建一次（`_download_one` 只负责写文件；测试会打桩它，
    #    要是目录只在桩里建，桩就不需要知道这件事 —— 少一个坑）。
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    last_err = None
    for prefix in order:
        target = prefix + url if prefix else url
        try:
            _download_one(target, dest, on_progress, timeout)
            if prefix:
                _log_note("下载走的镜像：%s" % prefix)
            return dest
        except Exception as e:
            last_err = e
            _log_note("这个源不行（%s），换下一个：%s"
                      % (prefix or "直连", str(e)[:80]))
    raise RuntimeError("所有下载源都失败：%s" % (str(last_err)[:120]))


#: 下载时试着加在前面的**加速前缀**（`update.mirror` 填了就当第一个）。
#:
#: ⚠️ 这些是第三方服务，随时可能挂/变慢 —— 所以是"**依次试**"而不是"必须用"，
#:    而且**直连永远排第一**（本机实测直连有时反而更快）。
#: ⚠️ 2026-11-09 实测（本机、那个 128MB 的包）：ghfast.top 0.69MB/s、
#:    ghproxy.net 0.03MB/s（不可用）。所以 ghproxy.net 放最后当兜底。
MIRROR_PREFIXES = [
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
]


def default_mirrors(custom: str = "") -> list:
    """默认的镜像顺序（`custom` 非空就放最前面）。返回带前导空串（= 直连）的列表。"""
    out = [""]
    c = (custom or "").strip()
    if c:
        if not c.endswith("/"):
            c += "/"
        if c not in out:
            out.append(c)
    for p in MIRROR_PREFIXES:
        if p not in out:
            out.append(p)
    return out


def _log_note(msg: str) -> None:
    """给更新过程的日志（没配 logger 就 print，源码版调试时看得见）。"""
    try:
        print("[更新] %s" % msg, flush=True)
    except Exception:
        pass


def _download_one(url: str, dest: str, on_progress, timeout: float) -> None:
    """下一个源。带**速度闸**：8 秒内没到 200KB/s 就当它不行。"""
    import time

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    req = urllib.request.Request(url, headers={
        "User-Agent": "wechat-ai-assistant-updater"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if on_progress:
                try:
                    on_progress(got, total)
                except Exception:
                    pass
            el = time.monotonic() - t0
            if el >= STALL_AFTER_S and got / el < STALL_MIN_BYTES_PER_S:
                raise RuntimeError("太慢（%.2f MB/s）" % (got / el / 1048576))
    if os.path.getsize(dest) < 1024:
        raise RuntimeError("下载下来的文件太小（可能被拦截了）")


def extract(zip_path: str, into: str) -> str:
    """解压到 `into`，返回**含 exe 的那层目录**（zip 里带一层 `微信AI助手/`）。"""
    if os.path.isdir(into):
        import shutil

        shutil.rmtree(into, ignore_errors=True)
    os.makedirs(into, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError("压缩包损坏（%s）" % bad)
        z.extractall(into)
    for dirpath, _dirs, files in os.walk(into):
        if EXE_NAME in files:
            return dirpath
    raise RuntimeError("压缩包里没找到 %s" % EXE_NAME)


def staged_version(app_dir: str, timeout: float = 30) -> str:
    """跑一次**解压出来的** exe 的 `--version`，返回它报的版本（失败抛异常）。

    ⚠️ 这一步是"半个包/坏包"的唯一防线：解压成功不代表能跑，
       而覆盖安装目录之后就跑不了 = 用户的程序没了。
    ⚠️⚠️ **必须让它把版本写进文件**，不能靠 stdout：打包版是 `console=False`，
       `print()` 会抛异常、PyInstaller 弹崩溃框然后**挂住**（实测挂 5 分钟）。
       见 `run_watch.py` 里 `--version-out` 的说明。
    """
    exe = os.path.join(app_dir, EXE_NAME)
    if not os.path.isfile(exe):
        raise RuntimeError("解压目录里没有 exe")
    out_file = os.path.join(work_dir(), "staged_version.txt")
    try:
        if os.path.exists(out_file):
            os.remove(out_file)
    except OSError:
        pass
    try:
        subprocess.run([exe, "--version", "--version-out", out_file],
                       capture_output=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError("新版 exe 启动超时（可能起不来）")
    text = ""
    try:
        with open(out_file, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        pass
    if not text:
        raise RuntimeError("新版 exe 没报出版本号（可能起不来）")
    return text.splitlines()[-1].strip()


def _ps_quote(path: str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def apply_script(pid: int, app_dir: str, install_dir: str, exe_path: str) -> str:
    """生成并返回要执行的 PowerShell 脚本（纯文本，调用方负责编码）。"""
    return (
        "$ErrorActionPreference = 'Stop'\n"
        # 等旧进程退出（最多 60 秒，超时就放弃 —— 别在用户还开着的时候硬覆盖）
        "$deadline = (Get-Date).AddSeconds(60)\n"
        "while (Get-Process -Id %d -ErrorAction SilentlyContinue) {\n"
        "  if ((Get-Date) -gt $deadline) { exit 1 }\n"
        "  Start-Sleep -Milliseconds 500\n"
        "}\n"
        "Start-Sleep -Milliseconds 800\n"
        "Copy-Item -Path (Join-Path %s '*') -Destination %s -Recurse -Force\n"
        "Start-Process -FilePath %s\n"
    ) % (int(pid), _ps_quote(app_dir), _ps_quote(install_dir), _ps_quote(exe_path))


def spawn_apply(pid: int, app_dir: str, install_dir: str, exe_path: str) -> None:
    """拉起后台 PowerShell 做"等退出 → 覆盖 → 重启"。本进程随后应当退出。"""
    script = apply_script(pid, app_dir, install_dir, exe_path)
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    flags = 0
    for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS"):
        flags |= int(getattr(subprocess, name, 0))
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", enc],
        creationflags=flags, close_fds=True)


def work_dir() -> str:
    """更新用的临时目录（`%TEMP%\\wxai_update`）。"""
    d = os.path.join(tempfile.gettempdir(), "wxai_update")
    os.makedirs(d, exist_ok=True)
    return d


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_dir() -> str:
    """当前程序所在目录（打包版 = exe 旁边；源码版 = 仓库目录）。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)
