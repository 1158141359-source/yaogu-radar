import os
import math
import time
import datetime as dt
from datetime import timedelta
from typing import Optional, Dict, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

import requests
from requests.exceptions import RequestException, ConnectionError
from fastapi import FastAPI
from fastapi.responses import HTMLResponse


# =========================================================
# 妖股雷达 v3
# 东方财富公开行情数据
# =========================================================

app = FastAPI(title="妖股雷达")

TZ = ZoneInfo("Asia/Shanghai")

EASTMONEY_QT = "https://push2.eastmoney.com/api/qt/stock/get"
EASTMONEY_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_LHB = "https://datacenter-web.eastmoney.com/api/data/v1/get"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://quote.eastmoney.com/",
    "Connection": "close",
}

# 避免东方财富接口瞬间大量请求
REQUEST_GAP = 0.08


class EastMoneyError(Exception):
    pass


# =========================================================
# 网络请求：自动重试
# =========================================================

def get_json(url, params, timeout=(6, 18), retries=3):
    last_error = None

    for attempt in range(retries):
        try:
            time.sleep(REQUEST_GAP)

            r = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=timeout,
                allow_redirects=True,
            )

            r.raise_for_status()

            data = r.json()

            if data is None:
                raise EastMoneyError("东方财富返回空数据")

            return data

        except Exception as e:
            last_error = e

            if attempt < retries - 1:
                time.sleep(0.8 * (attempt + 1))
            else:
                break

    raise EastMoneyError(
        f"东方财富接口连接失败：{type(last_error).__name__}: {last_error}"
    )


# =========================================================
# 工具
# =========================================================

def safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def safe_int(v, default=0):
    try:
        if v is None:
            return default
        return int(float(v))
    except Exception:
        return default


def fmt_money(v):
    v = safe_float(v)

    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿"

    if abs(v) >= 1e4:
        return f"{v / 1e4:.2f}万"

    return f"{v:.0f}"


def pct(v):
    return f"{safe_float(v):.2f}%"


def today_cn():
    return dt.datetime.now(TZ).date()


# =========================================================
# 股票代码
# =========================================================

def market_prefix(code: str):
    code = str(code)

    if code.startswith("6"):
        return "1"

    if code.startswith(("0", "3")):
        return "0"

    if code.startswith(("4", "8")):
        return "0"

    return "0"


def secid(code: str):
    return f"{market_prefix(code)}.{code}"


# =========================================================
# 交易日
# =========================================================

def get_trading_dates(end_date: str, days=100):
    params = {
        "secid": "1.000001",
        "klt": "101",
        "fqt": "1",
        "beg": "",
        "end": end_date.replace("-", ""),
        "fields1": "f1",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }

    data = get_json(EASTMONEY_KLINE, params)

    rows = ((data or {}).get("data") or {}).get("klines") or []

    dates = []

    for row in rows:
        parts = str(row).split(",")
        if parts:
            d = parts[0]
            if len(d) >= 10:
                dates.append(d[:10])

    dates = sorted(set(dates))

    if days:
        return dates[-days:]

    return dates


def actual_target_date(target_date: str):
    """
    自动寻找 <= 用户选择日期的最近交易日。
    """

    dates = get_trading_dates(target_date, 120)

    if dates:
        return dates[-1]

    raise EastMoneyError(
        "无法确认交易日，东方财富历史行情接口暂时不可用"
    )


# =========================================================
# 股票列表
# =========================================================

def get_stock_list(limit=500):
    """
    获取A股股票。
    先按活跃度筛选，降低历史K线请求量。
    """

    result = []

    page_size = 100
    pages = math.ceil(limit / page_size)

    for page in range(1, pages + 1):

        params = {
            "pn": page,
            "pz": page_size,
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f2,f3,f8,f12,f14,f20,f21,f100",
        }

        try:
            data = get_json(EASTMONEY_CLIST, params)
        except Exception:
            continue

        rows = ((data or {}).get("data") or {}).get("diff") or []

        if not rows:
            break

        for x in rows:

            code = str(x.get("f12") or "")
            name = str(x.get("f14") or "")

            if not code or not name:
                continue

            # 排除ST、退市整理、风险股
            if "ST" in name.upper():
                continue

            if "退" in name:
                continue

            market_cap = safe_float(x.get("f20"))

            # 300亿以内
            if market_cap > 300 * 1e8:
                continue

            result.append({
                "code": code,
                "name": name,
                "pct": safe_float(x.get("f3")),
                "turnover": safe_float(x.get("f8")),
                "market_cap": market_cap,
            })

            if len(result) >= limit:
                return result

    return result


# =========================================================
# K线
# =========================================================

def get_kline(code: str, beg: str, end: str):
    params = {
        "secid": secid(code),
        "klt": "101",
        "fqt": "1",
        "beg": beg,
        "end": end,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }

    data = get_json(EASTMONEY_KLINE, params)

    rows = ((data or {}).get("data") or {}).get("klines") or []

    result = []

    for row in rows:

        p = str(row).split(",")

        if len(p) < 11:
            continue

        result.append({
            "date": p[0],
            "open": safe_float(p[1]),
            "close": safe_float(p[2]),
            "high": safe_float(p[3]),
            "low": safe_float(p[4]),
            "volume": safe_float(p[5]),
            "amount": safe_float(p[6]),
            "amplitude": safe_float(p[7]),
            "pct": safe_float(p[8]),
            "change": safe_float(p[9]),
            "turnover": safe_float(p[10]),
        })

    return result


# =========================================================
# 当前行情
# =========================================================

def get_quote(code: str):

    params = {
        "secid": secid(code),
        "fields": (
            "f43,f57,f58,f60,f116,f117,f168,f170,"
            "f39,f40,f37,f38,f35,f36,f33,f34,f31,f32,"
            "f19,f20,f17,f18,f15,f16,f13,f14,f11,f12"
        ),
    }

    try:
        data = get_json(EASTMONEY_QT, params)
        return (data or {}).get("data") or {}
    except Exception:
        return {}


# =========================================================
# 龙虎榜
# =========================================================

def get_lhb(trade_date: str):

    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": (
            "SECURITY_CODE,SECURITY_NAME_ABBR,"
            "BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,"
            "BILLBOARD_SELL_AMT,TRADE_DATE"
        ),
        "filter": f'(TRADE_DATE=\'{trade_date}\')',
        "pageNumber": "1",
        "pageSize": "500",
        "sortColumns": "BILLBOARD_NET_AMT",
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    }

    try:
        data = get_json(EASTMONEY_LHB, params)

        rows = ((data or {}).get("result") or {}).get("data") or []

        result = {}

        for x in rows:

            code = str(
                x.get("SECURITY_CODE")
                or x.get("SECURITYCODE")
                or ""
            )

            if not code:
                continue

            result[code] = {
                "net": safe_float(x.get("BILLBOARD_NET_AMT")),
                "buy": safe_float(x.get("BILLBOARD_BUY_AMT")),
                "sell": safe_float(x.get("BILLBOARD_SELL_AMT")),
            }

        return result, None

    except Exception as e:
        return {}, str(e)


# =========================================================
# 五大硬条件
# =========================================================

def calc_hard_conditions(k):

    if len(k) < 30:
        return {
            "c1": False,
            "c2": False,
            "c3": False,
            "c4": False,
            "c5": False,
        }

    closes = [x["close"] for x in k]
    volumes = [x["volume"] for x in k]

    # -----------------------------------------------------
    # ① 连续5日上涨
    # -----------------------------------------------------

    c1 = True

    last5 = k[-5:]

    if len(last5) < 5:
        c1 = False
    else:
        for i in range(1, 5):
            if last5[i]["close"] <= last5[i - 1]["close"]:
                c1 = False
                break

    # -----------------------------------------------------
    # ② 30日内有过涨停
    #
    # 主板按接近10%识别。
    # 创业板/科创板按接近20%识别。
    # -----------------------------------------------------

    c2 = False

    for x in k[-30:]:
        p = abs(x["pct"])

        if p >= 9.5:
            c2 = True
            break

    # -----------------------------------------------------
    # ③ 收盘价不破5日线
    # -----------------------------------------------------

    if len(closes) >= 5:
        ma5 = sum(closes[-5:]) / 5
        c3 = closes[-1] >= ma5 * 0.995
    else:
        c3 = False

    # -----------------------------------------------------
    # ④ 堆量成交量
    #
    # 最近3日平均成交量 > 前10日平均成交量
    # -----------------------------------------------------

    if len(volumes) >= 13:
        recent_avg = sum(volumes[-3:]) / 3
        old_avg = sum(volumes[-13:-3]) / 10

        c4 = recent_avg >= old_avg * 1.15
    else:
        c4 = False

    # -----------------------------------------------------
    # ⑤ 底部筹码不动
    #
    # 公开行情接口无法直接得到完整历史筹码分布，
    # 因此使用K线代理：
    # 最近20日低点没有明显破坏，同时量价保持稳定。
    # -----------------------------------------------------

    if len(k) >= 20:

        last20 = k[-20:]

        lowest = min(x["low"] for x in last20)
        recent_low = min(x["low"] for x in last20[-5:])

        avg_vol = sum(x["volume"] for x in last20) / 20
        recent_vol = sum(x["volume"] for x in last20[-5:]) / 5

        low_stable = recent_low >= lowest * 0.97
        volume_stable = recent_vol <= avg_vol * 2.5

        c5 = low_stable and volume_stable

    else:
        c5 = False

    return {
        "c1": c1,
        "c2": c2,
        "c3": c3,
        "c4": c4,
        "c5": c5,
    }


# =========================================================
# 买卖信号
# =========================================================

def get_signals(k):

    if len(k) < 10:
        return {
            "buy": "等待",
            "hold": "观察",
            "sell": "暂不卖出",
            "patterns": [],
        }

    patterns = []

    last = k[-1]
    prev = k[-2]

    # -----------------------------------------------------
    # 仙人指路
    # -----------------------------------------------------

    body = abs(last["close"] - last["open"])
    upper_shadow = last["high"] - max(last["open"], last["close"])

    if body > 0 and upper_shadow >= body * 1.5:
        patterns.append("仙人指路")

    # -----------------------------------------------------
    # 双剑合璧
    # -----------------------------------------------------

    if len(k) >= 6:

        ma5 = sum(x["close"] for x in k[-5:]) / 5
        ma10 = sum(x["close"] for x in k[-10:]) / 10

        if last["close"] >= ma5 and last["close"] >= ma10:
            patterns.append("双剑合璧")

    # -----------------------------------------------------
    # 倚天剑
    # -----------------------------------------------------

    if len(k) >= 20:

        high20 = max(x["high"] for x in k[-20:])

        if last["close"] >= high20 * 0.96:
            patterns.append("倚天剑")

    # -----------------------------------------------------
    # 屠龙刀
    # -----------------------------------------------------

    if len(k) >= 10:

        high10 = max(x["high"] for x in k[-10:])

        if last["high"] >= high10 and last["close"] < last["open"]:
            patterns.append("屠龙刀")

    # -----------------------------------------------------
    # 援军战法 / 阴线
    # -----------------------------------------------------

    if last["close"] < last["open"]:

        if last["low"] < prev["low"] and last["close"] < prev["close"]:
            patterns.append("破位阴")

        elif last["volume"] > prev["volume"] * 1.5:
            patterns.append("加速阴")

        else:
            patterns.append("反转阴")

    # -----------------------------------------------------
    # 买点
    # -----------------------------------------------------

    if "反转阴" in patterns:
        buy = "反转阴低点附近 + 援军确认"
    elif "双剑合璧" in patterns:
        buy = "双剑合璧突破/回踩"
    elif "仙人指路" in patterns:
        buy = "仙人指路确认后"
    else:
        buy = "等待回踩确认"

    # -----------------------------------------------------
    # 持有
    # 高点高、低点高、收盘高
    # -----------------------------------------------------

    if (
        last["high"] >= prev["high"]
        and last["low"] >= prev["low"]
        and last["close"] >= prev["close"]
    ):
        hold = "高点高、低点高、收盘高：继续持有"
    else:
        hold = "走势转弱：观察"

    # -----------------------------------------------------
    # 卖出
    # -----------------------------------------------------

    if (
        last["high"] < prev["high"]
        and last["close"] <= prev["close"]
        and last["low"] < prev["low"]
    ):
        sell = "警戒卖出：高低收同时转弱"

    elif "破位阴" in patterns:
        sell = "警戒卖出：出现破位阴"

    else:
        sell = "未触发卖出规则"

    return {
        "buy": buy,
        "hold": hold,
        "sell": sell,
        "patterns": patterns,
    }


# =========================================================
# 综合评分
# =========================================================

def score_stock(k, hard, lhb):

    score = 0

    # -----------------------------------------------------
    # 五大硬条件
    # -----------------------------------------------------

    weights = {
        "c1": 22,
        "c2": 22,
        "c3": 18,
        "c4": 20,
        "c5": 18,
    }

    for key, weight in weights.items():
        if hard.get(key):
            score += weight

    # -----------------------------------------------------
    # 近期涨幅
    # -----------------------------------------------------

    last_pct = safe_float(k[-1]["pct"])

    if last_pct >= 5:
        score += 5

    # -----------------------------------------------------
    # 龙虎榜净买入
    # -----------------------------------------------------

    if lhb:

        net = safe_float(lhb.get("net"))

        if net > 0:
            score += 5

        if net > 50000000:
            score += 3

    # -----------------------------------------------------
    # 趋势强度
    # -----------------------------------------------------

    if len(k) >= 10:

        closes = [x["close"] for x in k]

        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10

        if ma5 > ma10:
            score += 3

    return min(100, int(score))


# =========================================================
# 单只股票分析
# =========================================================

def analyze_one(stock, target_date, lhb_map):

    code = stock["code"]

    try:

        end = target_date.replace("-", "")

        begin_dt = dt.datetime.strptime(
            target_date,
            "%Y-%m-%d"
        ).date() - timedelta(days=100)

        begin = begin_dt.strftime("%Y%m%d")

        k = get_kline(code, begin, end)

        if len(k) < 30:
            return None

        # 确保最后一根K线就是目标日期或之前最近数据
        k = [
            x for x in k
            if x["date"] <= target_date
        ]

        if len(k) < 30:
            return None

        hard = calc_hard_conditions(k)

        count = sum(
            1 for x in hard.values()
            if x
        )

        # 0-2条件直接过滤
        if count < 3:
            return None

        if count == 5:
            pool = "5/5 强势池"
        elif count == 4:
            pool = "4/5 高度接近"
        else:
            pool = "3/5 观察池"

        missing_map = {
            "c1": "连续5日上涨",
            "c2": "30日内有过涨停",
            "c3": "收盘不破5日线",
            "c4": "堆量成交量",
            "c5": "底部筹码不动",
        }

        missing = [
            missing_map[x]
            for x, ok in hard.items()
            if not ok
        ]

        lhb = lhb_map.get(code, {})

        signals = get_signals(k)

        score = score_stock(
            k,
            hard,
            lhb
        )

        last = k[-1]

        quote = get_quote(code)

        market_cap = safe_float(
            quote.get("f116"),
            stock.get("market_cap", 0)
        )

        # 如果实时接口没有返回市值，使用股票列表里的市值
        if market_cap <= 0:
            market_cap = stock.get("market_cap", 0)

        return {
            "code": code,
            "name": stock["name"],

            "close": last["close"],
            "pct": last["pct"],
            "turnover": last["turnover"],

            "market_cap": market_cap,

            "lhb": bool(lhb),
            "lhb_net": safe_float(
                lhb.get("net")
            ),

            "hard_count": count,
            "hard": hard,

            "pool": pool,
            "missing": missing,

            "score": score,

            "buy": signals["buy"],
            "hold": signals["hold"],
            "sell": signals["sell"],

            "patterns": signals["patterns"],

            "date": last["date"],
        }

    except Exception:
        # 单只股票失败，不影响整个扫描
        return None


# =========================================================
# 扫描
# =========================================================

def build_scan(requested_date: str):

    target_date = actual_target_date(
        requested_date
    )

    # 龙虎榜
    lhb_map, lhb_error = get_lhb(
        target_date
    )

    # 股票池
    stocks = get_stock_list(
        limit=500
    )

    if not stocks:
        raise EastMoneyError(
            "股票列表为空，东方财富行情接口可能暂时限流"
        )

    results = []

    # 4线程，降低东方财富限流概率
    with ThreadPoolExecutor(
        max_workers=4
    ) as executor:

        futures = [
            executor.submit(
                analyze_one,
                stock,
                target_date,
                lhb_map
            )
            for stock in stocks
        ]

        for future in as_completed(futures):

            try:
                item = future.result()

                if item:
                    results.append(item)

            except Exception:
                continue

    # 排序
    results.sort(
        key=lambda x: (
            x["hard_count"],
            x["score"],
            x["lhb_net"],
            x["pct"],
        ),
        reverse=True
    )

    strong = [
        x for x in results
        if x["hard_count"] == 5
    ]

    near = [
        x for x in results
        if x["hard_count"] == 4
    ]

    watch = [
        x for x in results
        if x["hard_count"] == 3
    ]

    # -----------------------------------------------------
    # TOP3
    #
    # 优先5/5
    # 没有5/5则4/5替补
    # 再没有才3/5观察
    # -----------------------------------------------------

    if strong:
        top3 = strong[:3]
        top_note = "今日5/5强势标的"
    elif near:
        top3 = near[:3]
        top_note = "今日无5/5，以下为4/5替补"
    else:
        top3 = watch[:3]
        top_note = "今日无5/5、4/5，以下仅作3/5观察"

    return {
        "date": target_date,
        "requested_date": requested_date,

        "top3": top3,

        "strong": strong[:50],
        "near": near[:50],
        "watch": watch[:50],

        "ranking": results[:100],

        "total": len(results),
        "stock_pool": len(stocks),

        "lhb_available": bool(lhb_map),
        "lhb_error": lhb_error,

        "top_note": top_note,

        "message": (
            "底部筹码不动为K线代理条件；"
            "当前盘口不作为历史硬条件。"
        ),
    }


# =========================================================
# HTML
# =========================================================

HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">

<head>

<meta charset="UTF-8">

<meta name="viewport"
      content="width=device-width,initial-scale=1,maximum-scale=1">

<title>🔥 妖股雷达</title>

<style>

*{
    box-sizing:border-box;
}

body{
    margin:0;
    background:#070707;
    color:#eee;
    font-family:-apple-system,BlinkMacSystemFont,
                 "PingFang SC","Microsoft YaHei",Arial;
}

.container{
    max-width:1100px;
    margin:auto;
    padding:14px;
}

.header{
    background:linear-gradient(135deg,#151515,#090909);
    border:1px solid #292929;
    border-radius:18px;
    padding:18px;
    margin-bottom:14px;
}

.title{
    font-size:26px;
    font-weight:800;
}

.subtitle{
    color:#999;
    font-size:13px;
    margin-top:6px;
}

.toolbar{
    display:flex;
    gap:8px;
    margin-top:15px;
}

input,button{
    border:0;
    border-radius:10px;
    padding:12px;
    font-size:15px;
}

input{
    flex:1;
    background:#1b1b1b;
    color:#fff;
    border:1px solid #333;
}

button{
    background:#e51b2b;
    color:#fff;
    font-weight:700;
    min-width:110px;
}

button:disabled{
    opacity:.5;
}

.card{
    background:#111;
    border:1px solid #292929;
    border-radius:16px;
    padding:15px;
    margin-bottom:12px;
}

.section-title{
    font-size:18px;
    font-weight:800;
    margin-bottom:12px;
}

.conditions{
    display:grid;
    grid-template-columns:repeat(5,1fr);
    gap:8px;
}

.condition{
    background:#181818;
    border-radius:10px;
    padding:10px;
    font-size:13px;
    text-align:center;
}

.top-note{
    color:#ffb020;
    font-size:13px;
    margin-bottom:10px;
}

.top3{
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:10px;
}

.stock-card{
    background:#171717;
    border:1px solid #333;
    border-radius:14px;
    padding:14px;
}

.stock-card.strong{
    border-color:#d92735;
}

.stock-card.near{
    border-color:#ff9d00;
}

.stock-card.watch{
    border-color:#c8a400;
}

.stock-name{
    font-size:20px;
    font-weight:800;
}

.stock-code{
    color:#777;
    font-size:12px;
}

.score{
    font-size:27px;
    font-weight:900;
    color:#ff4050;
    margin:8px 0;
}

.meta{
    color:#aaa;
    font-size:13px;
    line-height:1.8;
}

.signal{
    margin-top:10px;
    padding:9px;
    border-radius:9px;
    background:#0d0d0d;
    font-size:13px;
    line-height:1.7;
}

.good{
    color:#ff4d5c;
}

.warn{
    color:#ffb020;
}

.missing{
    color:#ff7b7b;
}

.pool-title{
    font-size:16px;
    font-weight:800;
    margin-bottom:8px;
}

.table-wrap{
    overflow:auto;
}

table{
    width:100%;
    border-collapse:collapse;
    min-width:720px;
}

th,td{
    padding:10px 8px;
    border-bottom:1px solid #292929;
    text-align:left;
    font-size:13px;
}

th{
    color:#999;
}

.empty{
    color:#777;
    padding:20px 0;
}

.loading{
    color:#ffb020;
}

.error{
    color:#ff5b6b;
    white-space:pre-wrap;
}

@media(max-width:700px){

    .conditions{
        grid-template-columns:repeat(2,1fr);
    }

    .top3{
        grid-template-columns:1fr;
    }

    .title{
        font-size:23px;
    }

}

</style>

</head>

<body>

<div class="container">

<div class="header">

<div class="title">🔥 妖股雷达</div>

<div class="subtitle">
5大硬条件 + 买点/持有/卖点规则 · 东方财富公开行情数据
</div>

<div class="toolbar">

<input id="date"
       type="date">

<button id="scan"
        onclick="scan()">
开始扫描
</button>

</div>

</div>


<div class="card">

<div class="section-title">
五大硬条件
</div>

<div class="conditions">

<div class="condition">
① 连续5日上涨
</div>

<div class="condition">
② 30日内有过涨停
</div>

<div class="condition">
③ 收盘不破5日线
</div>

<div class="condition">
④ 堆量成交量
</div>

<div class="condition">
⑤ 底部筹码不动
</div>

</div>

</div>


<div id="result">

<div class="card empty">
请选择日期后点击「开始扫描」
</div>

</div>

</div>


<script>

const today = new Date();

document.getElementById("date").value =
    today.toISOString().slice(0,10);


function money(v){

    v = Number(v || 0);

    if(Math.abs(v) >= 100000000){
        return (v / 100000000).toFixed(2) + "亿";
    }

    if(Math.abs(v) >= 10000){
        return (v / 10000).toFixed(2) + "万";
    }

    return v.toFixed(0);
}


function card(item){

    const hard = item.hard_count;

    let cls = "watch";

    if(hard === 5) cls = "strong";
    else if(hard === 4) cls = "near";

    const missing =
        item.missing && item.missing.length
        ? item.missing.join("、")
        : "无";

    const patterns =
        item.patterns && item.patterns.length
        ? item.patterns.join("、")
        : "暂无";

    return `

    <div class="stock-card ${cls}">

        <div class="stock-name">
            ${item.name}
        </div>

        <div class="stock-code">
            ${item.code}
        </div>

        <div class="score">
            ${item.score}分
        </div>

        <div class="meta">

            收盘：
            ${Number(item.close || 0).toFixed(2)}
            &nbsp;&nbsp;

            涨幅：
            ${Number(item.pct || 0).toFixed(2)}%

            <br>

            成交：
            ${Number(item.turnover || 0).toFixed(2)}%

            &nbsp;&nbsp;

            龙虎榜：
            ${item.lhb ? "是" : "否"}

            <br>

            龙虎榜净额：
            ${money(item.lhb_net)}

            <br>

            硬条件：
            ${hard}/5

        </div>

        <div class="signal">

            <div>
                <span class="good">买点：</span>
                ${item.buy}
            </div>

            <div>
                <span class="good">持有：</span>
                ${item.hold}
            </div>

            <div>
                <span class="warn">卖出：</span>
                ${item.sell}
            </div>

            <div>
                <span class="good">触发规则：</span>
                ${patterns}
            </div>

            ${
                hard < 5
                ?
                `<div>
                    <span class="missing">
                    缺少条件：
                    </span>
                    ${missing}
                </div>`
                :
                ""
            }

        </div>

    </div>

    `;
}


function pool(title, arr){

    if(!arr || !arr.length){
        return `
        <div class="card">
            <div class="pool-title">
                ${title}
            </div>
            <div class="empty">
                暂无符合标的
            </div>
        </div>
        `;
    }

    return `
    <div class="card">

        <div class="pool-title">
            ${title}
        </div>

        <div class="top3">

            ${arr.map(card).join("")}

        </div>

    </div>
    `;
}


async function scan(){

    const date =
        document.getElementById("date").value;

    const button =
        document.getElementById("scan");

    const result =
        document.getElementById("result");

    button.disabled = true;
    button.innerText = "扫描中...";

    result.innerHTML = `
        <div class="card loading">
            正在读取东方财富行情，请稍候...
        </div>
    `;

    try{

        const r = await fetch(
            "/api/scanner?date=" +
            encodeURIComponent(date)
        );

        const data = await r.json();

        if(data.error){

            result.innerHTML = `
                <div class="card error">
                    ${data.error}
                </div>
            `;

            return;
        }

        document.getElementById("date").value =
            data.date || date;

        let html = "";

        html += `
        <div class="card">

            <div class="section-title">
                TOP3 强势标的
            </div>

            <div class="top-note">
                ${data.top_note || ""}
            </div>

            <div class="top3">

                ${
                    data.top3 && data.top3.length
                    ?
                    data.top3.map(card).join("")
                    :
                    `<div class="empty">
                        今日暂无符合条件的标的
                    </div>`
                }

            </div>

        </div>
        `;

        html += pool(
            "5/5 强势池",
            data.strong
        );

        html += pool(
            "4/5 高度接近",
            data.near
        );

        html += pool(
            "3/5 观察池",
            data.watch
        );

        html += `

        <div class="card">

            <div class="section-title">
                综合评分排行
            </div>

            <div class="table-wrap">

            <table>

            <thead>

            <tr>
                <th>排名</th>
                <th>股票</th>
                <th>评分</th>
                <th>硬条件</th>
                <th>涨幅</th>
                <th>龙虎榜净额</th>
                <th>缺少条件</th>
            </tr>

            </thead>

            <tbody>

            ${
                (data.ranking || [])
                .map((x,i)=>`

                <tr>

                    <td>${i+1}</td>

                    <td>
                        ${x.name}
                        <br>
                        <small>${x.code}</small>
                    </td>

                    <td>${x.score}</td>

                    <td>${x.hard_count}/5</td>

                    <td>
                        ${Number(x.pct || 0).toFixed(2)}%
                    </td>

                    <td>
                        ${money(x.lhb_net)}
                    </td>

                    <td>
                        ${
                            x.missing && x.missing.length
                            ? x.missing.join("、")
                            : "无"
                        }
                    </td>

                </tr>

                `)
                .join("")
            }

            </tbody>

            </table>

            </div>

        </div>

        `;

        html += `

        <div class="card">

            <div class="meta">

                扫描日期：
                ${data.date}

                <br>

                股票池：
                ${data.stock_pool || 0}

                &nbsp;&nbsp;

                有效结果：
                ${data.total || 0}

                <br>

                ${
                    data.lhb_available
                    ?
                    "龙虎榜：已获取"
                    :
                    "龙虎榜：暂时不可用"
                }

                <br>

                ${data.message || ""}

            </div>

        </div>

        `;

        result.innerHTML = html;

    }
    catch(e){

        result.innerHTML = `
        <div class="card error">
            扫描失败：${e}
            <br><br>
            如果连续出现，请稍后再试。
        </div>
        `;

    }
    finally{

        button.disabled = false;
        button.innerText = "开始扫描";

    }

}

</script>

</body>

</html>
"""


# =========================================================
# API
# =========================================================

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)


@app.get("/api/scanner")
def scanner(date: Optional[str] = None):

    requested = date or today_cn().strftime("%Y-%m-%d")

    try:

        result = build_scan(requested)

        return result

    except Exception as e:

        return {
            "error": (
                "东方财富行情接口暂时无法连接。\n"
                "请稍后重新点击「开始扫描」。\n\n"
                f"详细信息：{e}"
            )
        }


# =========================================================
# 本地运行
# =========================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.environ.get(
            "PORT",
            "8000"
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
