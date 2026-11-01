# -*- coding: utf-8 -*-
"""
server.py —— 实时看盘页面后端服务

职责：
  · 提供静态页面（index.html）
  · 聚合多数据源，输出统一 JSON 到 /api/dashboard
  · 分层缓存：内存 TTL（实时块）+ 磁盘缓存（历史序列）
  · 单飞（single-flight）合并，避免并发请求打爆上游限流

运行：
  python3 server.py [--port 8770]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import backfill_history
import data_sources as ds

BASE_DIR = ds.BASE_DIR          # 打包环境下指向 PyInstaller 解包目录
PORT = 8770

# 缓存 TTL（秒）—— 交易时段刷新快，非交易时段放慢
TTL_LIVE = 8          # 实时行情快照
TTL_POOL = 20         # 涨停跌停池（变动没那么快）
TTL_RANK = 30         # 板块排行
TTL_MINUTE = 45       # 分时走势

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def cached_fetch(key: str, ttl: float, fn):
    """带 TTL 的记忆缓存 + 单飞合并。"""
    hit = ds.cache_get(key, ttl)
    if hit is not None:
        return hit
    lock = _key_lock(key)
    with lock:  # 并发时只有一个线程真正去拉
        hit = ds.cache_get(key, ttl)
        if hit is not None:
            return hit
        val = fn()
        ds.cache_set(key, val)
        return val


# ---------------------------------------------------------------- 数据聚合

def build_dashboard() -> Dict[str, Any]:
    t0 = time.time()

    indices = cached_fetch("indices", TTL_LIVE, ds.fetch_index_quotes)

    # 分时：4 指数 + 3 板块
    minutes = {}
    for meta in ds.INDICES:
        minutes[meta["key"]] = cached_fetch(
            "min_" + meta["key"], TTL_MINUTE,
            lambda m=meta: ds.fetch_minute(m, "index"))
    for meta in ds.SECTORS:
        minutes[meta["key"]] = cached_fetch(
            "min_" + meta["key"], TTL_MINUTE,
            lambda m=meta: ds.fetch_minute(m, "sector"))

    zt = cached_fetch("zt_pool", TTL_POOL, ds.fetch_zt_pool)
    dt = cached_fetch("dt_pool", TTL_POOL, ds.fetch_dt_pool)
    rank = cached_fetch("sector_rank", TTL_RANK, ds.fetch_sector_rank)

    # 当日涨跌停家数分时曲线（由封板时间重建，末值与池总数一致）
    intraday_limit = cached_fetch(
        "limit_intraday", TTL_POOL,
        lambda: ds.fetch_limit_intraday(zt, dt))

    # 全市场涨跌家数（情绪指数的上涨/下跌家数分项）
    breadth = cached_fetch("breadth", TTL_LIVE, ds.fetch_market_breadth)

    # 历史序列：磁盘缓存，回填线程后台维护
    hist = backfill_history.load()
    hist_list = backfill_history.series_list(hist)

    sentiment = ds.calc_sentiment(
        zt.get("total", 0), dt.get("total", 0),
        zt.get("pool", []), dt.get("pool", []),
        rank, indices, hist_list, breadth)

    # 连板梯队：一板 → N 板
    ladder: Dict[str, list] = {}
    for p in zt.get("pool", []):
        b = p.get("boards") or 1
        ladder.setdefault(str(b), []).append(p)
    for k in ladder:
        ladder[k].sort(key=lambda x: -(x.get("sealFund") or 0))
    ladder_out = []
    for b in sorted(ladder.keys(), key=lambda x: int(x)):
        tier = "首板" if b == "1" else "%s板" % b
        ladder_out.append({
            "boards": int(b),
            "label": tier,
            "count": len(ladder[b]),
            "stocks": [{
                "code": s.get("code"), "name": s.get("name"),
                "price": s.get("price"), "changePct": s.get("changePct"),
                "sealFund": s.get("sealFund"), "firstSeal": s.get("firstSeal"),
                "broken": s.get("broken"), "industry": s.get("industry"),
            } for s in ladder[b]],
        })

    # 交易日历状态
    now = time.localtime()
    hhmm = time.strftime("%H%M", now)
    hm = int(hhmm[:2]) * 60 + int(hhmm[2:])
    wd = now.tm_wday
    is_weekday = wd < 5
    in_session = is_weekday and (
        (570 <= hm <= 690) or (780 <= hm <= 900))  # 9:30-11:30 / 13:00-15:00
    if not is_weekday:
        phase = "休市（周末）"
    elif hm < 570:
        phase = "盘前"
    elif hm <= 690:
        phase = "交易中"
    elif hm < 780:
        phase = "午间休市"
    elif hm <= 900:
        phase = "交易中"
    else:
        phase = "已收盘"

    trade_date = ds.resolve_trade_date()

    sources = set()
    for q in indices:
        if q.get("source"):
            sources.add(q["source"])
    for v in minutes.values():
        if v.get("source"):
            sources.add(v["source"])
    if zt.get("source") and zt["source"] != "无":
        sources.add(zt["source"])
    if dt.get("source") and dt["source"] != "无":
        sources.add(dt["source"])

    return {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tradeDate": trade_date,
        "phase": phase,
        "inSession": in_session,
        "elapsed": round(time.time() - t0, 2),
        "sources": sorted(sources),
        "indices": indices,
        "minutes": minutes,
        "zt": zt,
        "dt": dt,
        "limitIntraday": intraday_limit,
        "breadth": breadth,
        "sectorRank": rank,
        "ladder": ladder_out,
        "history": hist_list,
        "sentiment": sentiment,
        "meta": {
            "indexCount": len(indices),
            "sectorCount": len(rank),
            "ztTotal": zt.get("total", 0),
            "dtTotal": dt.get("total", 0),
            "histDays": len(hist_list),
        },
    }


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "RealtimeBoard/1.0"

    def log_message(self, fmt, *args):
        pass  # 静默访问日志，避免刷屏

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            fp = os.path.join(BASE_DIR, "index.html")
            if not os.path.exists(fp):
                self._send(404, b"index.html not found", "text/plain; charset=utf-8")
                return
            with open(fp, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path.startswith("/vendor/"):
            # 本地静态资源（echarts 等），避免依赖外网 CDN
            rel = path[len("/vendor/"):]
            if ".." in rel or rel.startswith("/"):
                self._send(400, b"bad path", "text/plain; charset=utf-8")
                return
            fp = os.path.join(BASE_DIR, "vendor", rel)
            if not os.path.exists(fp):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            ctype = "application/javascript; charset=utf-8"
            if rel.endswith(".css"):
                ctype = "text/css; charset=utf-8"
            elif rel.endswith(".json"):
                ctype = "application/json; charset=utf-8"
            with open(fp, "rb") as f:
                self._send(200, f.read(), ctype)
        elif path == "/api/dashboard":
            try:
                data = build_dashboard()
                self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)},
                           ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
        elif path == "/api/health":
            self._send(200, json.dumps({
                "ok": True, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tradeDate": ds.resolve_trade_date(),
            }, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def bg_history_worker() -> None:
    """后台线程：每 10 分钟增量补一次历史序列。
    页面只展示最近 7 个交易日，但缓存保留 12 天（留缓冲，便于日后做分位参照）。"""
    while True:
        try:
            backfill_history.backfill(12, verbose=False)
        except Exception:
            pass
        time.sleep(600)


def main() -> None:
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址。默认仅本机；需要局域网访问用 0.0.0.0")
    args = ap.parse_args()
    PORT = args.port

    # 启动即回填一次历史（后台线程，不阻塞服务）
    threading.Thread(target=bg_history_worker, daemon=True).start()

    # 双栈监听：浏览器访问 localhost 时会优先解析到 IPv6 的 ::1，
    # 只绑 IPv4 会导致 "localhost 连不上"。此处同时监听 ::1 与 127.0.0.1。
    servers = []
    host = args.host
    if host in ("127.0.0.1", "localhost"):
        try:
            class _DualStack(ThreadingHTTPServer):
                address_family = socket.AF_INET6
                daemon_threads = True

                def server_bind(self):
                    # 关闭 IPv6 v6only，允许同一个端口同时接受 IPv4 连接
                    try:
                        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                    except OSError:
                        pass
                    return ThreadingHTTPServer.server_bind(self)

            s6 = _DualStack(("::1", PORT), Handler)
            servers.append(s6)
            print("  已监听 [::1]:%d (IPv6)" % PORT)
        except OSError as e:
            print("  IPv6 监听跳过: %s" % e)

    srv = ThreadingHTTPServer((host, PORT), Handler)
    servers.append(srv)

    print("=" * 62)
    print("  实时看盘页面已启动")
    print("  地址: http://127.0.0.1:%d" % PORT)
    print("        http://localhost:%d" % PORT)
    print("  接口: http://127.0.0.1:%d/api/dashboard" % PORT)
    print("  按 Ctrl+C 停止")
    print("=" * 62)
    try:
        # IPv6 实例放后台线程，主线程跑 IPv4
        if len(servers) > 1:
            threading.Thread(target=servers[0].serve_forever, daemon=True).start()
        servers[-1].serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        for s in servers:
            try:
                s.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    main()
