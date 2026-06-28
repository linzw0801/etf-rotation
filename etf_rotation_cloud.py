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
            dt = datetime.strptime(data["newest_date"], "%Y-%m-%d")
            age_days = (datetime.now() - dt).days
            if age_days > DATA_MAX_AGE_DAYS:
                data_warning = f"\n⚠ 数据已过期 {age_days} 天"
        except (ValueError, TypeError):
            pass

    lines = []
    lines.append(f"📊 ETF轮动选股报告  {time_str}")
    lines.append("=" * 45)

    # 排名
    for i, r in enumerate(data["results"]):
        icon = ["🥇", "🥈", "🥉"][i] if i < 3 else "   "
        score_str = f"{r['score']:.4f}" if r.get("valid", True) else "N/A"
        lines.append(f"  {icon} {r['name']:　<8} {score_str}")

    lines.append("-" * 45)
    lines.append(f"  vol20: {data['vol20']:.1f}%  {'⚠ 触发' if data['triggered'] else '正常'}")

    if data["triggered"]:
        lines.append(f"  仓位: 半仓 {data['best']['name']} + 50% 逆回购")
    else:
        lines.append(f"  仓位: 满仓 {data['best']['name']}")

    if data_warning:
        lines.append(data_warning)

    lines.append("-" * 45)
    lines.append("  明日 09:30 开盘执行")
    lines.append("  若降仓: 14:50 前买 GC001 / R-001")
    lines.append("=" * 45)

    return "\n".join(lines)


def build_html_report(data):
    """生成 HTML 邮件正文"""
    now = datetime.now(CN_TZ)
    time_str = now.strftime("%m-%d %H:%M")

    rows_html = ""
    for i, r in enumerate(data["results"]):
        icon = ["🥇", "🥈", "🥉"][i] if i < 3 else "　"
        cls = "rank-first" if i == 0 else ("rank-other" if i < 3 else "")
        score_str = f"{r['score']:.4f}" if r.get("valid", True) else "N/A"
        rows_html += f"""<tr class="{cls}">
          <td class="rank">{icon} {i+1}</td>
          <td class="name">{r['name']}</td>
          <td class="score">{score_str}</td>
          <td class="code">{r['code']}</td>
        </tr>"""

    trigger_class = "trigger-yes" if data["triggered"] else "trigger-no"
    trigger_text = "⚠ 触发降仓" if data["triggered"] else "正常"
    position_text = f"半仓 {data['best']['name']} + 50% 逆回购" if data["triggered"] else f"满仓 {data['best']['name']}"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>ETF轮动报告</title>
<style>
body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; background: #0d1117; color: #e6edf3; padding: 20px; }}
.container {{ max-width: 520px; margin: 0 auto; }}
.header {{ background: linear-gradient(135deg, #1a1a2e, #16213e); border-radius: 12px; padding: 16px; text-align: center; margin-bottom: 14px; }}
.header h1 {{ margin: 0; font-size: 18px; color: #58a6ff; }}
.header .time {{ font-size: 12px; color: #8b949e; margin-top: 4px; }}
.signal {{ border-radius: 12px; padding: 14px; text-align: center; margin-bottom: 14px; border: 2px solid; }}
.signal.buy {{ border-color: #238636; }}
.signal.caution {{ border-color: #d29922; }}
.signal .action {{ font-size: 20px; font-weight: 700; }}
.signal .detail {{ font-size: 13px; color: #8b949e; margin-top: 4px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; background: #161b22; border-radius: 10px; overflow: hidden; margin-bottom: 14px; }}
th {{ background: #1c2128; color: #8b949e; font-weight: 500; padding: 8px 6px; text-align: left; border-bottom: 1px solid #30363d; }}
td {{ padding: 8px 6px; border-bottom: 1px solid #21262d; }}
tr:last-child td {{ border-bottom: none; }}
.rank {{ width: 50px; text-align: center; }}
.rank-first td {{ color: #f0883e; font-weight: 700; }}
.name {{ font-weight: 600; }}
.score {{ text-align: right; font-family: monospace; }}
.code {{ text-align: right; color: #484f58; font-size: 11px; }}
.card {{ background: #161b22; border-radius: 10px; padding: 12px; margin-bottom: 14px; border: 1px solid #30363d; }}
.card-title {{ font-size: 12px; color: #8b949e; margin-bottom: 8px; }}
.row {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #21262d; }}
.row:last-child {{ border-bottom: none; }}
.label {{ color: #8b949e; }}
.value {{ font-weight: 600; }}
.{trigger_class} .value {{ color: {'#da3633' if data['triggered'] else '#3fb950'}; }}
.footer {{ text-align: center; color: #484f58; font-size: 11px; margin-top: 20px; }}
</style></head>
<body><div class="container">
<div class="header"><h1>📊 ETF 轮动选股</h1><div class="time">{time_str}</div></div>
<div class="signal {'caution' if data['triggered'] else 'buy'}">
<div class="action">{position_text}</div>
<div class="detail">vol20: {data['vol20']:.1f}% | {trigger_text}</div>
</div>
<table><tr><th></th><th>ETF</th><th style="text-align:right">得分</th><th style="text-align:right">代码</th></tr>
{rows_html}
</table>
<div class="card">
<div class="card-title">📋 风控指标</div>
<div class="row"><span class="label">vol20 (20日年化波动)</span><span class="value" style="color:{'#da3633' if data['triggered'] else '#3fb950'}">{data['vol20']:.1f}%</span></div>
<div class="row"><span class="label">阈值</span><span class="value">{VOL_THRESHOLD}%</span></div>
<div class="row"><span class="label">仓位</span><span class="value" style="color:{'#d29922' if data['triggered'] else '#3fb950'}">{'半仓' if data['triggered'] else '满仓'}</span></div>
</div>
<div class="card">
<div class="card-title">📌 操作提示</div>
<div class="row"><span class="label">执行时间</span><span class="value">明日 09:30</span></div>
<div class="row"><span class="label">若降仓</span><span class="value">14:50 前买 GC001/R-001</span></div>
</div>
<div class="footer">数据来源: 新浪财经 | 策略: ETF轮动 v5 (4源vol + 阈值{VOL_THRESHOLD}% + 降仓50% + 逆回购)</div>
</div></body></html>"""
    return html


# ============================================================
# 邮件发送 (QQ邮箱 SMTP) — 修复版
# ============================================================

def send_email(text_body, html_body, to_addr, from_addr, password):
    """通过 QQ邮箱 SMTP 发送邮件（支持 SSL 和 STARTTLS 两种方式）"""
    subject = f"ETF轮动选股报告 {datetime.now(CN_TZ).strftime('%m-%d')}"

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("ETF轮动选股器", from_addr))
    msg["To"] = to_addr

    # 检查邮箱格式
    if "@" not in from_addr:
        print(f"  [X] EMAIL_FROM 格式错误: {from_addr}")
        print(f"      应该是完整的邮箱地址如 xxx@qq.com")
        return False

    # 依次尝试 SSL(465) 和 STARTTLS(587)
    methods = [
        ("smtp.qq.com", 465, True,  "SSL"),
        ("smtp.qq.com", 587, False, "STARTTLS"),
    ]

    for host, port, use_ssl, mode in methods:
        try:
            print(f"  [*] 尝试 QQ邮箱 {mode} ({host}:{port})...")
            if use_ssl:
                server = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                server.ehlo()
                server.starttls()
                server.ehlo()

            server.login(from_addr, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
            server.quit()
            print(f"  [OK] ✅ 邮件已成功发送至 {to_addr}")
            return True

        except smtplib.SMTPAuthenticationError:
            print(f"  [X] ❌ 认证失败！邮箱或授权码错误")
            print(f"      提示: QQ邮箱必须用『授权码』（16位），不是登录密码")
            print(f"      去 https://mail.qq.com → 设置 → 账户 → 生成授权码")
            return False
        except smtplib.SMTPException as e:
            print(f"  [X] {mode} 失败: {e}")
            continue
        except Exception as e:
            print(f"  [X] {mode} 异常: {e}")
            continue

    print(f"  [X] ❌ 所有方式都失败了，请检查:")
    print(f"      1. EMAIL_FROM 是否是完整邮箱 (xxx@qq.com)")
    print(f"      2. EMAIL_PASSWORD 是否是16位授权码（不是登录密码）")
    print(f"      3. 授权码是否过期（过期需重新生成）")
    print(f"      4. 是否开启了 QQ邮箱的 SMTP 服务")
    return False


# ============================================================
# 命令行入口
# ============================================================

def main():
    print("=" * 50)
    print("  ETF轮动选股器 — 云端版")
    print("  " + datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 50)

    data = run()

    report = format_report(data)
    print(f"\n{report}\n")

    # 邮件发送模式
    if "--smtp" in sys.argv:
        from_addr = os.environ.get("EMAIL_FROM", "")
        password = os.environ.get("EMAIL_PASSWORD", "")
        to_addr = os.environ.get("EMAIL_TO", from_addr)

        if not from_addr or not password:
            print("[X] 环境变量 EMAIL_FROM / EMAIL_PASSWORD 未设置")
            print("     GitHub Actions 中通过 Secrets 设置")
            sys.exit(1)

        print("[*] 准备发送邮件...")
        html_body = build_html_report(data)
        send_email(report, html_body, to_addr, from_addr, password)

    elif "--email" in sys.argv:
        idx = sys.argv.index("--email")
        if idx + 1 < len(sys.argv):
            to_addr = sys.argv[idx + 1]
        else:
            print("[X] --email 需要收件地址参数")
            sys.exit(1)

        from_addr = os.environ.get("EMAIL_FROM", "")
        password = os.environ.get("EMAIL_PASSWORD", "")

        if not from_addr or not password:
            print("[X] 需要设置 EMAIL_FROM 和 EMAIL_PASSWORD 环境变量")
            sys.exit(1)

        print(f"[*] 发送邮件至 {to_addr}...")
        html_body = build_html_report(data)
        send_email(report, html_body, to_addr, from_addr, password)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
