# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir）。

怎么用
------
    build_exe.bat                 # 或者：
    python -m PyInstaller wechat_ai.spec --noconfirm

产物：`dist/微信AI助手/`，里面 `微信AI助手.exe` 双击即用。
整个文件夹可以压成 zip 发给别人（**不要只发 exe**，见下）。

为什么是 onedir 不是 onefile
---------------------------
onefile 每次启动都要把 ≈250MB 解压到临时目录再运行 —— 实测启动要多等好几秒，
而且这个程序是**常驻托盘**的，用户只启动一次、用一整天，省下的那一个文件夹
并不值得每次开机都慢几秒。onedir 还顺带让 `config.yaml` / `state/` / `logs/`
都明明白白躺在 exe 旁边，出问题用户能自己看。

⚠️ 两个必须记住的点
------------------
1. **`contents_directory='.'`**：PyInstaller 6 默认把 onedir 的所有文件塞进
   `_internal/`，那样 `paths.base_dir()`（= exe 所在目录）旁边就**没有**
   `config.yaml`，程序会当作"配置文件不存在"跑成默认值 —— 静默、不报错。
   写 `.` 就回到 exe 和内容同级的旧布局。
2. **`config.yaml` 是 datas 不是自动带的**：它是"出厂默认"，必须跟着走。
   ⚠️ 但 `secrets.yaml` **绝对不能打进去** —— 那是本机的 API Key。
"""

import os

from PyInstaller.utils.hooks import collect_data_files

# ---------------------------------------------------------------- 环境层面的坑
#
# ⚠️⚠️ **这台机器的 site-packages 里有一个已经不存在的 numpy 版本的 dist-info。**
#
#     实际装的：numpy 2.4.6（`numpy-2.4.6.dist-info`）
#     多出来一个：`numpy-1.26.4.dist-info`（+ 一个 numpy-1.26.4 的 .whl，早先装过一次）
#
#     而 PyInstaller 的 `hook-numpy.py` 第一行就是
#         numpy_version = Version(importlib_metadata.version("numpy"))
#     `importlib_metadata` 只认 dist-info，**不认实际 import 到的是哪一份**，
#     于是它读到 1.26.4，走了 `numpy < 2.0` 那条分支：
#         · 加了不存在的 `numpy.core._dtype_ctypes`（警告一下就算了）
#         · **漏掉 numpy 2.3+ 才需要的 `numpy._core._exceptions`**  ← 要命的是这条
#
#     后果（实测，不是我推测的）：打包版一 `import numpy` 就抛
#         ImportError: No module named 'numpy._core._exceptions'
#     而 run_watch.py 里"发送者判定"整段是包在 try/except 里的 ——
#     **异常被吞，程序照常启动**，只是日志里一行"发送者判定不可用，将跳过"，
#     表现是"群里认不出谁是谁、单聊也判不出发送者"，很难联想到打包。
#
#     修法：**只在本次构建进程内**让那次版本查询返回真实的模块版本。
#     版本本身没撒谎（装的确实是 2.4.6），是 dist-info 撒谎，所以这里补的是
#     "正确的输入"，不是绕过检查。
#     ⚠️ 没有去删那个 dist-info —— 那是用户全局 Python 环境里的东西，
#        为了打个包去动它不划算（而且删错会连累别的项目）。
_np = None
try:
    import numpy as _np
except Exception:
    pass

if _np is not None:
    from PyInstaller import compat as _compat

    _real_version = _compat.importlib_metadata.version

    def _version(name, *a, **kw):
        if name == "numpy":
            return _np.__version__
        return _real_version(name, *a, **kw)

    _compat.importlib_metadata.version = _version
    print("[spec] numpy 版本按实际模块取：%s（dist-info 那边是错的）" % _np.__version__)

HERE = SPECPATH           # PyInstaller 注入的全局量：spec 文件所在的目录
APP_NAME = "微信AI助手"

# ---------------------------------------------------------------- 数据文件
datas = [
    # 出厂默认配置。⚠️ 用户改的是 exe 旁边这一份，程序永不写入它。
    ("config.yaml", "."),
    # 给最终用户看的使用说明。放在 exe 旁边，让他不用来问你。
    ("packaging/使用说明.txt", "."),
    # 程序图标：任务栏 / 标题栏 / Alt-Tab / 托盘都用它。
    # ⚠️ 走 paths.icon_path() → pkg_dir()，所以**必须放在包目录下面**，
    #    不能像 config.yaml 那样放根目录。
    ("wechat_assistant/assets", "wechat_assistant/assets"),
]

# OCR 模型 + 它自己的 config.yaml。
# ⚠️ `ocr.py` 是**按 os.path.dirname(rapidocr_onnxruntime.__file__) 去找
#    models/*.onnx 和 config.yaml 的** —— 不把这两个一起打进去，
#    群聊昵称和图片文字识别会在运行时抛 FileNotFoundError，
#    而"发送者判定不可用"是被 except 吞掉的，表现只是"群里认不出谁是谁"。
datas += collect_data_files("rapidocr_onnxruntime")

# ---------------------------------------------------------------- 隐藏导入
hiddenimports = [
    # 全是**懒加载**的：写在函数体内、或包在 try/except 里，
    # 静态分析可能看不全，而漏掉的后果是"功能静默不可用"而不是启动报错。
    #
    # ⚠️ 我们自己包里的懒加载模块也列上（2026-10-13）：`run_watch` 是在
    #    `single_instance_guard()` 里才 `from wechat_assistant import single_instance`。
    #    漏掉的症状很隐蔽：**打包后第二次双击 exe 什么都不发生**
    #    （单实例判不出来 = 又开一个，或者唤不出原窗口），而不是报错。
    "wechat_assistant.single_instance",
    # ⚠️ 标题栏那颗齿轮图标是**用户给的 SVG**（`wechat_assistant/assets/gear.svg`），
    #    由 `panel._gear_pixmap()` 用 `QtSvg` 渲染。这一条是**懒加载**（函数里 import），
    #    静态分析不一定看得到；漏掉的症状很隐蔽：**图标静默变成自绘那个**
    #    （日志里只有一行"齿轮图标：用不了 SVG"）。
    "PySide6.QtSvg",
    "uiautomation",
    "comtypes",
    "rapidocr_onnxruntime",
    "onnxruntime",
    "cv2",
    "win32gui",
    "win32api",
    "win32con",
    "win32process",
    "win32ui",
    "pythoncom",
    "pywintypes",
    "win32timezone",
]

# comtypes 的 gen 模块是**运行时生成**的（`comtypes.client.GetModule`）。
# 把已经生成好的那份一起打进去，冻结态就不用现场生成 —— 现场生成也是能成的
# （comtypes 会回落到 %TEMP%\comtypes_cache\...），但那要多花一秒左右，
# 而且依赖 UIAutomationCore.dll 能被 typer 读出来。两条路都留着更稳。
try:
    from PyInstaller.utils.hooks import collect_submodules

    hiddenimports += collect_submodules("comtypes.gen")
except Exception:
    pass

# ---------------------------------------------------------------- 排除
# 本机 site-packages 里装了一大堆跟本程序无关的重家伙（torch / streamlit /
# langchain 都在）。它们不会被 import，正常也进不来；这里显式排掉是为了
# **万一某个 hook 顺手拉了进来**时能立刻看见，而不是打包体积莫名其妙多 2GB。
excludes = [
    "tkinter",
    "torch",
    "torchvision",
    "streamlit",
    "langchain",
    "langchain_core",
    "matplotlib",
    "pandas",
    "scipy",
    "IPython",
    "notebook",
    "pytest",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6.Qt3DCore",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtNetworkAuth",
    "PySide6.QtNfc",
    "PySide6.QtPdf",
    "PySide6.QtPositioning",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtTest",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    "PySide6.QtXml",
]

# ---------------------------------------------------------------- 构建

a = Analysis(
    ["run_watch.py"],
    pathex=[HERE],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# ---------------------------------------------------------------- 排掉用不到的 DLL
#
# ⚠️⚠️ **PyInstaller 找依赖是沿 PATH 找的** —— 本机装了什么，它就可能把什么打进包。
#     实测（2026-10-13）：它把
#         C:\Program Files\MySQL\MySQL Server 8.0\bin\libcrypto-3-x64.dll
#                                             \libssl-3-x64.dll      （5.7MB）
#     当成依赖收了进来，而 Python 自己那份 `libcrypto-3.dll` / `libssl-3.dll`
#     也在包里 —— 等于**两份 OpenSSL**，还随包分发 MySQL 的那一对
#     （版本、许可证都不该跟着走，加载顺序不对还可能顶掉 Python 自己那份）。
#     ⚠️ 这类"别人的 DLL"只能**按名字排掉**，`excludes`（那是给 Python 模块用的）管不到。
#
# ⚠️ `opencv_videoio_ffmpeg*.dll`（25.2MB）是 cv2 的**视频**编解码：这程序只截静态图，
#     `cv2.VideoCapture` 一行都没用，而它是**按需加载**的（不在 import 时读），
#     所以排掉不影响截图、连通域分析和 OCR。
#
# 判据（每次改这个名单都要跑一遍验收）：
#     build_exe.bat → python packaging\make_zip.py → python packaging\verify_zip.py
#     —— verify_zip 会在日志里找「OCR 引擎已加载」「悬浮窗已创建」这类
#        **只在真跑到那一步才打印**的词，并确认没有 ImportError。
DROP_DLLS = (
    "opencv_videoio_ffmpeg",
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
    # ---- 2026-10-15：都是「产品代码根本走不到」的东西，逐条给了判据 ----
    #
    # ⚠️ 判据一律是「**产品代码里有没有那条调用路径**」，不是「体积大就砍」。
    #    少一个 DLL 的典型症状是**某个功能静默不可用**，所以每加一条都要重跑
    #    build_exe.bat → make_zip.py → verify_zip.py。
    #
    # `PySide6/translations/*.qm`（6.4MB / 96 个文件）：**本程序从不加载翻译文件**。
    #     证据是产品代码自己的注释（`qt_ui/settings_ui.py` 模态对话框那一段）：
    #     它**故意不用** `QMessageBox.question()` 这类静态便捷方法，
    #     因为"按钮文案由 Qt 的翻译文件决定"，而这个程序没加载 `qtbase_zh_CN.qm`
    #     —— 所以是**手工建 QMessageBox 并把按钮文字写死**的。
    #     全项目 grep 不到 `loadTranslator` / `installTranslator`，这些 .qm 一个都用不上。
    "pyside6/translations/",
    # `Qt6Network.dll` + `QtNetwork.pyd` + `plugins/tls` + `plugins/networkinformation`
    #     （≈3.4MB）：网络请求全走**标准库 `urllib`**（`ai.py` / `settings.py` 里
    #     各有一处，`ai.py` 的文件头还专门写了"只用标准库 urllib，不引入 requests"）。
    #     Qt 的网络栈只服务于 QNetworkAccessManager，产品代码里没有。
    "qtnetwork",
    "pyside6/plugins/tls/",
    "pyside6/plugins/networkinformation/",
    # 只保留真正会被读的图片格式插件（`qico` 给 icon.ico、`qsvg` 给 gear.svg，
    #     PNG 是 Qt 内建的）。jpeg/webp/tiff/gif/icns/tga/wbmp 全都不会读：
    #     本程序显示的只有自绘控件里的文字，没有任何外部图片资源。
    "imageformats/qjpeg",
    "imageformats/qwebp",
    "imageformats/qtiff",
    "imageformats/qgif",
    "imageformats/qicns",
    "imageformats/qtga",
    "imageformats/qwbmp",
    # `qdirect2d.dll`（1.0MB）：direct2d 平台插件，只有显式
    #     `QT_QPA_PLATFORM=direct2d` 才会加载；Windows 上默认是 `qwindows`。
    "qdirect2d",
    # ⚠️ **能省但没做**：`PySide6/opengl32sw.dll`（19.7MB，软件 OpenGL 兜底）。
    #     本程序只用 QWidget，不用 QtQuick / QOpenGLWidget，理论上走不到它；
    #     但"虚拟机 / 没装显卡驱动的机器白屏"是它的典型症状，而这类机器
    #     **恰恰是下载来试用的人最可能用的**。19.7MB 换"一定不白屏"，
    #     在这个体积量级上不划算 —— 留着。要砍的话先在一台无显卡驱动的
    #     虚机上跑 verify_zip，别只在本机试。
)


def _drop_dlls(entries):
    kept, dropped = [], []
    for e in entries:
        low = e[0].lower().replace("\\", "/")
        if any(d in low for d in DROP_DLLS):
            dropped.append(e[0])
        else:
            kept.append(e)
    if dropped:
        print("[spec] 排掉用不到的 DLL（省体积 / 不夹带别人的库）: %s" % ", ".join(dropped))
    return kept


a.binaries = _drop_dlls(a.binaries)
a.datas = _drop_dlls(a.datas)

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # 本机没有 UPX；有也不建议对 Qt/onnxruntime 用
    console=False,           # ⚠️ 无控制台。run_watch.py 会把 print 接到 logs/app.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="wechat_assistant/assets/icon.ico",
    # ⚠️ 见文件头第 1 条：写 '.' 才能让 config.yaml 落在 exe 旁边
    contents_directory=".",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
