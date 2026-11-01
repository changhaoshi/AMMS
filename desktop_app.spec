# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（AStockDashboard）

产物：dist/AStockDashboard.app（macOS）/ dist/AStockDashboard（Windows/Linux）

关键点：
  · index.html 与 vendor/ 需随包分发 —— 但 index.html 里 ECharts 已内联，
    因此 vendor 仅在需要重新生成页面时才有；仍一并打包以备万一。
  · .cache 缓存目录不进包 —— 运行时会重定向到用户数据目录。
"""

import os
import sys

block_cipher = None

BASE = os.path.abspath(SPECPATH)

datas = [
    (os.path.join(BASE, "index.html"), "."),
    (os.path.join(BASE, "vendor"), "vendor"),
]

# pywebview 各平台 GUI 后端依赖完全不同，必须按目标平台条件化，
# 否则会出现两种失败：
#   · 在 macOS 上打包时若列入 Windows 专有的 clr → 报 ModuleNotFound
#   · 在 Windows 上打包时若只有 cocoa → 运行时找不到 edgechromium 后端
#
#   macOS   → cocoa   : objc / AppKit / Foundation / WebKit / PyObjCTools
#   Windows → edgechromium（首选，WebView2）/ winforms（备选，IE 内核）: clr / System / winreg
#   Linux   → gtk     : gi（PyGObject，需系统级 GTK 开发库）
if sys.platform == "darwin":
    gui_backends = [
        "webview.platforms.cocoa",
        "objc",
        "Foundation",
        "AppKit",
        "WebKit",
        "Quartz",
        "PyObjCTools",
        "UniformTypeIdentifiers",
    ]
elif sys.platform == "win32":
    gui_backends = [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr",
        "System",
        "winreg",
    ]
elif sys.platform.startswith("linux"):
    gui_backends = [
        "webview.platforms.gtk",
        "gi",
    ]
else:
    gui_backends = []

hiddenimports = [
    "webview",
    "bottle",
] + gui_backends

a = Analysis(
    [os.path.join(BASE, "desktop_app.py")],
    pathex=[BASE],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 减小体积：这些用不到
        "tkinter", "matplotlib", "numpy", "pandas", "PIL",
        "test", "unittest", "pydoc_data",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AStockDashboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AStockDashboard",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="A股实时看盘.app",
        icon=None,
        bundle_identifier="com.workbuddy.astock.dashboard",
        info_plist={
            "CFBundleName": "A股实时看盘",
            "CFBundleDisplayName": "A股实时看盘",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.13",
            "NSAppTransportSecurity": {
                # 允许访问明文 HTTP（本应用只访问 127.0.0.1 本地服务）
                "NSAllowsLocalNetworking": True,
            },
        },
    )
