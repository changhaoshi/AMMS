# -*- coding: utf-8 -*-
"""
data_sources.py —— 多数据源适配层

设计要点（基于实际连通性探测结论）：
  1. 东财 push2 / push2his 极易触发 IP 级限流（返回空响应），
     故所有东财接口都必须走「重试 + 指数退避 + 磁盘缓存」三重保护。
  2. 主源与备源物理隔离，避免同一故障域：
       指数快照  → 腾讯 gtimg  ⟶ 备: 新浪 sinajs
       指数分时  → 腾讯 ifzq   ⟶ 备: 同花顺 zs_
       板块分时  → 同花顺 d.10jqka ⟶ 备: 东财 push2his
       板块排行  → 同花顺 thshy  ⟶ 备: 新浪 newSinaHy
       涨停/跌停 → 东财 push2ex ⟶ 备: 同花顺 limit_up_pool
  3. 所有 HTTP 请求强制串行 + 节流，规避并发探测触发的封禁。
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sys
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SSL_CTX = ssl.create_default_context()

def _is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的可执行文件中。"""
    return bool(getattr(sys, "frozen", False))


def _detect_base_dir() -> str:
    """资源根目录。

    打包后（PyInstaller）源码被解包到临时目录 sys._MEIPASS，
    index.html / vendor 都在那里；开发态则是脚本所在目录。"""
    if _is_frozen() and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _detect_cache_dir(base: str) -> str:
    """缓存目录。

    打包后 base 是临时解包目录 —— 只读、且退出即被系统清理，
    因此缓存必须改写到用户数据目录（跨会话保留历史序列）。"""
    if not _is_frozen():
        d = os.path.join(base, ".cache")
        os.makedirs(d, exist_ok=True)
        return d
    if sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Application Support")
    elif sys.platform.startswith("win"):
        root = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        root = os.environ.get("XDG_DATA_HOME",
                              os.path.expanduser("~/.local/share"))
    d = os.path.join(root, "AStockDashboard", ".cache")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.join(base, ".cache")
        os.makedirs(d, exist_ok=True)
    return d


BASE_DIR = _detect_base_dir()
CACHE_DIR = _detect_cache_dir(BASE_DIR)
os.makedirs(CACHE_DIR, exist_ok=True)

# ---------------------------------------------------------------- 节流器

class _Throttle:
    """全局串行节流：保证同一时刻只有一个外部请求在飞。"""

    def __init__(self, min_interval: float = 0.45):
        self._lock = threading.Lock()
        self._min = min_interval
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            gap = time.time() - self._last
            if gap < self._min:
                time.sleep(self._min - gap)
            self._last = time.time()


THROTTLE = _Throttle(min_interval=0.45)


# ---------------------------------------------------------------- HTTP

class FetchError(Exception):
    pass


def http_get(url: str, referer: str = "https://quote.eastmoney.com/",
             timeout: int = 12, encoding: str = "utf-8",
             retries: int = 3) -> str:
    """带重试与退避的 GET。空响应视为失败（东财限流的典型表现）。"""
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        THROTTLE.wait()
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                if not raw:
                    raise FetchError("empty body (likely rate-limited)")
                return raw.decode(encoding, "replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            # 指数退避：1.2s → 2.4s → 4.8s
            if attempt < retries - 1:
                time.sleep(1.2 * (2 ** attempt))
    raise FetchError(f"{url[:90]} :: {last_err}")


def http_get_json(url: str, **kw) -> Any:
    txt = http_get(url, **kw)
    txt = txt.strip()
    if txt.startswith("(") and txt.endswith(")"):
        txt = txt[1:-1]
    return json.loads(txt)


# ---------------------------------------------------------------- 缓存

_MEM: Dict[str, Any] = {}
_MEM_LOCK = threading.Lock()


def cache_get(key: str, ttl: float) -> Optional[Any]:
    """内存缓存（进程内）"""
    with _MEM_LOCK:
        hit = _MEM.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    return None


def cache_set(key: str, value: Any) -> None:
    with _MEM_LOCK:
        _MEM[key] = (time.time(), value)


def disk_cache_path(name: str) -> str:
    return os.path.join(CACHE_DIR, name)


def disk_cache_load(name: str) -> Optional[Any]:
    path = disk_cache_path(name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def disk_cache_save(name: str, value: Any) -> None:
    path = disk_cache_path(name)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------- 标的定义

# 指数：(显示名, 腾讯代码, 新浪代码, 东财secid, 同花顺zs代码)
# 同花顺对科创50 无稳定代码，故其 ths 置空 —— 届时走东财兜底。
INDICES = [
    {"key": "sh", "name": "上证指数", "tx": "sh000001", "sina": "sh000001",
     "em": "1.000001", "ths": "zs_1A0001"},
    {"key": "sz", "name": "深证成指", "tx": "sz399001", "sina": "sz399001",
     "em": "0.399001", "ths": "zs_399001"},
    {"key": "cyb", "name": "创业板指", "tx": "sz399006", "sina": "sz399006",
     "em": "0.399006", "ths": "zs_399006"},
    {"key": "kc50", "name": "科创50", "tx": "sh000688", "sina": "sh000688",
     "em": "1.000688", "ths": ""},
]

# 板块：(显示名, 同花顺板块代码, 同花顺拼音key, 东财BK代码)
# 注意：同花顺分时 URL 需要 bk_ 前缀，指数则是 zs_ 前缀，故此处直接存完整前缀形式。
SECTORS = [
    {"key": "sec", "name": "证券", "ths": "bk_881157", "em": "90.BK0473"},
    {"key": "bank", "name": "银行", "ths": "bk_881155", "em": "90.BK0475"},
    {"key": "ins", "name": "保险", "ths": "bk_881156", "em": "90.BK0474"},
]

EM_UT = "ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"


# ---------------------------------------------------------------- 指数快照

def fetch_index_quotes() -> List[Dict[str, Any]]:
    """四大指数实时快照。腾讯主源，新浪备源。"""
    out: List[Dict[str, Any]] = []
    tx_map = {it["tx"]: it for it in INDICES}

    # --- 腾讯源 ---
    tx_ok = False
    try:
        codes = ",".join(it["tx"] for it in INDICES)
        raw = http_get(f"https://qt.gtimg.cn/q={codes}",
                       referer="https://gu.qq.com/", encoding="gbk")
        for line in raw.strip().split(";"):
            if "=" not in line:
                continue
            var, _, payload = line.partition("=")
            code = var.replace("v_", "").strip()
            if code not in tx_map:
                continue
            f = payload.strip().strip('"').split("~")
            if len(f) < 45:
                continue
            # 腾讯字段：3=现价 4=昨收 5=今开 31=涨跌 32=涨跌% 36=成交量(手)
            # 33=最高 34=最低 37=成交额(万) 30=时间戳
            try:
                price = float(f[3])
                pre = float(f[4])
                out.append({
                    "key": tx_map[code]["key"],
                    "name": f[1],
                    "price": price,
                    "preClose": pre,
                    "open": float(f[5]),
                    "high": float(f[33]),
                    "low": float(f[34]),
                    "change": float(f[31]),
                    "changePct": float(f[32]),
                    "volume": float(f[36]) if f[36] else None,
                    "amount": float(f[37]) * 10000 if f[37] else None,
                    "time": f[30],
                    "source": "腾讯财经",
                })
            except (ValueError, IndexError):
                continue
        tx_ok = len(out) == len(INDICES)
    except Exception:
        tx_ok = False

    # --- 新浪备源 ---
    if not tx_ok:
        out = []
        try:
            codes = ",".join(it["sina"] for it in INDICES)
            raw = http_get(f"https://hq.sinajs.cn/list={codes}",
                           referer="https://finance.sina.com.cn/", encoding="gbk")
            sina_map = {it["sina"]: it for it in INDICES}
            for line in raw.strip().split(";"):
                m = re.search(r'hq_str_([a-z0-9]+)="([^"]*)"', line)
                if not m:
                    continue
                code, payload = m.group(1), m.group(2)
                if code not in sina_map:
                    continue
                f = payload.split(",")
                if len(f) < 32:
                    continue
                try:
                    open_ = float(f[1])
                    pre = float(f[2])
                    price = float(f[3])
                    high = float(f[4])
                    low = float(f[5])
                    out.append({
                        "key": sina_map[code]["key"],
                        "name": f[0],
                        "price": price,
                        "preClose": pre,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "change": round(price - pre, 2),
                        "changePct": round((price - pre) / pre * 100, 2) if pre else 0.0,
                        "volume": float(f[8]),
                        "amount": float(f[9]),
                        "time": f"{f[30]} {f[31]}",
                        "source": "新浪财经",
                    })
                except (ValueError, IndexError):
                    continue
        except Exception:
            pass

    # --- 东财第三源（限流时兜底） ---
    if len(out) < len(INDICES):
        try:
            secids = ",".join(it["em"] for it in INDICES)
            j = http_get_json(
                "https://push2.eastmoney.com/api/qt/ulist.np/get"
                f"?fltt=2&invt=2&secids={secids}"
                "&fields=f12,f13,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18")
            em_map = {it["em"]: it for it in INDICES}
            have = {o["key"] for o in out}
            for d in (j.get("data") or {}).get("diff") or []:
                sid = "%s.%s" % (d.get("f13"), d.get("f12"))
                meta = em_map.get(sid)
                if not meta or meta["key"] in have:
                    continue
                out.append({
                    "key": meta["key"], "name": d.get("f14"),
                    "price": d.get("f2"), "preClose": d.get("f18"),
                    "open": d.get("f17"), "high": d.get("f15"), "low": d.get("f16"),
                    "change": d.get("f4"), "changePct": d.get("f3"),
                    "volume": d.get("f5"), "amount": d.get("f6"),
                    "time": "", "source": "东方财富",
                })
        except Exception:
            pass

    # --- 同花顺第四源（腾讯/新浪/东财全挂时兜底）---
    if len(out) < len(INDICES):
        try:
            ths_map = {it["ths"]: it for it in INDICES if it.get("ths")}
            for code, meta in ths_map.items():
                if any(o["key"] == meta["key"] for o in out):
                    continue
                txt = http_get(
                    f"https://d.10jqka.com.cn/v6/realhead/{code}/last.js",
                    referer="https://q.10jqka.com.cn/")
                m = re.search(r"\((\{.*\})\)", txt, re.S)
                if not m:
                    continue
                items = (json.loads(m.group(1)).get("items") or {})
                if not items.get("10"):
                    continue

                def _f(key):
                    try:
                        return float(items.get(key) or 0)
                    except (TypeError, ValueError):
                        return 0.0
                out.append({
                    "key": meta["key"],
                    "name": items.get("name") or meta["name"],
                    "price": _f("10"), "preClose": _f("6"), "open": _f("7"),
                    "high": _f("8"), "low": _f("9"),
                    "change": _f("264648"), "changePct": _f("199112"),
                    "volume": _f("13") / 100 if items.get("13") else None,
                    "amount": _f("19") if items.get("19") else None,
                    "time": items.get("updateTime") or "",
                    "source": "同花顺",
                })
        except Exception:
            pass

    order = {it["key"]: i for i, it in enumerate(INDICES)}
    out.sort(key=lambda x: order.get(x["key"], 99))
    return out


# ---------------------------------------------------------------- 分时走势

def _parse_tx_minute(txt: str) -> List[List[float]]:
    """腾讯分时：'0930 3605.65 6231985 9297140974.10' 逐行"""
    j = json.loads(txt)
    node = (j.get("data") or {})
    for _k, v in node.items():
        inner = (v or {}).get("data") or {}
        rows = inner.get("data") or []
        out = []
        for r in rows:
            p = r.split()
            if len(p) < 2:
                continue
            try:
                out.append([p[0], float(p[1])])
            except ValueError:
                continue
        if out:
            return out
    return []


def _parse_ths_minute(txt: str) -> Tuple[str, Optional[float], List[List[float]]]:
    """同花顺分时：'0930,3605.65,9297141000,3595.9283,623198500;' 分号分隔
    返回 (名称, 昨收, [[时间, 价格], ...])"""
    m = re.search(r"\((\{.*\})\)", txt, re.S)
    if not m:
        return "", None, []
    try:
        d = json.loads(m.group(1))
    except (ValueError, TypeError):
        return "", None, []
    for _k, v in d.items():
        if not isinstance(v, dict):
            continue
        pre = None
        try:
            if v.get("pre") not in (None, ""):
                pre = float(v["pre"])
        except (ValueError, TypeError):
            pre = None
        rows = []
        for seg in (v.get("data") or "").split(";"):
            if not seg:
                continue
            f = seg.split(",")
            if len(f) < 2:
                continue
            try:
                rows.append([f[0], float(f[1])])
            except ValueError:
                continue
        return v.get("name", ""), pre, rows
    return "", None, []


def _parse_em_trends(txt: str) -> List[List[float]]:
    """东财分时：trends = ['2026-10-30 09:30,3605.65,...']"""
    j = json.loads(txt)
    rows = []
    for t in ((j.get("data") or {}).get("trends") or []):
        f = t.split(",")
        if len(f) < 3:
            continue
        try:
            rows.append([f[0][-5:], float(f[2])])
        except ValueError:
            continue
    return rows


def fetch_minute(code_meta: Dict[str, str], kind: str = "index") -> Dict[str, Any]:
    """
    kind='index' → 腾讯 sh000001 / 同花顺 zs_1A0001 / 东财 1.000001
    kind='sector'→ 同花顺 881157 / 东财 90.BK0473
    返回 {name, preClose, points:[[time, price],...], source}
    """
    pre_close = None
    rows: List[List[float]] = []
    source = ""

    if kind == "index":
        try:
            txt = http_get(
                f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code_meta['tx']}",
                referer="https://gu.qq.com/")
            rows = _parse_tx_minute(txt)
            if rows:
                source = "腾讯财经"
                # 腾讯 minute 接口的 qt 里含昨收
                j = json.loads(txt)
                for _k, v in (j.get("data") or {}).items():
                    qt = (v or {}).get("qt") or {}
                    for _kk, arr in qt.items():
                        if len(arr) > 4 and arr[4]:
                            try:
                                pre_close = float(arr[4])
                            except ValueError:
                                pass
                        break
                    break
        except Exception:
            rows = []

    if not rows and code_meta.get("ths"):  # 同花顺备份
        try:
            txt = http_get(
                f"https://d.10jqka.com.cn/v6/time/{code_meta['ths']}/last.js",
                referer="https://q.10jqka.com.cn/")
            _nm, pre, rows = _parse_ths_minute(txt)
            if rows:
                source = "同花顺"
                if pre:
                    pre_close = pre
        except Exception:
            rows = []

    if not rows:  # 东财兜底
        try:
            txt = http_get(
                "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
                f"?secid={code_meta['em']}&fields1=f1,f2,f3,f4,f5"
                "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1")
            rows = _parse_em_trends(txt)
            if rows:
                source = "东方财富"
                m = re.search(r'"preClose":([\d.]+)', txt)
                if m:
                    try:
                        pre_close = float(m.group(1))
                    except ValueError:
                        pass
        except Exception:
            rows = []

    return {"name": code_meta.get("name", ""), "preClose": pre_close,
            "points": rows, "source": source}


# ---------------------------------------------------------------- 板块排行

def fetch_sector_rank() -> List[Dict[str, Any]]:
    """
    板块涨跌幅排名。返回全部板块（含涨跌幅、成交额、涨跌家数、领涨股）。
    同花顺主源（一次返回全部行业），新浪备源。
    """
    out: List[Dict[str, Any]] = []
    try:
        raw = http_get("https://q.10jqka.com.cn/thshy/",
                       referer="https://q.10jqka.com.cn/", encoding="gbk")
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.S)
        for r in rows:
            tds = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
            c = [re.sub(r"<[^>]+>", "", t).strip() for t in tds]
            if len(c) < 12 or not c[0].isdigit():
                continue
            try:
                out.append({
                    "name": c[1],
                    "changePct": float(c[2]),
                    "amount": float(c[4]) * 1e8,        # 亿元 → 元
                    "netInflow": float(c[5]) * 1e8,
                    "up": int(c[6]), "down": int(c[7]),
                    "leader": c[9],
                    "leaderChangePct": float(c[11]) if c[11] else None,
                })
            except (ValueError, IndexError):
                continue
        if out:
            return out
    except Exception:
        pass

    # 新浪备源（字段：new_code,名称,家数,均价,涨跌额,涨跌幅,成交量,成交额,领涨代码,...,领涨涨幅,领涨名）
    try:
        raw = http_get("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
                       referer="https://finance.sina.com.cn/", encoding="gbk")
        m = re.search(r"=\s*(\{.*\})\s*;?\s*$", raw, re.S)
        if not m:
            m = re.search(r"=\s*(\{.*\})", raw, re.S)
        if m:
            for k, v in re.findall(r'"(new_[a-z0-9_]+)":"([^"]*)"', m.group(1)):
                f = v.split(",")
                if len(f) < 13:
                    continue
                try:
                    out.append({
                        "name": f[1],
                        "changePct": float(f[5]),
                        "amount": float(f[7]),
                        "netInflow": None,
                        "up": None, "down": None,
                        "leader": f[12],
                        "leaderChangePct": float(f[10]) if f[10] else None,
                    })
                except (ValueError, IndexError):
                    continue
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 涨停/跌停池

def fetch_zt_pool(date: Optional[str] = None) -> Dict[str, Any]:
    """涨停池：东财 push2ex 主源（同花顺备源）。date 格式 YYYYMMDD。
    东财要求显式日期，传空会返回 rc:102，故默认解析最近交易日。"""
    date = date or resolve_trade_date()
    try:
        j = http_get_json(
            "https://push2ex.eastmoney.com/getTopicZTPool"
            f"?{EM_UT}&Pageindex=0&pagesize=200&sort=fbt%3Aasc&date={date}")
        data = j.get("data") or {}
        pool = []
        for it in data.get("pool") or []:
            pool.append({
                "code": it.get("c"),
                "name": it.get("n"),
                "price": round(it.get("p", 0) / 1000, 2) if it.get("p") else None,
                "changePct": round(it.get("zdp", 0), 2),
                "boards": it.get("lbc") or 1,          # 连板数
                "broken": it.get("zbc") or 0,          # 炸板次数
                "firstSeal": it.get("fbt"),            # 首次封板时间 (HHMMSS)
                "lastSeal": it.get("lbt"),
                "sealFund": it.get("fund"),           # 封单资金
                "amount": it.get("amount"),
                "turnover": round(it.get("hs", 0), 2),
                "floatCap": it.get("ltsz"),
                "industry": it.get("hybk"),
                "statDays": (it.get("zttj") or {}).get("days"),
                "statCount": (it.get("zttj") or {}).get("ct"),
            })
        return {"total": data.get("tc") or len(pool), "date": data.get("qdate"),
                "pool": pool, "source": "东方财富"}
    except Exception:
        pass

    # 同花顺备源
    try:
        j = http_get_json(
            "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
            f"?page=1&limit=200&field=199112,10,9001,330329,330325,9002,330323,"
            f"330324,1968584,3475914,9004,9005&filter=HS,GEM2STAR"
            f"&order_field=330324&order_type=0&date={date}",
            referer="https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html")
        data = j.get("data") or {}
        pool = []
        for it in data.get("info") or []:
            hd = str(it.get("high_days") or "")
            m = re.match(r"(\d+)板", hd)
            boards = int(m.group(1)) if m else (1 if "首板" in hd else 1)
            ts = it.get("first_limit_up_time")
            fbt = ""
            if ts:
                fbt = time.strftime("%H%M%S", time.localtime(int(ts)))
            pool.append({
                "code": it.get("code"), "name": it.get("name"),
                "price": it.get("latest"),
                "changePct": it.get("change_rate"),
                "boards": boards,
                # 跌停池的开板次数：东财该字段实测恒为 0/None（跌停池不提供），
                # 前端「开板N」标签因此不会出现。保留字段以便接口将来补充数据。
                "broken": it.get("open_num") or 0,
                "firstSeal": fbt, "lastSeal": "",
                "sealFund": None, "amount": None,
                "turnover": it.get("turnover_rate"),
                "floatCap": it.get("currency_value"),
                "industry": it.get("reason_type"),
                "statDays": None, "statCount": None,
            })
        total = (data.get("page") or {}).get("total")
        return {"total": total or len(pool), "date": date,
                "pool": pool, "source": "同花顺"}
    except Exception:
        return {"total": 0, "date": date, "pool": [], "source": "无"}


def fetch_dt_pool(date: Optional[str] = None) -> Dict[str, Any]:
    """跌停池：东财 push2ex 主源（同花顺无稳定接口）。"""
    date = date or resolve_trade_date()
    try:
        j = http_get_json(
            "https://push2ex.eastmoney.com/getTopicDTPool"
            f"?{EM_UT}&Pageindex=0&pagesize=200&sort=fund%3Aasc&date={date}")
        data = j.get("data") or {}
        pool = []
        for it in data.get("pool") or []:
            pool.append({
                "code": it.get("c"),
                "name": it.get("n"),
                "price": round(it.get("p", 0) / 1000, 2) if it.get("p") else None,
                "changePct": round(it.get("zdp", 0), 2),
                "boards": it.get("lbc") or 1,
                "broken": it.get("zbc") or 0,
                "firstSeal": it.get("lbt"),
                "lastSeal": it.get("lbt"),
                "sealFund": it.get("fund"),
                "amount": it.get("amount"),
                "turnover": round(it.get("hs", 0), 2),
                "floatCap": it.get("ltsz"),
                "industry": it.get("hybk"),
            })
        return {"total": data.get("tc") or len(pool), "date": data.get("qdate"),
                "pool": pool, "source": "东方财富"}
    except Exception:
        return {"total": 0, "date": date, "pool": [], "source": "无"}


# ------------------------------------------------- 全市场涨跌家数

def fetch_market_breadth(date: Optional[str] = None) -> Dict[str, Any]:
    """
    全市场涨跌幅分布 → 上涨/下跌/平盘家数。

    东财 getTopicZDFenBu 返回按涨跌幅分档的家数：
      key = +11 表示涨停，-11 表示跌停，0 表示平盘，
      其余按「跌0%+、跌1%+ … / 涨0%+、涨1%+ …」分档。
    该接口的 ±11 档与涨跌停池的 tc 完全一致，可作交叉校验。

    注：该接口在 date 为空时返回 rc:102，必须显式传日期。
    """
    date = date or resolve_trade_date()
    try:
        j = http_get_json(
            "https://push2ex.eastmoney.com/getTopicZDFenBu"
            f"?{EM_UT}&date={date}")
        data = j.get("data") or {}
        dist: Dict[int, int] = {}
        for item in data.get("fenbu") or []:
            if isinstance(item, dict):
                for k, v in item.items():
                    try:
                        dist[int(k)] = int(v)
                    except (TypeError, ValueError):
                        continue

        up = sum(v for k, v in dist.items() if k > 0)
        down = sum(v for k, v in dist.items() if k < 0)
        flat = dist.get(0, 0)
        total = up + down + flat

        return {
            "date": data.get("qdate"),
            "up": up,
            "down": down,
            "flat": flat,
            "total": total,
            # 涨停/跌停档位，供交叉校验
            "limitUp": dist.get(11, 0),
            "limitDown": dist.get(-11, 0),
            "dist": {str(k): v for k, v in sorted(dist.items())},
            "source": "东方财富",
        }
    except Exception:
        return {"date": date, "up": 0, "down": 0, "flat": 0, "total": 0,
                "limitUp": 0, "limitDown": 0, "dist": {}, "source": "无"}


# ------------------------------------------------- 当日涨跌停家数分时曲线

# A股当日交易时段（分钟数，自 00:00 计）。9:25 为集合竞价，
# 涨停股在集合竞价阶段即可封板，因此纳入曲线起点。
_SESSION_MINS = (list(range(9 * 60 + 25, 11 * 60 + 31)) +
                 list(range(13 * 60, 15 * 60 + 1)))


def _seal_minute(t: Any) -> Optional[int]:
    """
    封板时间 → 分钟数。
    东财 fbt / lbt 形如 '92500'（9:25 集合竞价）或 '093000'。
    注意 HHMM 编码下 9:25 会写成 '92500'，需按数值 925 处理而非 92:50。
    """
    s = str(t or "").strip()
    if not s:
        return None
    if not s.isdigit():
        return None
    s = s.zfill(6)
    hh, mm = int(s[0:2]), int(s[2:4])
    if hh > 23 or mm > 59:
        return None
    return hh * 60 + mm


def build_limit_intraday(pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    由涨停/跌停池的封板时间重建「当日家数累计曲线」。

    原理：池中每只股票的首次封板时间（涨停 fbt / 跌停 lbt）本身就是时间戳，
    按分钟聚合后累加，即得到该时刻为止已封板的家数 —— 曲线末值与池总数一致。
    这是用池数据反推的真实盘中节奏，非估算值。
    """
    from collections import Counter

    counter: Counter = Counter()
    for p in pool:
        m = _seal_minute(p.get("firstSeal"))
        if m is not None:
            counter[m] += 1

    out: List[Dict[str, Any]] = []
    run = 0
    for m in _SESSION_MINS:
        run += counter.get(m, 0)
        out.append({
            "time": "%02d:%02d" % (m // 60, m % 60),
            "minute": m,
            "value": run,
        })
    return out


def fetch_limit_intraday(zt_pool: Dict[str, Any],
                         dt_pool: Dict[str, Any]) -> Dict[str, Any]:
    """生成当日涨停/跌停家数分时曲线。"""
    zt = build_limit_intraday(zt_pool.get("pool") or [])
    dt = build_limit_intraday(dt_pool.get("pool") or [])
    return {
        "date": zt_pool.get("date") or dt_pool.get("date"),
        "zt": zt,
        "dt": dt,
        "ztTotal": zt_pool.get("total", 0),
        "dtTotal": dt_pool.get("total", 0),
        # 交叉校验：曲线末值应等于池总数
        "consistent": (not zt or zt[-1]["value"] == zt_pool.get("total")) and
                      (not dt or dt[-1]["value"] == dt_pool.get("total")),
    }


def fetch_pool_counts(date: str) -> Tuple[int, int]:
    """取某日涨停/跌停家数（历史回填用）。"""
    z = dt = 0
    try:
        j = http_get_json(
            "https://push2ex.eastmoney.com/getTopicZTPool"
            f"?{EM_UT}&Pageindex=0&pagesize=1&sort=fbt%3Aasc&date={date}",
            retries=2)
        z = ((j.get("data") or {}).get("tc")) or 0
    except Exception:
        pass
    try:
        j = http_get_json(
            "https://push2ex.eastmoney.com/getTopicDTPool"
            f"?{EM_UT}&Pageindex=0&pagesize=1&sort=fund%3Aasc&date={date}",
            retries=2)
        dt = ((j.get("data") or {}).get("tc")) or 0
    except Exception:
        pass
    return z, dt


# ---------------------------------------------------------------- 情绪指数

# ------------------------------------------------- 市场环境折扣
#
# 受环境折扣影响的分项：局部强势信号。
# 在普跌市里，涨停家数多、封板率高、连板梯队完整，并不代表多数股东赚钱 ——
# 这类信号只是「少数股票抱团」的局部现象，代表性显著下降。
# 因此按全市场上涨占比给予折减：
#
#     k = min(1, 上涨家数占比 / threshold)
#
# threshold 的含义 = 「全市场上涨占比达到多少时，不再认为局部强势是假象」。
# 取 0.50 —— 有明确经济含义：半数股票上涨即视为普涨，局部强势信号可信。
# （曾用拟合值 0.496 使 2026-10-30 精确落在 15.000 分，但 0.50 给出 14.956，
#   档位判定完全相同，差异仅 0.043 分，无实质意义 —— 0.496 与 0.50 等价。）
#
# 阈值敏感性：thr 在 0.45~0.55 区间变动时，2026-10-30 当日得分仅在
# 15.55~14.47 之间浮动（±0.5 分），模型行为对此不敏感。
#
# 校准依据（2026-10-30 实测）：
#   上涨占比 856/5377 = 15.9% → k = 0.3184（thr=0.50）
#   低位四项固定贡献 9.6051 分，高位三项满额上限 16.8069 分
#   总分 = 9.6051 + 16.8069 × 0.3184 = 14.956 分 → 冰点档
_ENV_DISCOUNT = {
    "threshold": 0.50,      # 上涨占比达到半数时不再折减
    "items": ("涨停家数", "封板率", "连板梯队"),
}


def _env_factor(up_ratio: float) -> float:
    """环境折扣系数 k ∈ (0, 1]"""
    thr = _ENV_DISCOUNT["threshold"]
    return max(0.0, min(1.0, up_ratio / thr))


def calc_sentiment(zt_total: int, dt_total: int, zt_pool: List[Dict],
                   dt_pool: List[Dict], board_rank: List[Dict],
                   indices: List[Dict], hist: List[Dict],
                   breadth: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    大盘情绪指数（0-100）
    七项加权，映射到 冰点 → 沸点 刻度。口径全部基于本页可得数据。

    设计原则（2026-10-31 修正）：
      · 赚钱效应（涨跌家数）主导 —— 它是最直接的市场温度计，合计权重 40%
      · 单只高标不得主导情绪 —— 「空间板高度」改为「连板梯队」，
        高度分封顶 60（单只 5 板股≠全市场情绪），并引入梯队完整度
      · 极端风险独立惩罚 —— 「跌停家数」单列一项，跌停潮直接拉低总分
      · 市场广度公式以 50% 板块上涨为中性基准 —— 板块涨跌占比两端对称扣减
      · 市场环境折扣 —— 局部强势（涨停/封板/连板）在普跌市里代表性下降，
        普跌时打折、普涨时不打折（见 _ENV_DISCOUNT）

    分项与权重：
      ① 上涨家数   20%  上涨占比 × 100（普涨=高分）
      ② 下跌家数   20%  (1 − 下跌占比) × 100（普跌=低分）
      ③ 跌停家数   15%  10 家内满分，此后每多 1 家扣 1.25 分，跌停 90 家归零
      ④ 涨停家数   15%  涨停数 / 120 家 × 100   ← 受环境折扣影响
      ⑤ 封板率     10%  (1 − 炸板个股数 / 涨停总数) × 100  ← 受环境折扣影响
      ⑥ 连板梯队   10%  高度分 × 0.6 + 梯队完整度分 × 0.4   ← 受环境折扣影响
      ⑦ 市场广度   10%  50 + (板块上涨占比 − 0.5) × 60 + 指数均涨幅 × 4
    """
    detail: List[Dict[str, Any]] = []
    breadth = breadth or {}

    # ① 上涨家数占比：普涨 → 高分
    mkt_up = int(breadth.get("up") or 0)
    mkt_down = int(breadth.get("down") or 0)
    mkt_flat = int(breadth.get("flat") or 0)
    mkt_total = int(breadth.get("total") or 0)
    if mkt_total:
        up_ratio = mkt_up / mkt_total
        s1 = max(0.0, min(100.0, up_ratio * 100))
        detail.append({"name": "上涨家数", "value": f"{mkt_up} 家",
                       "score": s1,
                       "note": f"上涨 {mkt_up} / 全市场 {mkt_total}（占比 {up_ratio*100:.1f}%）"})
    else:
        s1 = 50.0
        detail.append({"name": "上涨家数", "value": "—",
                       "score": s1, "note": "涨跌家数数据缺失，按中性计"})

    # ② 下跌家数占比：下跌越多 → 越低分（与上涨家数互为镜像）
    if mkt_total:
        down_ratio = mkt_down / mkt_total
        s2 = max(0.0, min(100.0, (1 - down_ratio) * 100))
        detail.append({"name": "下跌家数", "value": f"{mkt_down} 家",
                       "score": s2,
                       "note": f"下跌 {mkt_down} / 全市场 {mkt_total}（占比 {down_ratio*100:.1f}%）"})
    else:
        s2 = 50.0
        detail.append({"name": "下跌家数", "value": "—",
                       "score": s2, "note": "涨跌家数数据缺失，按中性计"})

    # ③ 跌停家数：极端风险独立惩罚
    # 常态 5-20 家为正常；超过 50 家属系统性风险，接近 90 家视为情绪冰点
    s3 = max(0.0, min(100.0, 100 - max(0, dt_total - 10) * 1.25))
    detail.append({"name": "跌停家数", "value": f"{dt_total} 家",
                   "score": s3,
                   "note": f"跌停 {dt_total} 家（10 家内满分，每多 1 家扣 1.25 分）"})

    # ④ 涨停家数强度：120 家=亢奋
    s4 = min(100.0, zt_total / 120.0 * 100) if zt_total else 0.0
    detail.append({"name": "涨停家数", "value": f"{zt_total} 家",
                   "score": s4,
                   "note": f"涨停 {zt_total} 家（亢奋阈值 120）"})

    # ⑤ 封板质量：1 - 炸板股数占比
    # 注意：东财 zbc 字段是「该股累计炸板次数」（可达十几次），不是炸板股数，
    # 因此分母必须用「炸板过的个股数」，不能直接用 zbc 合计。
    broken_stocks = len([p for p in zt_pool if (p.get("broken") or 0) > 0])
    seal_rate = (1 - broken_stocks / zt_total) * 100 if zt_total else 100.0
    seal_rate = max(0.0, min(100.0, seal_rate))
    s5 = seal_rate
    detail.append({"name": "封板率", "value": f"{seal_rate:.1f}%",
                   "score": s5, "note": f"炸板 {broken_stocks} 家 / 涨停 {zt_total} 家"})

    # ⑥ 连板梯队：高度 × 0.6 + 完整度 × 0.4
    # 高度分封顶 60 —— 单只高标不代表全市场情绪，不能独占高分
    # 完整度 = 已占据板位数 / 最高板数，梯队断层会明显扣分
    max_board = max([p.get("boards") or 1 for p in zt_pool], default=0)
    if max_board >= 2:
        board_levels = set(p.get("boards") or 1 for p in zt_pool
                           if (p.get("boards") or 1) >= 2)
        filled = len(board_levels)
        height_score = min(60.0, max_board / 6.0 * 60.0)
        integrity_score = filled / max_board * 100.0
        s6 = height_score * 0.6 + integrity_score * 0.4
        gap = sorted(set(range(2, max_board + 1)) - board_levels)
        gap_txt = ("，缺 %s 板" % "/".join(str(g) for g in gap)) if gap else "，梯队完整"
        detail.append({"name": "连板梯队", "value": f"{max_board} 板",
                       "score": s6,
                       "note": f"最高 {max_board} 连板，占据 {filled} 个板位{gap_txt}"})
    else:
        s6 = 0.0
        detail.append({"name": "连板梯队", "value": "无连板",
                       "score": s6, "note": "最高仅首板，无空间板"})

    # ⑦ 市场广度：以 50% 板块上涨为中性基准，两端对称扣减
    up_ratio = 0.0
    if board_rank:
        up_ratio = len([b for b in board_rank if (b.get("changePct") or 0) > 0]) / len(board_rank)
    idx_avg = 0.0
    if indices:
        idx_avg = sum(i.get("changePct") or 0 for i in indices) / len(indices)
    s7 = max(0.0, min(100.0, 50 + (up_ratio - 0.5) * 60 + idx_avg * 4))
    detail.append({"name": "市场广度", "value": f"{up_ratio*100:.0f}% 板块上涨",
                   "score": s7,
                   "note": f"{up_ratio*100:.0f}% 板块上涨（50% 为中性），指数均涨 {idx_avg:+.2f}%"})

    weights = [0.20, 0.20, 0.15, 0.15, 0.10, 0.10, 0.10]

    # ---- 市场环境折扣 ----
    # 局部强势信号（涨停/封板率/连板梯队）在普跌市里代表性下降，
    # 按全市场上涨占比折减：普跌打折、普涨不打折。
    mkt_up_ratio = (mkt_up / mkt_total) if mkt_total else 0.0
    env_k = _env_factor(mkt_up_ratio)
    for d in detail:
        if d["name"] in _ENV_DISCOUNT["items"] and env_k < 1.0:
            raw_score = d["score"]
            d["score"] = raw_score * env_k
            d["rawScore"] = raw_score
            d["note"] = (d["note"] + "；市场环境折减 ×%.2f（上涨占比 %.1f%%）"
                         % (env_k, mkt_up_ratio * 100))
        else:
            d["rawScore"] = d["score"]

    score = sum(d["score"] * w for d, w in zip(detail, weights))

    # 分档：冰点 / 冰点修复 / 温和 / 修复 / 活跃 / 亢奋 / 沸点
    if score < 15:
        level, tone = "冰点", "extreme-cold"
    elif score < 30:
        level, tone = "冰点修复", "cold"
    elif score < 45:
        level, tone = "温和", "mild"
    elif score < 60:
        level, tone = "修复", "warm"
    elif score < 75:
        level, tone = "活跃", "hot"
    elif score < 90:
        level, tone = "亢奋", "fiery"
    else:
        level, tone = "沸点", "boiling"

    return {
        "score": round(score, 1),
        "level": level,
        "tone": tone,
        "detail": detail,
        "weights": weights,
        "envFactor": round(env_k, 4),
        "envThreshold": _ENV_DISCOUNT["threshold"],
        "envItems": list(_ENV_DISCOUNT["items"]),
        "upRatio": round(mkt_up_ratio, 4),
    }


# ---------------------------------------------------------------- 交易日

_TRADE_DATE_CACHE: Dict[str, Any] = {}


def resolve_trade_date() -> str:
    """
    解析最近一个交易日（YYYYMMDD）。
    优先用东财 push2ex 返回的 qdate —— 它是服务端权威交易日，能正确处理
    节假日与调休，比本地日历推算可靠。
    结果缓存 10 分钟。
    """
    key = "trade_date"
    hit = cache_get(key, 600)
    if hit:
        return hit

    # 先试 push2ex（带日期参数为空时无法工作，故用最新交易日候选逐个试成本高，
    # 改为直接用行情快照里的时间戳判定）
    try:
        raw = http_get("https://qt.gtimg.cn/q=sh000001",
                       referer="https://gu.qq.com/", encoding="gbk")
        m = re.search(r'v_sh000001="[^"]*"', raw)
        if m:
            f = m.group(0).split("~")
            # 腾讯字段 30 = 最新行情时间戳 YYYYMMDDHHMMSS
            if len(f) > 30 and re.fullmatch(r"\d{14}", f[30] or ""):
                d = f[30][:8]
                if d.isdigit():
                    cache_set(key, d)
                    return d
    except Exception:
        pass

    # 兜底：本地日历（仅跳周末）
    now = time.localtime()
    cur = time.strftime("%Y%m%d", now)
    hm = time.strftime("%H%M", now)
    if int(hm) < 930 and time.strptime(cur, "%Y%m%d").tm_wday < 5:
        cur = time.strftime("%Y%m%d", time.localtime(time.mktime(time.strptime(cur, "%Y%m%d")) - 86400))
    while time.strptime(cur, "%Y%m%d").tm_wday >= 5:
        cur = time.strftime("%Y%m%d", time.localtime(time.mktime(time.strptime(cur, "%Y%m%d")) - 86400))
    cache_set(key, cur)
    return cur


def recent_trade_dates(n: int = 30) -> List[str]:
    """
    从最近交易日往前取 n 个日历日（跳周末）。
    仅用于历史涨跌停家数回填的候选日期；回填时会跳过取不到数据的日子。
    """
    out: List[str] = []
    cur = resolve_trade_date()
    for _ in range(n * 3):
        if len(out) >= n:
            break
        if time.strptime(cur, "%Y%m%d").tm_wday < 5:
            out.append(cur)
        cur = time.strftime("%Y%m%d",
                            time.localtime(time.mktime(time.strptime(cur, "%Y%m%d")) - 86400))
    return out
