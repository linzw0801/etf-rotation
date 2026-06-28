#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 轮动选股器 — 云端版 (纯 Python, 无外部依赖)
=================================================
基于原版 Scriptable 方案 (C 方案 v5):
  - 4 只 ETF: 沪深300 / 创业板 / 纳指 / 黄金
  - 线性回归动量得分 (25日)
  - vol20 波动率风控 (阈值 35%)
  - vol20>=35% → 半仓+逆回购; 否则满仓

用法:
  python etf_rotation_cloud.py                  # 运行选股, 输出到终端
  python etf_rotation_cloud.py --email TO@qq.com # 运行并发送邮件

GitHub Actions 定时任务调用:
  python etf_rotation_cloud.py --smtp
"""

import json
import math
import smtplib
import sys
import time
import urllib.request
import os
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

# ============================================================
# 配置 — 与你的 Scriptable 原版一致
# ============================================================
ETF_LIST = [
    {"code": "510300", "name": "沪深300 ETF", "market": "sh"},
    {"code": "159915", "name": "创业板 ETF",  "market": "sz"},
    {"code": "513100", "name": "纳指 ETF",    "market": "sh"},
    {"code": "518880", "name": "黄金 ETF",    "market": "sh"},
]

N = 25               # 线性回归窗口
VOL_WINDOW = 21      # 波动率计算窗口
VOL_THRESHOLD = 35   # vol20 阈值 (%)
TRADING_DAYS = 250   # 年化交易日数
DATA_MAX_AGE_DAYS = 5  # 数据过期警告天数
FETCH_DAYS = 60      # 从 API 获取的天数
TIMEOUT = 15         # HTTP 超时秒数

# 用于 vol20 计算的核心 ETF (与用户原版一致)
VOL_CORE_CODES = ["510300", "159915", "513100", "518880"]

# 中国大陆时区
CN_TZ = timezone(timedelta(hours=8))

# ============================================================
# 数据获取
# ============================================================

def market_prefix(code):
    """判断市场前缀: sh / sz"""
    if code.startswith(("6", "5", "11")):
        return "sh"
    return "sz"


def fetch_klines(code, market, days=FETCH_DAYS):
    """从新浪财经获取日K线数据"""
    url = (
        f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={market}{code}"
        f"&datalen={days}&scale=240&ma=no"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://finance.sina.com.cn",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("gbk")
    except Exception as e:
        print(f"  [!] {code} 请求失败: {e}")
        return None

    if not raw or raw.strip() == "null":
        print(f"  [!] {code} 返回空数据")
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [!] {code} JSON解析失败: {e}")
        return None

    if not isinstance(data, list) or len(data) < N:
        print(f"  [!] {code} 数据不足 ({len(data) if isinstance(data, list) else 0})")
        return None

    # 过滤 volume=0 (与原版一致)
    valid = [d for d in data if float(d.get("volume", 0)) > 0]
    if len(valid) < N:
        print(f"  [!] {code} 有效数据不足 (volume>0: {len(valid)})")
        return None

    print(f"  [OK] {code}: {len(valid)} 条有效数据 (共 {len(data)} 条)")
    return valid


# ============================================================
# 核心算法 — 与原版 Scriptable 完全一致
# ============================================================

def linreg(x, y):
    """
    一元线性回归
    返回: {slope, intercept}
    """
    n = len(x)
    sx = sum(x)
    sy = sum(y)
    sxx = sum(xi * xi for xi in x)
    sxy = sum(x[i] * y[i] for i in range(n))

    denom = n * sxx - sx * sx
    if denom == 0:
        return {"slope": 0.0, "intercept": 0.0}

    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return {"slope": slope, "intercept": intercept}


def calc_score(closes):
    """
    动量得分 = 年化收益率 * R²
    步骤:
      1. 取最近 N 个收盘价
      2. y = ln(close), x = 0..N-1
      3. 线性回归
      4. annualRet = exp(slope * TRADING_DAYS) - 1
      5. R² = 1 - SSres/SStot
      6. score = annualRet * R²
    """
    c = closes[-N:]
    y = [math.log(x) for x in c]
    x = list(range(N))

    reg = linreg(x, y)
    annual_ret = math.exp(reg["slope"] * TRADING_DAYS) - 1

    # R²
    y_pred = [reg["slope"] * xi + reg["intercept"] for xi in x]
    y_mean = sum(y) / len(y)
    ss_res = sum((y[i] - y_pred[i]) ** 2 for i in range(len(y)))
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)

    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    return annual_ret * r2


def calc_vol20(all_data):
    """
    计算 vol20: 各ETF最近21日年化波动率的均值
    与原版 Scriptable calcVol20 一致
    """
    vols = []
    for code in VOL_CORE_CODES:
        data = all_data.get(code)
        if not data or len(data) < VOL_WINDOW:
            continue

        prices = [float(d["close"]) for d in data[-VOL_WINDOW:]]
        rets = [(prices[i] - prices[i - 1]) / prices[i - 1]
                for i in range(1, len(prices))]

        mean = sum(rets) / len(rets)
        variance = sum((r - mean) ** 2 for r in rets) / len(rets)
        std = math.sqrt(variance)
        vol = std * math.sqrt(TRADING_DAYS) * 100
        vols.append(vol)

    return sum(vols) / len(vols) if vols else 0.0


# ============================================================
# 主逻辑
# ============================================================

def run():
    """
    执行选股, 返回结果字典.
    与原版 Scriptable run() 一致.
    """
    all_data = {}
    results = []
    newest_date = None

    for etf in ETF_LIST:
        code = etf["code"]
        name = etf["name"]
        market = etf["market"]

        raw = fetch_klines(code, market)
        if raw is None:
            results.append({"code": code, "name": name, "score": 0.0, "valid": False})
            continue

        all_data[code] = raw
        closes = [float(d["close"]) for d in raw]
        last_date = raw[-1]["day"]

        if newest_date is None or last_date > newest_date:
            newest_date = last_date

        sc = calc_score(closes)
        results.append({
            "code": code,
            "name": name,
            "score": sc,
            "valid": True,
            "price": closes[-1],
            "date": last_date,
        })

    # 按得分降序排列
    results.sort(key=lambda r: r["score"], reverse=True)

    vol20 = calc_vol20(all_data)
    best = results[0] if results else None
    triggered = vol20 >= VOL_THRESHOLD

    return {
        "results": results,
        "vol20": vol20,
        "best": best,
        "triggered": triggered,
        "newest_date": newest_date,
        "run_time": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


# ============================================================
# 格式化输出
# ============================================================

def format_report(data):
    """生成文字报告 (与原版 showResult 风格一致)"""
    now = datetime.now(CN_TZ)
    time_str = now.strftime("%m-%d %H:%M")

    # 数据过期检查
    data_warning = ""
    if data["newest_date"]:
        try:
            dt = datetime.strptime(data["newest_date"], "%
