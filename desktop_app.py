# -*- coding: utf-8 -*-
"""
桌面应用壳（原型验证版）

架构：
    [pywebview 窗口 / WKWebView]
              │  http://127.0.0.1:<port>
              ▼
    [内置 HTTP 服务 · 复用 server.py 的 Handler]
              │
              ▼
    [data_sources → 腾讯/新浪/东财/同花顺]

要点：
  1. 后端跑在后台线程，窗口关闭时通过 on_closed 回调优雅关闭
  2. 打包后资源定位走 sys._MEIPASS，缓存写到用户数据目录
"""
import os
import sys
import socket
import threading
import time
import urllib.parse

# --------------------------------------------------- 打包环境适配

def _resource_root() -> str:
    """资源根目录：打包后是 PyInstaller 的解包目录，开发时是脚本所在目录。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _user_data_dir() -> str:
    """可写目录：缓存、配置放这里，不能写进 bundle（只读且会被清理）。"""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    d = os.path.join(base, "AStockDashboard")
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------- 端口

def pick_port(preferred: int = 8770) -> int:
    """优先用 preferred，被占用则自动找一个空闲端口。

    注意 SO_REUSEADDR 的跨平台差异：
      · Unix  —— 允许 bind 到处于 TIME_WAIT 的地址（安全）
      · Windows —— 语义相反！允许**两个 socket 绑定同一端口**，
        导致多实例时请求被随机分发到其中一个，行为不确定。
    因此 Windows 下不设该选项（Windows 默认即允许重用 TIME_WAIT），
    让「端口已被占用」正确表现为 bind 失败。
    """
    for p in (preferred, 0):
        s = socket.socket()
        if sys.platform != "win32":
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except OSError:
                pass
        try:
            s.bind(("127.0.0.1", p))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            s.close()
            continue
    return preferred


def _log(msg: str) -> None:
    """写文件日志。

    打包后 console=False，stdout 不可见 —— 排障只能靠日志。
    """
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line)
    except Exception:
        pass
    try:
        with open(os.path.join(_user_data_dir(), "app.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _shutdown(httpd) -> None:
    """优雅关闭 HTTP 服务（可重复调用）。"""
    try:
        httpd.shutdown()
    except Exception:
        pass
    try:
        httpd.server_close()
    except Exception:
        pass


# --------------------------------------------------- 系统版本检测

# Windows Build Number → 系统版本
#   7601  = Windows 7
#   9600  = Windows 8.1
#   10240 = Windows 10 1507（最低支持线）
#   22000 = Windows 11 21H2
_WIN_BUILD_NAMES = (
    (22000, "Windows 11"),
    (19041, "Windows 10 2004+"),
    (17763, "Windows 10 1809+"),
    (17134, "Windows 10 1803+"),
    (16299, "Windows 10 Fall Creators"),
    (15063, "Windows 10 1703+"),
    (14393, "Windows 10 1607+"),
    (10586, "Windows 10 1511"),
    (10240, "Windows 10 1507"),
    (9600, "Windows 8.1"),
    (7601, "Windows 7"),
    (0, "更早版本"),
)


def detect_windows_version():
    """返回 (版本名, build号, 是否受支持)。非 Windows 返回 (None, 0, True)。"""
    if sys.platform != "win32":
        return None, 0, True
    build = 0
    try:
        import winreg
        for hive, path in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"),
        ):
            try:
                k = winreg.OpenKey(hive, path)
                build = int(winreg.QueryValueEx(k, "CurrentBuildNumber")[0])
                break
            except OSError:
                continue
    except Exception:
        pass
    if not build:
        return "Windows（版本未知）", 0, True
    name = next(n for b, n in _WIN_BUILD_NAMES if build >= b)
    return name, build, build >= 10240


# --------------------------------------------------- 环境自检

def _selftest() -> int:
    """打印运行环境与依赖自检报告。

    用于在其他平台（如 Windows）验证打包产物是否可用。
    退出码 0 = 全部通过；1 = 有阻断项。

    注意：打包成无控制台 GUI（console=False）后 sys.stdout 为 None，
    窗口模式下看不到任何输出 —— 因此报告同时写入用户数据目录的
    selftest.txt，CI / 排障时可直接读文件断言。
    """
    import platform

    ok = True
    lines = []

    def out(s=""):
        lines.append(s)

    out("=" * 62)
    out("  A股实时看盘 · 环境自检")
    out("=" * 62)

    out("[1] 运行环境")
    out("    Python   : %s" % sys.version.split()[0])
    out("    平台     : %s (%s)" % (platform.platform(), platform.machine()))
    out("    打包状态 : %s" % ("已打包" if getattr(sys, "frozen", False) else "源码运行"))
    out("    资源目录 : %s" % _resource_root())
    out("    缓存目录 : %s" % _user_data_dir())

    out("")
    out("[2] GUI 后端（pywebview）")
    try:
        import webview
        out("    pywebview : %s" % getattr(webview, "__version__", "已安装"))
        cands = {
            "darwin": ["cocoa"],
            "win32": ["edgechromium", "winforms"],
        }.get(sys.platform, ["gtk", "qt"])
        for name in cands:
            try:
                __import__("webview.platforms.%s" % name)
                out("    后端 %-14s ✅ 可用" % name)
            except Exception as e:
                out("    后端 %-14s ❌ %s" % (name, str(e)[:60]))
                ok = False
    except Exception as e:
        out("    ❌ pywebview 未安装: %s" % str(e)[:60])
        ok = False

    out("")
    out("[3] 系统版本")
    wname, wbuild, wok = detect_windows_version()
    if wname:
        out("    Windows  : %s (Build %d)" % (wname, wbuild))
        if not wok:
            out("    ❌ 不受支持 —— 本应用最低要求 Windows 10 (Build 10240)")
            out("                 原因：Python 3.9+ 已不支持 Win7/8.1；")
            out("                       WebView2 Runtime 自 2023-01-10 起停止对 Win7/8.1 官方支持")
            ok = False
        else:
            out("    ✅ 受支持")
    else:
        out("    系统     : ✅ 受支持")

    out("")
    out("[4] 系统 WebView 依赖")
    if sys.platform == "darwin":
        fw = "/System/Library/Frameworks/WebKit.framework"
        out("    WKWebView : %s" % ("✅ 已内置" if os.path.isdir(fw) else "❌ 缺失"))
        ok = ok and os.path.isdir(fw)
    elif sys.platform == "win32":
        # WebView2 Runtime 注册表检查
        found = False
        try:
            import winreg
            for key in (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                        r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"):
                try:
                    k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key)
                    v, _ = winreg.QueryValueEx(k, "pv")
                    if v:
                        found = True
                        out("    WebView2   : ✅ 已安装 (v%s)" % v)
                        break
                except OSError:
                    continue
        except Exception:
            pass
        if not found:
            out("    WebView2   : ⚠️  未检测到 —— 需安装 WebView2 Runtime")
            out("                 https://developer.microsoft.com/microsoft-edge/webview2/")
            # 说明：winforms 后端可作为无 WebView2 时的降级方案，
            # 因此这里只告警不判定为阻断项，避免误杀可运行产物。
        # .NET Framework（pythonnet 的 netfx 后端依赖，Win10/11 自带）
        try:
            import winreg as _wr
            k = _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full")
            v, _ = _wr.QueryValueEx(k, "Release")
            out("    .NET 4.x   : ✅ 已安装 (Release %s)" % v)
        except Exception:
            out("    .NET 4.x   : ❌ 未检测到 —— pythonnet 需要 .NET Framework")
            ok = False
    else:
        out("    WebKitGTK : %s" % ("✅" if os.path.isdir("/usr/lib/x86_64-linux-gnu/libwebkit2gtk-4.0.so") or
                                          os.path.isdir("/usr/lib64/libwebkit2gtk-4.0.so") else "⚠️  未在默认路径找到"))
        out("                 需要: sudo apt install libwebkit2gtk-4.0-37 libgtk-3-0")

    out("")
    out("[5] 页面资源")
    root = _resource_root()
    for rel in ("index.html", "vendor/echarts.min.js"):
        p = os.path.join(root, rel)
        exists = os.path.isfile(p)
        size = (os.path.getsize(p) / 1024) if exists else 0
        out("    %-24s %s" % (rel, ("✅ %.0f KB" % size) if exists else "❌ 缺失"))
        ok = ok and exists

    out("")
    out("[6] 数据源连通性")
    try:
        import data_sources as ds
        idx = ds.fetch_index_quotes()
        out("    指数快照   : ✅ %d 个 (源: %s)" % (len(idx), idx[0]["source"] if idx else "-"))
    except Exception as e:
        out("    指数快照   : ❌ %s" % str(e)[:60])
        ok = False
    try:
        import data_sources as ds
        b = ds.fetch_market_breadth()
        out("    涨跌家数   : ✅ 涨%d 平%d 跌%d" % (b["up"], b["flat"], b["down"]))
    except Exception as e:
        out("    涨跌家数   : ❌ %s" % str(e)[:60])
        ok = False

    out("")
    out("=" * 62)
    out("  结果: %s" % ("✅ 全部通过，可以正常使用" if ok else "❌ 存在阻断项，见上方 ❌ 标记"))
    out("=" * 62)

    report = "\n".join(lines)
    # 落盘：GUI 模式下 stdout 不可见，这是唯一可靠的输出通道
    try:
        rp = os.path.join(_user_data_dir(), "selftest.txt")
        with open(rp, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        lines.append("  报告已写入: %s" % rp)
    except Exception:
        pass

    try:
        print(report)
    except Exception:
        pass
    return 0 if ok else 1


# --------------------------------------------------- 主程序

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="A股实时看盘桌面应用")
    ap.add_argument("--port", type=int, default=8770, help="后端端口，0=自动分配")
    ap.add_argument("--headless", action="store_true",
                    help="不开窗口，只跑后端（用于服务器部署 / 自动化验证）")
    ap.add_argument("--selftest", action="store_true",
                    help="环境自检：检查 GUI 后端 / 系统依赖 / 页面资源 / 数据源")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(_selftest())

    # Windows 7/8.1 无法运行（Python 运行时 + WebView2 均已停止支持），
    # 提前给出明确提示而不是让用户对着崩溃信息猜。
    wname, wbuild, wok = detect_windows_version()
    if wname and not wok:
        _log("❌ 当前系统 %s (Build %d) 不受支持，最低要求 Windows 10" % (wname, wbuild))
        print("=" * 62)
        print("  错误：当前系统 %s (Build %d) 不受支持" % (wname, wbuild))
        print("  本应用最低要求 Windows 10 (Build 10240) 及以上")
        print("")
        print("  原因：")
        print("    · Python 3.9+ 官方已不支持 Windows 7/8.1")
        print("    · WebView2 Runtime 自 2023-01-10 起停止对 Win7/8.1 官方支持")
        print("    · ECharts 5 不支持 IE11 内核")
        print("=" * 62)
        raise SystemExit(1)

    if not args.headless:
        import webview  # 延迟导入，无 GUI 环境下也能 import 本模块做测试

    import server as S
    import data_sources as ds

    # 路径：data_sources 已内置打包环境检测（BASE_DIR→解包目录，CACHE_DIR→用户数据目录），
    # 这里只做兜底修正，避免开发态与打包态行为不一致。
    if getattr(sys, "frozen", False):
        ds.BASE_DIR = _resource_root()
        ds.CACHE_DIR = os.path.join(_user_data_dir(), ".cache")
        os.makedirs(ds.CACHE_DIR, exist_ok=True)

    port = pick_port(args.port)
    S.PORT = port

    # 后端：后台线程启动
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", port), S.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # 历史回填后台线程
    threading.Thread(target=S.bg_history_worker, daemon=True).start()

    url = "http://127.0.0.1:%d/" % port
    # 端口可能是动态分配的（8770 被占用时），把真实后端地址通过 ?api= 告诉前端，
    # 否则前端只能按硬编码的候选端口探测，会找不到这个动态端口。
    page_url = url + "?api=" + urllib.parse.quote(url, safe="")
    _log("后端已启动: %s" % url)
    _log("页面地址: %s" % page_url)
    _log("缓存目录: %s" % ds.CACHE_DIR)
    _log("资源目录: %s" % ds.BASE_DIR)

    # 等待后端就绪再开窗，避免首帧白屏
    for _ in range(50):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            s.close()
            break
        except OSError:
            time.sleep(0.1)

    # ---- 关闭流程 ----
    # 注意：不能在 closed 事件回调里调 httpd.shutdown() —— 它是阻塞调用，
    # 而事件回调跑在独立线程里，阻塞会导致退出流程卡住。
    # 正确做法：事件里只记日志，真正的关闭放在 webview.start() 返回之后
    # （start() 本身就会在窗口关闭后返回）。
    def on_closed():
        _log("窗口已关闭")

    if args.headless:
        _log("headless 模式：不打开窗口，后端持续运行（Ctrl+C 退出）")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            _log("收到中断信号，关闭后端")
        _shutdown(httpd)
        _log("已退出")
        return

    window = webview.create_window(
        "A股实时看盘看板",
        page_url,
        width=1600, height=1000,
        min_size=(1200, 800),
        confirm_close=False,
        on_top=False,
    )

    # pywebview 6.x 用事件订阅模型：webview.start() 不接受 on_closed 参数，
    # 关闭事件需通过 window.events.closed 订阅。
    if window is not None:
        window.events.closed += on_closed

    webview.start()
    _log("GUI 事件循环结束，关闭后端")
    _shutdown(httpd)
    _log("已退出")


if __name__ == "__main__":
    main()
