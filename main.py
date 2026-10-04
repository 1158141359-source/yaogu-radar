from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import time
import math
import re

app = FastAPI(title="妖股雷达")

TZ = ZoneInfo("Asia/Shanghai")
TODAY = datetime.now(TZ).date()

TIMEOUT = 8

EASTMONEY = "https://push2.eastmoney.com"
EASTMONEY_EX = "https://push2ex.eastmoney.com"
DATACENTER = "https://datacenter-web.eastmoney.com"
MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://quote.eastmoney.com/"
})

CACHE = {}
CACHE_SECONDS = 60


# =========================
# 基础工具
# =========================

def safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except:
        return default


def clean_code(code):
    if code is None:
        return ""
    s = str(code)
    m = re.search(r"(\d{6})", s)
    return m.group(1) if m else ""


def secid(code):
    code = clean_code(code)
    if code.startswith(("6", "68", "11", "51", "56", "58")):
        return f"1.{code}"
    return f"0.{code}"


def is_st(name):
    n = str(name or "").upper()
    return "ST" in n or "*ST" in n


def normalize_date(value):
    if not value:
        return TODAY

    if hasattr(value, "date"):
        return value.date()

    s = str(value).strip()

    for fmt in [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%Y%m%d"
    ]:
        try:
            return datetime.strptime(s[:10], fmt).date()
        except:
            pass

    return TODAY


def get_json(url, params=None, timeout=TIMEOUT):
    try:
        r = session.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


# =========================
# 交易日
# =========================

def get_trading_dates(end_date, count=10):
    params = {
        "secid": "1.000001",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60",
        "klt": 101,
        "fqt": 1,
        "beg": "0",
        "end": "20500101",
    }

    data = get_json(
        f"{EASTMONEY}/api/qt/stock/kline/get",
        params
    )

    rows = (
        data.get("data", {}).get("klines", [])
        if isinstance(data, dict)
        else []
    )

    dates = []

    for row in rows:
        try:
            d = str(row).split(",")[0]
            dates.append(datetime.strptime(d, "%Y-%m-%d").date())
        except:
            continue

    dates = sorted(set(d for d in dates if d <= end_date), reverse=True)

    return dates[:count]


def get_actual_target_date(requested_date):
    dates = get_trading_dates(requested_date, 3)

    if dates:
        return dates[0]

    return requested_date


# =========================
# K线
# =========================

def get_kline(code, end_date, limit=120):
    params = {
        "secid": secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60",
        "klt": 101,
        "fqt": 1,
        "beg": "0",
        "end": "20500101",
    }

    data = get_json(
        f"{EASTMONEY}/api/qt/stock/kline/get",
        params
    )

    rows = (
        data.get("data", {}).get("klines", [])
        if isinstance(data, dict)
        else []
    )

    result = []

    for row in rows[-limit:]:
        p = str(row).split(",")

        if len(p) < 7:
            continue

        try:
            result.append({
                "date": p[0],
                "open": safe_float(p[1]),
                "close": safe_float(p[2]),
                "high": safe_float(p[3]),
                "low": safe_float(p[4]),
                "volume": safe_float(p[5]),
                "amount": safe_float(p[6]),
            })
        except:
            pass

    target = str(end_date)

    return [
        x for x in result
        if x["date"] <= target
    ]


# =========================
# A股全市场
# =========================

def get_all_a_stocks():
    params = {
        "pn": 1,
        "pz": 5000,
        "po": 1,
        "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f2,f3,f8,f12,f14,f20,f21",
    }

    data = get_json(
        f"{EASTMONEY}/api/qt/clist/get",
        params
    )

    rows = (
        data.get("data", {}).get("diff", [])
        if isinstance(data, dict)
        else []
    )

    result = []

    for r in rows:
        code = clean_code(r.get("f12"))
        name = r.get("f14", "")

        if not code or is_st(name):
            continue

        result.append({
            "code": code,
            "name": name,
            "price": safe_float(r.get("f2")),
            "pct": safe_float(r.get("f3")),
            "turnover": safe_float(r.get("f8")),
            "market_cap": safe_float(r.get("f20")) / 1e8,
            "float_cap": safe_float(r.get("f21")) / 1e8,
        })

    return result


# =========================
# 行情
# =========================

def get_quotes(codes):
    codes = list(dict.fromkeys(clean_code(x) for x in codes if clean_code(x)))

    if not codes:
        return {}

    result = {}

    # Eastmoney一次最多一批
    for i in range(0, len(codes), 500):
        batch = codes[i:i + 500]

        secids = ",".join(secid(x) for x in batch)

        params = {
            "secids": secids,
            "fields": "f2,f3,f8,f12,f14,f20,f21",
        }

        data = get_json(
            f"{EASTMONEY}/api/qt/ulist.np/get",
            params
        )

        rows = (
            data.get("data", {}).get("diff", [])
            if isinstance(data, dict)
            else []
        )

        for r in rows:
            code = clean_code(r.get("f12"))

            if not code:
                continue

            result[code] = {
                "code": code,
                "name": r.get("f14", ""),
                "price": safe_float(r.get("f2")),
                "pct": safe_float(r.get("f3")),
                "turnover": safe_float(r.get("f8")),
                "market_cap": safe_float(r.get("f20")) / 1e8,
                "float_cap": safe_float(r.get("f21")) / 1e8,
            }

    return result


# =========================
# 涨停池
# =========================

def get_zt_pool(date):
    params = {
        "date": date.strftime("%Y%m%d"),
        "pageindex": 0,
        "pagesize": 200,
        "sort": "fbt:asc",
    }

    data = get_json(
        f"{EASTMONEY_EX}/getTopicZTPool",
        params
    )

    rows = (
        data.get("data", {}).get("pool", [])
        if isinstance(data, dict)
        else []
    )

    result = []

    for r in rows:
        code = clean_code(
            r.get("c")
            or r.get("code")
            or r.get("SECURITY_CODE")
        )

        name = (
            r.get("n")
            or r.get("name")
            or r.get("SECURITY_NAME_ABBR")
            or ""
        )

        if code:
            result.append({
                "code": code,
                "name": name
            })

    return result


def get_zt_codes(date):
    return {
        x["code"]
        for x in get_zt_pool(date)
        if x.get("code")
    }


def get_limit_up_counts(code, dates):
    count = 0

    for d in dates:
        pool = get_zt_codes(d)

        if code in pool:
            count += 1

    return count


# =========================
# 龙虎榜
# =========================

def get_lhb(begin_date, end_date):
    begin = begin_date.strftime("%Y-%m-%d")
    end = end_date.strftime("%Y-%m-%d")

    filter_raw = (
        f"(TRADE_DATE>='{begin}')"
        f"(TRADE_DATE<='{end}')"
    )

    params = {
        "sortColumns": "SECURITY_CODE,TRADE_DATE",
        "sortTypes": "-1,-1",
        "pageNumber": 1,
        "pageSize": 5000,
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,NET_BUY_AMT",
        "source": "WEB",
        "client": "WEB",
        "filter": quote(
            filter_raw,
            safe="()'="
        )
    }

    data = get_json(
        f"{DATACENTER}/api/data/v1/get",
        params,
        timeout=TIMEOUT
    )

    rows = (
        data.get("result", {}).get("data", [])
        if isinstance(data, dict)
        else []
    )

    result = {}

    for r in rows:
        code = clean_code(
            r.get("SECURITY_CODE")
            or r.get("code")
        )

        if not code:
            continue

        net_buy = safe_float(
            r.get("NET_BUY_AMT")
            or r.get("NET_BUY")
            or 0
        )

        result[code] = {
            "code": code,
            "name": r.get("SECURITY_NAME_ABBR", ""),
            "net_buy": net_buy,
            "date": str(r.get("TRADE_DATE", ""))[:10]
        }

    return result


# =========================
# 当前盘口
# =========================

def get_order_book(code):
    params = {
        "secid": secid(code),
        "fields": (
            "f43,f44,f45,f46,f47,f48,"
            "f49,f50,f51,f52,f53,f54"
        )
    }

    data = get_json(
        f"{EASTMONEY}/api/qt/stock/get",
        params,
        timeout=5
    )

    d = data.get("data", {}) if isinstance(data, dict) else {}

    if not d:
        return {
            "buy": 0,
            "sell": 0,
            "ratio": 0
        }

    buy = (
        safe_float(d.get("f49")) +
        safe_float(d.get("f50")) +
        safe_float(d.get("f51")) +
        safe_float(d.get("f52")) +
        safe_float(d.get("f53"))
    )

    sell = (
        safe_float(d.get("f43")) +
        safe_float(d.get("f44")) +
        safe_float(d.get("f45")) +
        safe_float(d.get("f46")) +
        safe_float(d.get("f47"))
    )

    ratio = sell / buy if buy > 0 else 0

    return {
        "buy": buy,
        "sell": sell,
        "ratio": ratio
    }


# =========================
# 妙想增强
# =========================

def get_mx_candidates():
    try:
        data = get_json(
            MX_URL,
            timeout=6
        )

        codes = set()

        def walk(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.lower() in (
                        "code",
                        "stock_code",
                        "security_code",
                        "symbol"
                    ):
                        c = clean_code(v)

                        if len(c) == 6:
                            codes.add(c)

                    walk(v)

            elif isinstance(obj, list):
                for x in obj:
                    walk(x)

        walk(data)

        return codes

    except:
        return set()


# =========================
# 评分
# =========================

def calculate_score(
    zt_count,
    lhb_info,
    quote,
    order_book=None,
    mx=False
):
    score = 0
    reasons = []

    # --------------------------------
    # 核心指标1：近3日涨停
    # --------------------------------
    if zt_count >= 3:
        score += 25
        reasons.append("近3日3次涨停")
    elif zt_count == 2:
        score += 20
        reasons.append("近3日2次涨停")
    elif zt_count == 1:
        score += 15
        reasons.append("近3日有涨停")

    # --------------------------------
    # 核心指标2：龙虎榜
    # --------------------------------
    if lhb_info:
        score += 15
        reasons.append("近期上过龙虎榜")

        net_buy = safe_float(
            lhb_info.get("net_buy")
        )

        if net_buy > 1e8:
            score += 8
            reasons.append("龙虎榜净买入较强")
        elif net_buy > 5e7:
            score += 5
            reasons.append("龙虎榜净买入")

    # --------------------------------
    # 核心指标3：市值
    # --------------------------------
    market_cap = safe_float(
        quote.get("market_cap")
    )

    if market_cap <= 50:
        score += 15
        reasons.append("市值≤50亿")
    elif market_cap <= 100:
        score += 12
        reasons.append("市值≤100亿")
    elif market_cap <= 200:
        score += 8
        reasons.append("市值≤200亿")
    elif market_cap <= 300:
        score += 5
        reasons.append("市值≤300亿")

    # --------------------------------
    # 动量
    # --------------------------------
    pct = safe_float(quote.get("pct"))

    if pct >= 9:
        score += 15
        reasons.append("强势涨幅")
    elif pct >= 7:
        score += 12
        reasons.append("涨幅较强")
    elif pct >= 5:
        score += 8
        reasons.append("涨幅>5%")
    elif pct >= 3:
        score += 4

    # --------------------------------
    # 换手
    # --------------------------------
    turnover = safe_float(
        quote.get("turnover")
    )

    if turnover >= 30:
        score += 15
        reasons.append("高换手")
    elif turnover >= 20:
        score += 12
        reasons.append("换手活跃")
    elif turnover >= 10:
        score += 8
    elif turnover >= 5:
        score += 4

    # --------------------------------
    # 盘口
    # --------------------------------
    if order_book:
        buy = safe_float(order_book.get("buy"))
        sell = safe_float(order_book.get("sell"))

        if sell > buy and sell > 0:
            score += 8
            reasons.append("卖盘强于买盘")

        if buy > sell and buy > 0:
            score += 5
            reasons.append("买盘较强")

    # --------------------------------
    # 妙想增强
    # --------------------------------
    if mx:
        score += 5
        reasons.append("妙想模型增强")

    # 最大基础分约100+
    probability = min(
        99,
        max(
            1,
            round(score * 0.95)
        )
    )

    return {
        "score": score,
        "probability": probability,
        "reasons": reasons
    }


# =========================
# 构建扫描
# =========================

def build_scan(target_date):
    actual_date = get_actual_target_date(target_date)

    trading_dates = get_trading_dates(
        actual_date,
        5
    )

    last3 = trading_dates[:3]

    if not last3:
        last3 = [actual_date]

    # --------------------------------
    # 近3日涨停
    # --------------------------------
    zt_by_date = {}

    for d in last3:
        zt_by_date[d] = get_zt_codes(d)

    zt_codes = set()

    for codes in zt_by_date.values():
        zt_codes |= codes

    # --------------------------------
    # 龙虎榜
    # --------------------------------
    begin_date = (
        actual_date - timedelta(days=14)
    )

    lhb_map = get_lhb(
        begin_date,
        actual_date
    )

    lhb_codes = set(lhb_map.keys())

    # --------------------------------
    # 全市场
    # --------------------------------
    stocks = get_all_a_stocks()

    stock_map = {
        x["code"]: x
        for x in stocks
    }

    # --------------------------------
    # 活跃股票池
    # --------------------------------
    active = sorted(
        stocks,
        key=lambda x: (
            safe_float(x.get("pct")),
            safe_float(x.get("turnover"))
        ),
        reverse=True
    )

    # 不再要求三个条件同时满足
    # 只要属于核心池或者市场活跃池即可进入评分
    special_codes = (
        zt_codes |
        lhb_codes
    )

    candidate_codes = (
        set(x["code"] for x in active[:350])
        |
        special_codes
    )

    # 防止候选过多
    candidate_codes = list(candidate_codes)[:500]

    quotes = {}

    # --------------------------------
    # 如果是今天，直接使用实时行情
    # 如果是历史日期，使用K线收盘数据估算
    # --------------------------------
    if actual_date == TODAY:
        quotes = {
            c: stock_map[c]
            for c in candidate_codes
            if c in stock_map
        }

    else:
        def load_history(code):
            kl = get_kline(
                code,
                actual_date,
                60
            )

            if not kl:
                return code, None

            row = kl[-1]

            prev = kl[-2] if len(kl) >= 2 else None

            pct = 0

            if prev and safe_float(prev["close"]) > 0:
                pct = (
                    row["close"] /
                    prev["close"] -
                    1
                ) * 100

            return code, {
                "code": code,
                "name": stock_map.get(
                    code,
                    {}
                ).get("name", code),
                "price": row["close"],
                "pct": pct,
                "turnover": 0,
                "market_cap": (
                    safe_float(
                        stock_map.get(
                            code,
                            {}
                        ).get("market_cap")
                    )
                    *
                    row["close"]
                    /
                    max(
                        safe_float(
                            stock_map.get(
                                code,
                                {}
                            ).get("price")
                        ),
                        0.01
                    )
                ),
                "float_cap": 0,
                "historical": True
            }

        with ThreadPoolExecutor(max_workers=12) as ex:
            futures = [
                ex.submit(
                    load_history,
                    c
                )
                for c in candidate_codes[:160]
            ]

            for f in as_completed(futures):
                try:
                    code, q = f.result()

                    if q:
                        quotes[code] = q
                except:
                    pass

    # --------------------------------
    # 妙想只在今天调用
    # --------------------------------
    mx_codes = set()

    if actual_date == TODAY:
        mx_codes = get_mx_candidates()

    # --------------------------------
    # 第一轮评分
    # --------------------------------
    results = []

    for code, quote in quotes.items():

        zt_count = sum(
            1
            for d in last3
            if code in zt_by_date.get(d, set())
        )

        lhb_info = lhb_map.get(code)

        scoring = calculate_score(
            zt_count=zt_count,
            lhb_info=lhb_info,
            quote=quote,
            order_book=None,
            mx=(code in mx_codes)
        )

        results.append({
            "code": code,
            "name": quote.get("name", code),
            "price": round(
                safe_float(
                    quote.get("price")
                ),
                2
            ),
            "pct": round(
                safe_float(
                    quote.get("pct")
                ),
                2
            ),
            "turnover": round(
                safe_float(
                    quote.get("turnover")
                ),
                2
            ),
            "market_cap": round(
                safe_float(
                    quote.get("market_cap")
                ),
                2
            ),
            "zt_count": zt_count,
            "lhb": bool(lhb_info),
            "lhb_net_buy": round(
                safe_float(
                    lhb_info.get("net_buy")
                    if lhb_info
                    else 0
                ) / 1e8,
                2
            ),
            "score": scoring["score"],
            "probability": scoring["probability"],
            "reasons": scoring["reasons"],
            "order_book": None,
        })

    # --------------------------------
    # 初步排序
    # --------------------------------
    results.sort(
        key=lambda x: (
            x["score"],
            x["zt_count"],
            x["pct"],
            x["turnover"]
        ),
        reverse=True
    )

    # --------------------------------
    # 当前日才查盘口
    # 只查前15名，避免 Render 超时
    # --------------------------------
    if actual_date == TODAY:

        top_codes = [
            x["code"]
            for x in results[:15]
        ]

        def load_book(code):
            return code, get_order_book(code)

        with ThreadPoolExecutor(max_workers=8) as ex:

            futures = [
                ex.submit(
                    load_book,
                    c
                )
                for c in top_codes
            ]

            for f in as_completed(futures):
                try:
                    code, book = f.result()

                    for item in results:
                        if item["code"] == code:
                            item["order_book"] = book

                            scoring = calculate_score(
                                zt_count=item["zt_count"],
                                lhb_info=(
                                    lhb_map.get(code)
                                ),
                                quote=quotes.get(code, {}),
                                order_book=book,
                                mx=(code in mx_codes)
                            )

                            item["score"] = scoring["score"]
                            item["probability"] = scoring[
                                "probability"
                            ]
                            item["reasons"] = scoring[
                                "reasons"
                            ]

                            break

                except:
                    pass

    # --------------------------------
    # 最终排序
    # --------------------------------
    results.sort(
        key=lambda x: (
            x["score"],
            x["zt_count"],
            x["pct"],
            x["turnover"]
        ),
        reverse=True
    )

    top3 = results[:3]

    return {
        "success": True,
        "date": str(actual_date),
        "requested_date": str(target_date),
        "is_today": actual_date == TODAY,

        "core_rules": [
            "近3个交易日出现涨停：加分项",
            "上过龙虎榜：加分项",
            "总市值≤300亿：加分项"
        ],

        "message": (
            "三个核心指标现在全部采用加权评分，"
            "不要求同时满足；按综合妖股概率排名。"
        ),

        "top3": top3,
        "ranking": results[:50],

        "stats": {
            "candidate_count": len(candidate_codes),
            "result_count": len(results),
            "zt_count": len(zt_codes),
            "lhb_count": len(lhb_codes),
            "mx_count": len(mx_codes)
        }
    }


# =========================
# API
# =========================

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "time": datetime.now(TZ).isoformat()
    }


@app.get("/api/scanner")
def scanner(
    date: str = Query(
        default=str(TODAY)
    )
):
    try:
        target_date = normalize_date(date)

        cache_key = str(target_date)

        now = time.time()

        if cache_key in CACHE:
            cached_time, cached_data = CACHE[
                cache_key
            ]

            if now - cached_time < CACHE_SECONDS:
                return cached_data

        data = build_scan(target_date)

        CACHE[cache_key] = (
            now,
            data
        )

        return data

    except Exception as e:

        return {
            "success": False,
            "error": str(e),
            "top3": [],
            "ranking": []
        }


# =========================
# 前端
# =========================

HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport"
content="width=device-width,initial-scale=1,
maximum-scale=1,user-scalable=no">

<title>🔥 妖股雷达</title>

<style>

*{
    box-sizing:border-box;
}

body{
    margin:0;
    background:#050505;
    color:#eee;
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "PingFang SC",
        "Microsoft YaHei",
        sans-serif;
}

.container{
    width:100%;
    max-width:900px;
    margin:auto;
    padding:15px;
}

.header{
    display:flex;
    justify-content:space-between;
    align-items:center;
    margin-bottom:15px;
}

.title{
    font-size:25px;
    font-weight:800;
}

.status{
    font-size:12px;
    color:#888;
}

.panel{
    background:#101010;
    border:1px solid #252525;
    border-radius:14px;
    padding:14px;
    margin-bottom:14px;
}

.controls{
    display:flex;
    gap:8px;
}

input{
    flex:1;
    background:#050505;
    color:#fff;
    border:1px solid #333;
    border-radius:8px;
    padding:10px;
}

button{
    border:0;
    border-radius:8px;
    padding:10px 15px;
    background:#e33;
    color:white;
    font-weight:bold;
}

button.secondary{
    background:#333;
}

.rule{
    margin-top:8px;
    padding:9px;
    background:#171717;
    border-radius:8px;
    font-size:13px;
}

.green{
    color:#00d084;
}

.red{
    color:#ff4d4d;
}

.top3{
    display:grid;
    grid-template-columns:
        repeat(3,1fr);
    gap:10px;
}

.card{
    background:#151515;
    border:1px solid #2b2b2b;
    border-radius:12px;
    padding:13px;
}

.card-title{
    font-size:18px;
    font-weight:bold;
}

.prob{
    font-size:25px;
    font-weight:900;
    color:#ff4747;
}

.score{
    font-size:13px;
    color:#aaa;
}

.reason{
    font-size:12px;
    color:#bbb;
    line-height:1.7;
    margin-top:5px;
}

.table-wrap{
    overflow-x:auto;
}

table{
    width:100%;
    border-collapse:collapse;
    min-width:700px;
}

th,td{
    padding:10px 7px;
    border-bottom:1px solid #222;
    text-align:center;
    font-size:13px;
}

th{
    color:#999;
}

.error{
    background:#281010;
    border:1px solid #542020;
    color:#ff7777;
    padding:12px;
    border-radius:10px;
}

.notice{
    color:#999;
    font-size:12px;
    line-height:1.7;
}

@media(max-width:600px){

    .top3{
        grid-template-columns:1fr;
    }

    .title{
        font-size:22px;
    }

    .controls{
        flex-wrap:wrap;
    }

    input{
        min-width:150px;
    }
}

</style>
</head>

<body>

<div class="container">

<div class="header">

<div class="title">
🔥 妖股雷达
</div>

<div id="status"
class="status">
准备就绪
</div>

</div>

<div class="panel">

<div class="controls">

<input
type="date"
id="date"
>

<button onclick="scan()">
开始扫描
</button>

<button
class="secondary"
onclick="today()">
今天
</button>

</div>

<div class="rule">
① 近3个交易日出现涨停
</div>

<div class="rule">
② 上过龙虎榜
</div>

<div class="rule">
③ 总市值 ≤ 300亿
</div>

<div class="notice">
以上3项现在都是<strong>评分项</strong>，
不要求全部满足。系统按照涨停、
龙虎榜、市值、涨幅、换手及盘口等
综合计算妖股概率。
</div>

</div>

<div id="error"></div>

<div class="panel">

<h3>
🔥 TOP3 强势标的
</h3>

<div
id="top3"
class="top3">
</div>

</div>

<div class="panel">

<h3>
📊 妖股概率排行榜
</h3>

<div class="table-wrap">

<table>

<thead>

<tr>

<th>排名</th>
<th>股票</th>
<th>概率</th>
<th>评分</th>
<th>涨幅</th>
<th>换手</th>
<th>市值</th>
<th>3日涨停</th>
<th>龙虎榜</th>

</tr>

</thead>

<tbody id="ranking">
</tbody>

</table>

</div>

</div>

<div class="notice">
数据源：东方财富公开行情接口 + 妙想增强。
历史日期的盘口/竞价无法完整还原，
历史市值属于估算值。
</div>

</div>

<script>

const dateInput =
document.getElementById("date");

function today(){

    const d =
    new Date();

    const y =
    d.getFullYear();

    const m =
    String(
        d.getMonth()+1
    ).padStart(2,"0");

    const day =
    String(
        d.getDate()
    ).padStart(2,"0");

    dateInput.value =
    `${y}-${m}-${day}`;

    scan();
}


async function scan(){

    const date =
    dateInput.value;

    const status =
    document.getElementById(
        "status"
    );

    const error =
    document.getElementById(
        "error"
    );

    status.innerText =
    "正在扫描...";

    error.innerHTML = "";

    try{

        const controller =
        new AbortController();

        const timer =
        setTimeout(
            () => controller.abort(),
            30000
        );

        const response =
        await fetch(
            `/api/scanner?date=${date}`,
            {
                signal:
                controller.signal
            }
        );

        clearTimeout(timer);

        if(!response.ok){
            throw new Error(
                "HTTP " +
                response.status
            );
        }

        const data =
        await response.json();

        if(!data.success){
            throw new Error(
                data.error ||
                "数据读取失败"
            );
        }

        render(data);

        status.innerText =
        "数据读取成功";

    }catch(e){

        status.innerText =
        "数据读取失败";

        error.innerHTML =
        `<div class="error">
        加载失败：${e.message}
        </div>`;
    }
}


function render(data){

    const top =
    document.getElementById(
        "top3"
    );

    const ranking =
    document.getElementById(
        "ranking"
    );

    top.innerHTML = "";

    ranking.innerHTML = "";

    if(!data.top3 ||
       data.top3.length === 0){

        top.innerHTML =
        `<div class="notice">
        当前没有有效数据
        </div>`;

    }else{

        data.top3.forEach(
            (x,i)=>{

                top.innerHTML += `

                <div class="card">

                <div class="card-title">
                ${i+1}. ${x.name}
                </div>

                <div>
                ${x.code}
                </div>

                <div class="prob">
                ${x.probability}%
                </div>

                <div class="score">
                综合评分：
                ${x.score}
                </div>

                <div class="reason">
                ${x.reasons.join(" · ")}
                </div>

                </div>

                `;
            }
        );
    }

    (data.ranking || [])
    .forEach((x,i)=>{

        ranking.innerHTML += `

        <tr>

        <td>
        ${i+1}
        </td>

        <td>
        <strong>
        ${x.name}
        </strong>
        <br>
        ${x.code}
        </td>

        <td class="red">
        ${x.probability}%
        </td>

        <td>
        ${x.score}
        </td>

        <td class="${
            x.pct >= 0
            ? "red"
            : "green"
        }">
        ${x.pct}%
        </td>

        <td>
        ${x.turnover}%
        </td>

        <td>
        ${x.market_cap}亿
        </td>

        <td>
        ${x.zt_count}
        </td>

        <td>
        ${
            x.lhb
            ? "✓"
            : "-"
        }
        </td>

        </tr>

        `;
    });
}


(function init(){

    const d =
    new Date();

    const y =
    d.getFullYear();

    const m =
    String(
        d.getMonth()+1
    ).padStart(2,"0");

    const day =
    String(
        d.getDate()
    ).padStart(2,"0");

    dateInput.value =
    `${y}-${m}-${day}`;

    scan();

})();

</script>

</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTML
