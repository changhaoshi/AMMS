# -*- coding: utf-8 -*-
"""
backfill_history.py —— 回填近 N 个交易日涨跌停家数序列

用途：
  1. 页面「涨停家数走势 / 跌停家数走势」折线图
  2. 大盘情绪指数的历史分位参照

要点：
  · 东财 push2ex 限流敏感，必须串行 + 退避
  · 结果落盘到 .cache/limit_history.json，服务启动时直接读缓存，不重复回填
  · 增量更新：已有日期跳过，只补新的
"""
from __future__ import annotations

import json
import os
import sys
import time

import data_sources as ds

HIST_FILE = ds.disk_cache_path("limit_history.json")
DAYS = 30


def load() -> dict:
    data = ds.disk_cache_load("limit_history.json")
    if not isinstance(data, dict):
        data = {}
    data.setdefault("series", {})
    data.setdefault("updated_at", "")
    return data


def save(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ds.disk_cache_save("limit_history.json", data)


def backfill(days: int = DAYS, verbose: bool = True) -> dict:
    data = load()
    series = data["series"]
    candidates = ds.recent_trade_dates(days)

    # 从远到近回填，命中率与缓存友好
    added = 0
    for d in reversed(candidates):
        if d in series:
            if verbose:
                print("  %s 已有，跳过" % d)
            continue
        z, dt = ds.fetch_pool_counts(d)
        if z or dt:
            series[d] = {"zt": z, "dt": dt}
            added += 1
            if verbose:
                print("  %s  涨停=%-4s 跌停=%-4s" % (d, z, dt))
        else:
            if verbose:
                print("  %s  无数据（可能非交易日）" % d)
        time.sleep(0.8)

    save(data)
    if verbose:
        print("\n完成：新增 %d 天，累计 %d 天，缓存写入 %s"
              % (added, len(series), HIST_FILE))
    return data


def series_list(data: dict) -> list:
    """转成按日期升序的列表，供图表使用。"""
    items = []
    for d in sorted(data.get("series", {}).keys()):
        v = data["series"][d]
        items.append({
            "date": d,
            "label": "%s/%s" % (d[4:6], d[6:8]),
            "zt": v.get("zt", 0),
            "dt": v.get("dt", 0),
        })
    return items


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else DAYS
    print("回填近 %d 个交易日涨跌停家数..." % n)
    backfill(n)
