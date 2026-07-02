#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 轮动选股器 — 云端版 (纯 Python, 无外部依赖)
方案 C: 4 ETF 动量轮动 + VOL20>35 清仓转逆回购

口径对齐 (2026-06-29 修正):
- 动量公式: (exp(slope*250)-1) * R^2, N=25
- VOL20: 对数收益率, STD ddof=0, √250, 20 个收益率 (取 21 个 close)
- 数据源: 东财前复权日线 (避免 ETF 拆分失真)
- 阈值: 0.35 (35%), 触发后清仓 ETF 全仓 GC001/R-001
"""
import json, math, sys, urllib.request, os, argparse
from datetime import datetime, timezone, timedelta

ETF_LIST = [
    {"code": "510300", "name": "沪深300 ETF", "market": "sh"},
    {"code": "159915", "name": "创业板 ETF",  "market": "sz"},
    {"code": "513100", "name": "纳指 ETF",    "market": "sh"},
    {"code": "518880", "name": "黄金 ETF",    "market": "sh"},
]

N = 25
VOL_WINDOW = 21
VOL_THRESHOLD = 35
TRADING_DAYS = 250
FETCH_DAYS = 60
TIMEOUT = 15
VOL_CORE_CODES = ["510300", "159915", "513100", "518880"]
CN_TZ = timezone(timedelta(hours=8))


def fetch_klines(code, market, days=FETCH_DAYS):
    secid = f"1.{code}" if market == "sh" else f"0.{code}"
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f2,f3"
           f"&fields2=f51,f52,f53,f54,f55,f56"
           f"&klt=101&fqt=1&end=20500101&lmt={days}")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/"
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        print(f"  [ERR] {code}: 网络异常 {e}")
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if not d.get("data") or not d["data"].get("klines"):
        return None
    rows = [k.split(",") for k in d["data"]["klines"]]
    valid = [{
        "day":   r[0],
        "open":  float(r[1]),
        "close": float(r[2]),
        "high":  float(r[3]),
        "low":   float(r[4]),
        "volume": float(r[5]),
    } for r in rows if float(r[5]) > 0]
    if len(valid) < N:
        return None
    print(f"  [OK] {code}: {len(valid)} 条 ({valid[0]['day']} ~ {valid[-1]['day']})")
    return valid


def calc_score(closes):
    c = closes[-N:]
    y = [math.log(x) for x in c]
    x = list(range(N))
    n = len(x)
    sx = sum(x); sy = sum(y)
    sxx = sum(xi * xi for xi in x)
    sxy = sum(x[i] * y[i] for i in range(n))
    denom = n * sxx - sx * sx
    if denom == 0: return 0
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    annual = math.exp(slope * TRADING_DAYS) - 1
    y_pred = [slope * xi + intercept for xi in x]
    ym = sum(y) / len(y)
    ssr = sum((y[i] - y_pred[i]) ** 2 for i in range(len(y)))
    sst = sum((yi - ym) ** 2 for yi in y)
    r2 = 1 - ssr / sst if sst > 0 else 0
    return annual * r2


def calc_vol20(all_data):
    vols = []
    details = []
    for code in VOL_CORE_CODES:
        d = all_data.get(code)
        if not d or len(d) < VOL_WINDOW:
            continue
        p = [float(x["close"]) for x in d[-VOL_WINDOW:]]
        r = [math.log(p[i] / p[i - 1]) for i in range(1, len(p))]
        m = sum(r) / len(r)
        v = sum((ri - m) ** 2 for ri in r) / len(r)
        vol = math.sqrt(v) * math.sqrt(TRADING_DAYS) * 100
        vols.append(vol)
        name = next(e["name"] for e in ETF_LIST if e["code"] == code)
        details.append((name, vol))
    avg = sum(vols) / len(vols) if vols else 0
    return avg, details


def send_feishu(webhook_url, data):
    lines = []
    lines.append("📊 ETF轮动选股报告 " + datetime.now(CN_TZ).strftime("%m-%d %H:%M"))
    lines.append("")
    for i, r in enumerate(data["results"]):
        medals = ["🥇", "🥈", "🥉"]
        icon = medals[i] if i < 3 else "  "
        sc = f"{r['score']:.4f}" if r.get("valid", True) else "N/A"
        lines.append(f"{icon} {r['name']} {sc}")
    lines.append("")
    lines.append(f"vol20 均值: {data['vol20']:.2f}%  阈值 {VOL_THRESHOLD}%")
    for name, v in data.get("vol_details", []):
        lines.append(f"  · {name} {v:.2f}%")
    lines.append("")
    if data["triggered"]:
        lines.append("⚠️ 触发风控: 清仓 ETF → 全仓 GC001/R-001")
    else:
        lines.append(f"✅ 仓位: 满仓 {data['best']['name']}  (距离阈值 {data['vol20']-VOL_THRESHOLD:+.2f}pp)")
    lines.append("")
    lines.append("明日 09:30 开盘执行")
    lines.append("触发: 14:50 前买 GC001 / R-001")
    lines.append("国庆节后两天内强制逆回购")
    lines.append("2+标的>0.35 OR 均>0.35")

    text = "\n".join(lines)
    payload = json.dumps({
        "msg_type": "text",
        "content": {"text": text}
    }).encode("utf-8")

    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            result = resp.read().decode()
            print(f"[Feishu] 发送结果: {result}")
            return True
    except Exception as e:
        print(f"[Feishu] 发送失败: {e}")
        return False


def run():
    all_data = {}; results = []; newest_date = None
    for etf in ETF_LIST:
        raw = fetch_klines(etf["code"], etf["market"])
        if raw is None:
            results.append({"code": etf["code"], "name": etf["name"],
                            "score": 0, "valid": False})
            continue
        all_data[etf["code"]] = raw
        closes = [float(d["close"]) for d in raw]
        last = raw[-1]["day"]
        if newest_date is None or last > newest_date:
            newest_date = last
        sc = calc_score(closes)
        results.append({"code": etf["code"], "name": etf["name"],
                        "score": sc, "valid": True,
                        "price": closes[-1], "date": last})
    results.sort(key=lambda r: r["score"], reverse=True)
    vol20, vol_details = calc_vol20(all_data)
    best = results[0] if results else None
    return {"results": results, "vol20": vol20, "vol_details": vol_details,
            "best": best, "triggered": vol20 >= VOL_THRESHOLD,
            "newest_date": newest_date}


def main():
    parser = argparse.ArgumentParser(description="ETF轮动选股器")
    parser.add_argument("--feishu", action="store_true", help="发送结果到飞书 Webhook")
    args = parser.parse_args()

    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "")
    if args.feishu and not webhook_url:
        print("[错误] 请设置 FEISHU_WEBHOOK_URL 环境变量")
        sys.exit(1)

    print("=" * 50)
    print("  ETF轮动选股器 (方案C)  " + datetime.now(CN_TZ).strftime("%m-%d %H:%M"))
    print("=" * 50)
    data = run()
    for i, r in enumerate(data["results"]):
        icon = ["1", "2", "3"][i] if i < 3 else "  "
        sc = f"{r['score']:.4f}" if r.get("valid", True) else "N/A"
        print(f"  {icon} {r['name']:<8} {sc}")
    print("-" * 50)
    print(f"  vol20 (4源均值): {data['vol20']:.2f}%  阈值 {VOL_THRESHOLD}%")
    for name, v in data.get("vol_details", []):
        print(f"    - {name:<10} {v:>6.2f}%")
    print("-" * 50)
    if data["triggered"]:
        print(f"  ⚠ 触发风控: 清仓 ETF → 全仓 GC001/R-001")
    else:
        print(f"  仓位: 满仓 {data['best']['name']}  (距离阈值 {data['vol20']-VOL_THRESHOLD:+.2f}pp)")
    print("-" * 50)
    print("  明日 09:30 开盘执行")
    print("  触发: 14:50 前买 GC001 / R-001")
    print("=" * 50)

    if args.feishu:
        print("\n--- 发送到飞书 ---")
        send_feishu(webhook_url, data)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
