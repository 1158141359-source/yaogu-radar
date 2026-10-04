import os

import time

import math

import threading

from datetime import datetime, timedelta

from concurrent.futures import ThreadPoolExecutor, as_completed

from urllib.parse import urlencode, quote

import requests

from fastapi import FastAPI

from fastapi.responses import HTMLResponse, JSONResponse

from zoneinfo import ZoneInfo

# =========================================================

# 妖股雷达 - 最终版

# 核心硬条件：

# ① 近3个交易日出现涨停

# ② 上过龙虎榜

# ③ 总市值 <= 300亿

# =========================================================

app = FastAPI(title="妖股雷达")

TZ = ZoneInfo("Asia/Shanghai")

TODAY = datetime.now(TZ).strftime("%Y-%m-%d")

TIMEOUT = 8

BASE = "https://push2.eastmoney.com"

BASE_EX = "https://push2ex.eastmoney.com"

DATA = "https://datacenter-web.eastmoney.com"

MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"

session = requests.Session()

session.headers.update({

    "User-Agent": (

        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "

        "AppleWebKit/605.1.15 (KHTML, like Gecko) "

        "Version/17.0 Mobile/15E148 Safari/604.1"

    ),

    "Referer": "https://quote.eastmoney.com/"

})

CACHE = {}

CACHE_LOCK = threading.Lock()

# =========================================================

# 基础工具

# =========================================================

def now_text():

    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def safe_float(v, default=0.0):

    try:

        if v is None or v == "":

            return default

        return float(v)

    except Exception:

        return default

def clean_code(code):

    if not code:

        return ""

    s = str(code).strip()

    s = s.replace("SH", "").replace("SZ", "").replace("BJ", "")

    s = s.replace(".", "")

    return s.zfill(6)

def secid(code):

    code = clean_code(code)

    if code.startswith(("6", "68", "69")):

        return f"1.{code}"

    return f"0.{code}"

def is_st(name):

    name = str(name or "").upper()

    return "ST" in name or "*ST" in name

def normalize_date(value):

    if not value:

        return TODAY

    s = str(value)

    if "T" in s:

        s = s.split("T")[0]

    if " " in s:

        s = s.split(" ")[0]

    s = s.replace("/", "-")

    try:

        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")

    except Exception:

        return TODAY

def get_json(url, params=None, timeout=TIMEOUT):

    """

    东方财富请求：

    - 单次请求

    - 短超时

    - 失败返回 {}

    """

    try:

        r = session.get(

            url,

            params=params,

            timeout=timeout

        )

        if r.status_code != 200:

            return {}

        return r.json()

    except Exception:

        return {}

# =========================================================

# 交易日

# =========================================================

def get_trading_dates(end_date, count=10):

    """

    使用上证指数日线获得交易日。

    """

    try:

        dt = datetime.strptime(end_date, "%Y-%m-%d")

    except Exception:

        dt = datetime.now(TZ)

    begin = (dt - timedelta(days=30)).strftime("%Y-%m-%d")

    url = f"{BASE}/api/qt/stock/kline/get"

    params = {

        "secid": "1.000001",

        "klt": "101",

        "fqt": "0",

        "beg": begin.replace("-", ""),

        "end": dt.strftime("%Y%m%d"),

        "fields1": "f1,f2,f3,f4,f5,f6",

        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

    }

    data = get_json(url, params)

    klines = (

        data.get("data", {}).get("klines", [])

        if isinstance(data, dict)

        else []

    )

    dates = []

    for row in klines:

        try:

            d = str(row).split(",")[0]

            d = normalize_date(d)

            if d not in dates:

                dates.append(d)

        except Exception:

            pass

    dates.sort()

    if end_date in dates:

        idx = dates.index(end_date)

        return dates[max(0, idx - count + 1):idx + 1]

    return dates[-count:]

def get_actual_target_date(target_date):

    target_date = normalize_date(target_date)

    dates = get_trading_dates(target_date, 10)

    if target_date in dates:

        return target_date

    if dates:

        return dates[-1]

    return target_date

# =========================================================

# K线

# =========================================================

def get_kline(code, limit=120, end_date=None):

    code = clean_code(code)

    if not code:

        return []

    if not end_date:

        end_date = TODAY

    try:

        dt = datetime.strptime(end_date, "%Y-%m-%d")

    except Exception:

        dt = datetime.now(TZ)

    begin = (dt - timedelta(days=260)).strftime("%Y%m%d")

    url = f"{BASE}/api/qt/stock/kline/get"

    params = {

        "secid": secid(code),

        "klt": "101",

        "fqt": "1",

        "beg": begin,

        "end": dt.strftime("%Y%m%d"),

        "lmt": str(limit),

        "fields1": "f1,f2,f3,f4,f5,f6",

        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

    }

    data = get_json(url, params)

    rows = (

        data.get("data", {}).get("klines", [])

        if isinstance(data, dict)

        else []

    )

    result = []

    for row in rows:

        try:

            parts = str(row).split(",")

            if len(parts) < 7:

                continue

            result.append({

                "date": normalize_date(parts[0]),

                "open": safe_float(parts[1]),

                "close": safe_float(parts[2]),

                "high": safe_float(parts[3]),

                "low": safe_float(parts[4]),

                "volume": safe_float(parts[5]),

                "amount": safe_float(parts[6]),

            })

        except Exception:

            continue

    return result

# =========================================================

# 股票行情

# =========================================================

def get_quotes(codes):

    codes = [clean_code(x) for x in codes if clean_code(x)]

    if not codes:

        return {}

    url = f"{BASE}/api/qt/ulist.np/get"

    params = {

        "fltt": "2",

        "invt": "2",

        "np": "1",

        "fs": "b:BK0501",

        "fields": "f2,f3,f8,f12,f14,f20,f21",

        "secids": ",".join(secid(x) for x in codes)

    }

    # 东方财富 ulist 的 fs 在部分环境下限制较多，

    # 这里改用板块/市场查询方式逐批获取。

    result = {}

    for i in range(0, len(codes), 100):

        batch = codes[i:i + 100]

        params = {

            "fltt": "2",

            "invt": "2",

            "np": "1",

            "fs": ",".join(

                f"m:{1 if x.startswith(('6', '68', '69')) else 0}+f:!2"

                for x in batch

            ),

            "fields": "f2,f3,f8,f12,f14,f20,f21"

        }

        data = get_json(url, params)

        diff = (

            data.get("data", {}).get("diff", [])

            if isinstance(data, dict)

            else []

        )

        if isinstance(diff, dict):

            diff = list(diff.values())

        for x in diff:

            code = clean_code(x.get("f12"))

            if code in batch:

                result[code] = {

                    "price": safe_float(x.get("f2")),

                    "pct": safe_float(x.get("f3")),

                    "turnover": safe_float(x.get("f8")),

                    "name": x.get("f14") or code,

                    "market_cap": safe_float(x.get("f20")),

                    "float_cap": safe_float(x.get("f21"))

                }

    return result

# =========================================================

# 涨停池

# =========================================================

def get_zt_pool(date):

    date = normalize_date(date)

    url = f"{BASE_EX}/getTopicZTPool"

    params = {

        "ut": "7eea3edcaed734bea9cbfcf3c6a2c7f1",

        "dpt": "wz.ztzt",

        "Pageindex": "0",

        "pagesize": "200",

        "sort": "fbt:asc",

        "date": date.replace("-", ""),

        "type": "zt",

        "zttj": "st",

        "iszt": "1"

    }

    data = get_json(url, params)

    pool = (

        data.get("data", {}).get("pool", [])

        if isinstance(data, dict)

        else []

    )

    if not isinstance(pool, list):

        return []

    return pool

def get_zt_codes(date):

    pool = get_zt_pool(date)

    result = set()

    for x in pool:

        code = clean_code(

            x.get("c")

            or x.get("code")

            or x.get("SECURITY_CODE")

        )

        if code:

            result.add(code)

    return result

def get_limit_up_counts(dates):

    counts = {}

    for d in dates:

        pool = get_zt_pool(d)

        for x in pool:

            code = clean_code(

                x.get("c")

                or x.get("code")

                or x.get("SECURITY_CODE")

            )

            if code:

                counts[code] = counts.get(code, 0) + 1

    return counts

# =========================================================

# 龙虎榜

# =========================================================

def get_lhb(codes=None, dates=None):

    """

    龙虎榜数据。

    重点：

    东方财富 DataCenter 的 filter 如果直接传 > <，

    某些环境会返回空数据，因此这里手动 URL 编码。

    """

    if dates is None:

        dates = [TODAY]

    dates = [normalize_date(x) for x in dates]

    begin = min(dates)

    end = max(dates)

    url = f"{DATA}/api/data/v1/get"

    filter_raw = (

        f"(TRADE_DATE>='{begin}')"

        f"(TRADE_DATE<='{end}')"

    )

    params = {

        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",

        "columns": "ALL",

        "pageNumber": "1",

        "pageSize": "500",

        "sortColumns": "TRADE_DATE",

        "sortTypes": "-1",

        "source": "WEB",

        "client": "WEB"

    }

    try:

        query = urlencode(params)

        final_url = (

            url

            + "?"

            + query

            + "&filter="

            + quote(filter_raw, safe="()'=")

        )

        data = get_json(final_url)

    except Exception:

        return {}

    rows = (

        data.get("result", {}).get("data", [])

        if isinstance(data, dict)

        else []

    )

    result = {}

    for row in rows:

        code = clean_code(

            row.get("SECURITY_CODE")

            or row.get("SECUCODE")

            or row.get("CODE")

        )

        if not code:

            continue

        if codes and code not in codes:

            continue

        trade_date = normalize_date(

            row.get("TRADE_DATE")

            or row.get("TRADE_DATE_NAME")

        )

        if trade_date not in dates:

            continue

        net = safe_float(

            row.get("NET_BUY_AMT")

            or row.get("NET_BUY")

            or row.get("NET_BUY_AMOUNT")

        )

        if code not in result:

            result[code] = {

                "dates": set(),

                "net": 0

            }

        result[code]["dates"].add(trade_date)

        result[code]["net"] += net

    return result

# =========================================================

# 当前五档盘口

# =========================================================

def get_order_book(code):

    code = clean_code(code)

    url = f"{BASE}/api/qt/stock/get"

    params = {

        "secid": secid(code),

        "fields": (

            "f43,f44,f45,f46,f47,f48,"

            "f50,f51,f52,f53,f54,f55,f56,f57,f58,"

            "f59,f60,f61,f62,f63,f64,f65,f66,f67,f68"

        )

    }

    data = get_json(url, params)

    d = data.get("data", {}) if isinstance(data, dict) else {}

    if not d:

        return {

            "buy": 0,

            "sell": 0,

            "ratio": 0

        }

    buy = 0

    sell = 0

    # 买一至买五

    for key in ("f58", "f60", "f62", "f64", "f66"):

        buy += safe_float(d.get(key))

    # 卖一至卖五

    for key in ("f57", "f59", "f61", "f63", "f65"):

        sell += safe_float(d.get(key))

    total = buy + sell

    ratio = sell / total if total > 0 else 0

    return {

        "buy": buy,

        "sell": sell,

        "ratio": ratio

    }

# =========================================================

# 妙想增强

# =========================================================

def get_mx_candidates():

    api_key = os.getenv("MX_APIKEY", "").strip()

    if not api_key:

        return {}

    headers = {

        "Authorization": f"Bearer {api_key}",

        "Content-Type": "application/json"

    }

    payload = {

        "query": "今日A股强势股票，关注涨停、龙虎榜、短线强势股票"

    }

    try:

        r = session.post(

            MX_URL,

            headers=headers,

            json=payload,

            timeout=6

        )

        if r.status_code != 200:

            return {}

        data = r.json()

    except Exception:

        return {}

    result = {}

    def walk(obj):

        if isinstance(obj, dict):

            for k, v in obj.items():

                if k.lower() in (

                    "code",

                    "stock_code",

                    "security_code",

                    "symbol"

                ):

                    code = clean_code(v)

                    if len(code) == 6:

                        result[code] = True

                walk(v)

        elif isinstance(obj, list):

            for x in obj:

                walk(x)

        elif isinstance(obj, str):

            # 简单识别 6 位股票代码

            import re

            for code in re.findall(r"\b[036]\d{5}\b", obj):

                result[code] = True

    walk(data)

    return result

# =========================================================

# 评分

# =========================================================

def calculate_score(

    code,

    quote,

    zt_count,

    lhb_info,

    order_book=None,

    mx=False

):

    score = 0

    reasons = []

    # -----------------------------------------------------

    # 三项核心硬条件

    # -----------------------------------------------------

    hard_zt = zt_count >= 1

    hard_lhb = bool(lhb_info)

    market_cap = safe_float(

        quote.get("market_cap")

    )

    hard_cap = (

        market_cap > 0

        and market_cap <= 300

    )

    # -----------------------------------------------------

    # 基础评分

    # -----------------------------------------------------

    if hard_zt:

        score += 30

        reasons.append("3日内涨停")

    if hard_lhb:

        score += 25

        reasons.append("龙虎榜")

    if hard_cap:

        score += 20

        reasons.append("市值≤300亿")

    # -----------------------------------------------------

    # 涨幅

    # -----------------------------------------------------

    pct = safe_float(quote.get("pct"))

    if pct >= 9.5:

        score += 15

        reasons.append("接近涨停")

    elif pct >= 7:

        score += 10

        reasons.append("强势上涨")

    elif pct >= 5:

        score += 6

        reasons.append("涨幅>5%")

    # -----------------------------------------------------

    # 换手

    # -----------------------------------------------------

    turnover = safe_float(

        quote.get("turnover")

    )

    if turnover >= 29.25:

        score += 15

        reasons.append("高换手")

    elif turnover >= 15:

        score += 8

        reasons.append("换手较高")

    elif turnover >= 8:

        score += 4

    # -----------------------------------------------------

    # 龙虎榜净买

    # -----------------------------------------------------

    lhb_net = safe_float(

        lhb_info.get("net") if lhb_info else 0

    )

    if lhb_net > 0:

        score += 8

        reasons.append("龙虎榜净买")

    elif lhb_net < 0:

        score -= 3

    # -----------------------------------------------------

    # 盘口

    # -----------------------------------------------------

    order_score = 0

    if order_book:

        buy = safe_float(order_book.get("buy"))

        sell = safe_float(order_book.get("sell"))

        if sell > buy and sell > 0:

            order_score += 5

            reasons.append("委卖>委买")

        elif buy > sell and buy > 0:

            order_score += 3

    score += order_score

    # -----------------------------------------------------

    # 妙想增强

    # -----------------------------------------------------

    if mx:

        score += 5

        reasons.append("妙想强势")

    return {

        "score": round(score, 2),

        "hard": [

            hard_zt,

            hard_lhb,

            hard_cap

        ],

        "hard_ok": all([

            hard_zt,

            hard_lhb,

            hard_cap

        ]),

        "reasons": reasons,

        "zt_count": zt_count,

        "lhb_net": round(lhb_net, 2),

        "market_cap": market_cap,

        "pct": pct,

        "turnover": turnover

    }

# =========================================================

# 单只股票计算

# =========================================================

def analyze_one(

    code,

    quotes,

    zt_counts,

    lhb_map,

    target_date,

    history_map=None

):

    quote = quotes.get(code)

    if not quote:

        return None

    name = quote.get("name") or code

    if is_st(name):

        return None

    zt_count = zt_counts.get(code, 0)

    lhb_info = lhb_map.get(code)

    # 三项硬条件先判断

    market_cap = safe_float(

        quote.get("market_cap")

    )

    if zt_count < 1:

        return None

    if not lhb_info:

        return None

    if market_cap <= 0 or market_cap > 300:

        return None

    # 历史日期：

    # 使用历史K线估算涨跌幅，避免大量实时盘口请求

    if target_date != TODAY and history_map:

        rows = history_map.get(code, [])

        if rows:

            target_row = None

            for r in rows:

                if r["date"] == target_date:

                    target_row = r

                    break

            if target_row:

                closes = [

                    x["close"]

                    for x in rows

                    if x["date"] < target_date

                    and x["close"] > 0

                ]

                if closes:

                    prev_close = closes[-1]

                    if prev_close > 0:

                        quote = dict(quote)

                        quote["pct"] = (

                            target_row["close"] / prev_close - 1

                        ) * 100

    scoring = calculate_score(

        code=code,

        quote=quote,

        zt_count=zt_count,

        lhb_info=lhb_info

    )

    return {

        "code": code,

        "name": name,

        "price": round(

            safe_float(quote.get("price")), 2

        ),

        "pct": round(

            safe_float(quote.get("pct")), 2

        ),

        "turnover": round(

            safe_float(quote.get("turnover")), 2

        ),

        "market_cap": round(market_cap, 2),

        "zt_count": zt_count,

        "lhb": True,

        "lhb_net": scoring["lhb_net"],

        "score": scoring["score"],

        "hard_ok": scoring["hard_ok"],

        "reasons": scoring["reasons"],

        "order_book": None

    }

# =========================================================

# 主扫描

# =========================================================

def build_scan(target_date=None):

    if not target_date:

        target_date = TODAY

    target_date = get_actual_target_date(

        normalize_date(target_date)

    )

    # -----------------------------------------------------

    # 1. 最近交易日

    # -----------------------------------------------------

    trading_dates = get_trading_dates(

        target_date,

        10

    )

    if not trading_dates:

        trading_dates = [target_date]

    last3 = trading_dates[-3:]

    # -----------------------------------------------------

    # 2. 三日涨停池

    # -----------------------------------------------------

    zt_counts = {}

    for d in last3:

        pool = get_zt_pool(d)

        for x in pool:

            code = clean_code(

                x.get("c")

                or x.get("code")

                or x.get("SECURITY_CODE")

            )

            if not code:

                continue

            zt_counts[code] = (

                zt_counts.get(code, 0) + 1

            )

    zt_codes = set(zt_counts.keys())

    # -----------------------------------------------------

    # 3. 龙虎榜

    # -----------------------------------------------------

    lhb_dates = trading_dates[-10:]

    lhb_map = get_lhb(

        codes=None,

        dates=lhb_dates

    )

    lhb_codes = set(lhb_map.keys())

    # -----------------------------------------------------

    # 4. 核心候选池

    #

    # 只取：

    # 3日涨停 ∩ 龙虎榜

    #

    # 最后再判断市值 <=300亿

    # -----------------------------------------------------

    candidate_codes = (

        zt_codes & lhb_codes

    )

    # 防止接口异常导致全市场大量请求

    candidate_codes = list(candidate_codes)[:300]

    if not candidate_codes:

        return {

            "success": True,

            "date": target_date,

            "updated": now_text(),

            "message": "当前日期没有同时满足涨停+龙虎榜的候选股",

            "top3": [],

            "ranking": [],

            "stats": {

                "zt_count": len(zt_codes),

                "lhb_count": len(lhb_codes),

                "candidate_count": 0

            }

        }

    # -----------------------------------------------------

    # 5. 行情

    # -----------------------------------------------------

    quotes = get_quotes(

        candidate_codes

    )

    # -----------------------------------------------------

    # 6. 历史K线

    #

    # 只有回看历史日期才需要。

    # 并发请求。

    # -----------------------------------------------------

    history_map = {}

    if target_date != TODAY:

        def load_history(code):

            return code, get_kline(

                code,

                120,

                target_date

            )

        with ThreadPoolExecutor(

            max_workers=8

        ) as executor:

            futures = [

                executor.submit(

                    load_history,

                    code

                )

                for code in candidate_codes

            ]

            for future in as_completed(futures):

                try:

                    code, rows = future.result()

                    if rows:

                        history_map[code] = rows

                except Exception:

                    pass

    # -----------------------------------------------------

    # 7. 第一轮筛选

    # -----------------------------------------------------

    results = []

    for code in candidate_codes:

        try:

            item = analyze_one(

                code=code,

                quotes=quotes,

                zt_counts=zt_counts,

                lhb_map=lhb_map,

                target_date=target_date,

                history_map=history_map

            )

            if item:

                results.append(item)

        except Exception:

            continue

    # -----------------------------------------------------

    # 8. 按评分排序

    # -----------------------------------------------------

    results.sort(

        key=lambda x: (

            x["score"],

            x["pct"],

            x["zt_count"]

        ),

        reverse=True

    )

    # -----------------------------------------------------

    # 9. 妙想增强

    #

    # 只对已经满足三项硬条件的股票加分。

    # 妙想失败不影响主选股。

    # -----------------------------------------------------

    mx_codes = set()

    if target_date == TODAY:

        mx_data = get_mx_candidates()

        mx_codes = set(mx_data.keys())

    for item in results:

        if item["code"] in mx_codes:

            item["score"] += 5

            item["reasons"].append("妙想强势")

    # -----------------------------------------------------

    # 10. 当前日期才请求盘口

    #

    # 只请求前10名，防止Render超时。

    # -----------------------------------------------------

    if target_date == TODAY and results:

        top_for_order = results[:10]

        def load_order(item):

            return (

                item["code"],

                get_order_book(item["code"])

            )

        with ThreadPoolExecutor(

            max_workers=5

        ) as executor:

            futures = [

                executor.submit(

                    load_order,

                    item

                )

                for item in top_for_order

            ]

            for future in as_completed(futures):

                try:

                    code, ob = future.result()

                    for item in results:

                        if item["code"] == code:

                            item["order_book"] = ob

                            buy = safe_float(

                                ob.get("buy")

                            )

                            sell = safe_float(

                                ob.get("sell")

                            )

                            if sell > buy and sell > 0:

                                item["score"] += 5

                                if "委卖>委买" not in item["reasons"]:

                                    item["reasons"].append(

                                        "委卖>委买"

                                    )

                            break

                except Exception:

                    pass

        results.sort(

            key=lambda x: (

                x["score"],

                x["pct"],

                x["zt_count"]

            ),

            reverse=True

        )

    # -----------------------------------------------------

    # 11. TOP3

    #

    # 只从三项硬条件全部满足的股票中产生。

    # -----------------------------------------------------

    hard_results = [

        x for x in results

        if x.get("hard_ok")

    ]

    hard_results.sort(

        key=lambda x: (

            x["score"],

            x["pct"],

            x["zt_count"]

        ),

        reverse=True

    )

    top3 = hard_results[:3]

    # -----------------------------------------------------

    # 12. 输出排名

    # -----------------------------------------------------

    ranking = hard_results[:30]

    return {

        "success": True,

        "date": target_date,

        "updated": now_text(),

        "message": (

            "已按三项核心硬条件完成筛选"

        ),

        "conditions": [

            "近3个交易日出现涨停",

            "上过龙虎榜",

            "总市值≤300亿"

        ],

        "top3": top3,

        "ranking": ranking,

        "stats": {

            "zt_count": len(zt_codes),

            "lhb_count": len(lhb_codes),

            "candidate_count": len(candidate_codes),

            "hard_count": len(hard_results)

        }

    }

# =========================================================

# API

# =========================================================

@app.get("/api/health")

def health():

    return {

        "success": True,

        "status": "ok",

        "time": now_text()

    }

@app.get("/api/scanner")

def scanner(date: str = None):

    target = normalize_date(

        date or TODAY

    )

    cache_key = target

    # 60秒缓存

    with CACHE_LOCK:

        cached = CACHE.get(cache_key)

        if cached:

            if time.time() - cached["time"] < 60:

                return cached["data"]

    try:

        result = build_scan(target)

        with CACHE_LOCK:

            CACHE[cache_key] = {

                "time": time.time(),

                "data": result

            }

        return result

    except Exception as e:

        return JSONResponse(

            status_code=200,

            content={

                "success": False,

                "error": str(e),

                "date": target,

                "updated": now_text(),

                "top3": [],

                "ranking": []

            }

        )

# =========================================================

# 手机网页

# =========================================================

HTML = r"""

<!DOCTYPE html>

<html lang="zh-CN">

<head>

<meta charset="UTF-8">

<meta name="viewport"

      content="width=device-width,

               initial-scale=1,

               maximum-scale=1,

               user-scalable=no">

<title>🔥 妖股雷达</title>

<style>

*{

    box-sizing:border-box;

}

body{

    margin:0;

    background:#050505;

    color:#eee;

    font-family:-apple-system,

                BlinkMacSystemFont,

                "PingFang SC",

                "Microsoft YaHei",

                sans-serif;

}

.container{

    max-width:900px;

    margin:auto;

    padding:14px;

}

.header{

    display:flex;

    align-items:center;

    justify-content:space-between;

    margin-bottom:14px;

}

.title{

    font-size:26px;

    font-weight:800;

}

.status{

    font-size:12px;

    color:#888;

}

.panel{

    background:#111;

    border:1px solid #292929;

    border-radius:14px;

    padding:14px;

    margin-bottom:12px;

}

.controls{

    display:flex;

    gap:8px;

}

input{

    flex:1;

    background:#181818;

    color:#fff;

    border:1px solid #333;

    border-radius:9px;

    padding:11px;

    font-size:15px;

}

button{

    border:0;

    border-radius:9px;

    padding:10px 15px;

    background:#e60012;

    color:#fff;

    font-weight:700;

    font-size:14px;

}

button.secondary{

    background:#292929;

}

.conditions{

    line-height:1.9;

    font-size:14px;

}

.condition{

    padding:3px 0;

}

.green{

    color:#19d66b;

}

.red{

    color:#ff3b45;

}

.muted{

    color:#888;

    font-size:12px;

}

.section-title{

    font-size:18px;

    font-weight:800;

    margin-bottom:10px;

}

.card{

    background:#171717;

    border:1px solid #303030;

    border-radius:12px;

    padding:13px;

    margin-bottom:9px;

}

.card.top{

    border-color:#e60012;

}

.row{

    display:flex;

    justify-content:space-between;

    gap:10px;

}

.name{

    font-size:17px;

    font-weight:800;

}

.code{

    color:#888;

    font-size:12px;

    margin-left:6px;

}

.score{

    color:#ffcc00;

    font-weight:900;

    font-size:20px;

}

.data{

    display:flex;

    flex-wrap:wrap;

    gap:8px;

    margin-top:9px;

}

.tag{

    background:#222;

    border-radius:6px;

    padding:4px 7px;

    font-size:12px;

}

.reason{

    margin-top:9px;

    color:#aaa;

    font-size:12px;

    line-height:1.6;

}

.error{

    color:#ff4650;

    background:#22090b;

    border:1px solid #5b1419;

}

.loading{

    text-align:center;

    color:#888;

    padding:20px;

}

.empty{

    text-align:center;

    color:#777;

    padding:25px 5px;

}

.footer{

    text-align:center;

    color:#555;

    font-size:11px;

    padding:20px 0;

}

</style>

</head>

<body>

<div class="container">

<div class="header">

    <div class="title">🔥 妖股雷达</div>

    <div id="status" class="status">准备就绪</div>

</div>

<div class="panel">

    <div class="controls">

        <input

            id="date"

            type="date"

        >

        <button

            onclick="scan()">

            开始扫描

        </button>

        <button

            class="secondary"

            onclick="today()">

            今天

        </button>

    </div>

</div>

<div class="panel">

    <div class="section-title">

        核心硬条件

    </div>

    <div class="conditions">

        <div class="condition">

            ① <span class="red">

            近3个交易日出现涨停

            </span>

        </div>

        <div class="condition">

            ② <span class="red">

            上过龙虎榜

            </span>

        </div>

        <div class="condition">

            ③ <span class="green">

            总市值 ≤ 300亿

            </span>

        </div>

    </div>

    <div class="muted" style="margin-top:8px;">

        评分用于排序，不会突破以上三项硬条件。

    </div>

</div>

<div id="message"></div>

<div class="panel">

    <div class="section-title">

        🔥 TOP 3 强势标的

    </div>

    <div id="top3">

        <div class="loading">

            等待扫描

        </div>

    </div>

</div>

<div class="panel">

    <div class="section-title">

        📊 妖股概率排行

    </div>

    <div id="ranking">

        <div class="loading">

            等待扫描

        </div>

    </div>

</div>

<div class="footer">

    数据：东方财富公开行情接口<br>

    妖股雷达仅用于量化分析参考，不构成投资建议

</div>

</div>

<script>

const dateInput =

    document.getElementById("date");

const statusEl =

    document.getElementById("status");

const messageEl =

    document.getElementById("message");

const topEl =

    document.getElementById("top3");

const rankingEl =

    document.getElementById("ranking");

function todayText(){

    const d = new Date();

    const y = d.getFullYear();

    const m =

        String(d.getMonth()+1)

        .padStart(2,"0");

    const day =

        String(d.getDate())

        .padStart(2,"0");

    return `${y}-${m}-${day}`;

}

function today(){

    dateInput.value =

        todayText();

    scan();

}

function money(v){

    v = Number(v || 0);

    if(v >= 100){

        return v.toFixed(1)

            + "亿";

    }

    return v.toFixed(2)

        + "亿";

}

function pct(v){

    v = Number(v || 0);

    const s =

        v >= 0 ? "+" : "";

    return s + v.toFixed(2) + "%";

}

function card(item,index){

    const p =

        Number(item.pct || 0);

    const pctClass =

        p >= 0 ? "red" : "green";

    const reasons =

        (item.reasons || [])

        .join(" · ");

    return `

    <div class="card ${index < 3 ? "top" : ""}">

        <div class="row">

            <div>

                <span class="name">

                    ${item.name || item.code}

                </span>

                <span class="code">

                    ${item.code}

                </span>

            </div>

            <div class="score">

                ${Number(item.score || 0).toFixed(0)}

            </div>

        </div>

        <div class="data">

            <span class="tag">

                涨幅

                <b class="${pctClass}">

                    ${pct(p)}

                </b>

            </span>

            <span class="tag">

                涨停

                ${item.zt_count || 0}次

            </span>

            <span class="tag">

                换手

                ${Number(item.turnover || 0).toFixed(2)}%

            </span>

            <span class="tag">

                市值

                ${money(item.market_cap)}

            </span>

            <span class="tag">

                龙虎榜

                ✓

            </span>

        </div>

        <div class="reason">

            ${reasons || "满足核心条件"}

        </div>

    </div>

    `;

}

function render(data){

    if(!data){

        topEl.innerHTML =

            `<div class="empty">

                没有返回数据

             </div>`;

        return;

    }

    if(data.success === false){

        messageEl.innerHTML =

            `<div class="panel error">

                加载失败：

                ${data.error || "接口异常"}

             </div>`;

        topEl.innerHTML =

            `<div class="empty">

                暂无数据

             </div>`;

        rankingEl.innerHTML =

            `<div class="empty">

                暂无数据

             </div>`;

        return;

    }

    messageEl.innerHTML =

        `<div class="panel">

            <div class="muted">

                ${data.date || ""}

                · 更新时间：

                ${data.updated || ""}

            </div>

            <div style="margin-top:7px;">

                ${data.message || ""}

            </div>

         </div>`;

    const top =

        data.top3 || [];

    if(top.length){

        topEl.innerHTML =

            top.map((x,i)=>

                card(x,i)

            ).join("");

    }else{

        topEl.innerHTML =

            `<div class="empty">

                当前日期没有满足三项硬条件的股票

             </div>`;

    }

    const ranking =

        data.ranking || [];

    if(ranking.length){

        rankingEl.innerHTML =

            ranking.map((x,i)=>

                card(x,i)

            ).join("");

    }else{

        rankingEl.innerHTML =

            `<div class="empty">

                暂无符合条件的股票

             </div>`;

    }

    const stats =

        data.stats || {};

    statusEl.innerText =

        `候选 ${stats.hard_count || 0} 只`;

}

async function scan(){

    const date =

        dateInput.value ||

        todayText();

    statusEl.innerText =

        "数据读取中...";

    messageEl.innerHTML = "";

    topEl.innerHTML =

        `<div class="loading">

            正在扫描，请稍候...

         </div>`;

    rankingEl.innerHTML =

        `<div class="loading">

            正在计算...

         </div>`;

    try{

        const controller =

            new AbortController();

        const timer =

            setTimeout(

                () => controller.abort(),

                30000

            );

        const res =

            await fetch(

                `/api/scanner?date=${encodeURIComponent(date)}`,

                {

                    cache:"no-store",

                    signal:controller.signal

                }

            );

        clearTimeout(timer);

        if(!res.ok){

            throw new Error(

                "服务器 HTTP " +

                res.status

            );

        }

        const data =

            await res.json();

        render(data);

    }catch(e){

        statusEl.innerText =

            "数据读取失败";

        messageEl.innerHTML =

            `<div class="panel error">

                加载失败：

                ${e.message || "Load failed"}

             </div>`;

        topEl.innerHTML =

            `<div class="empty">

                请稍后重新扫描

             </div>`;

        rankingEl.innerHTML =

            `<div class="empty">

                暂无数据

             </div>`;

    }

}

dateInput.value =

    todayText();

// 页面打开自动扫描

scan();

</script>

</body>

</html>

"""

@app.get("/", response_class=HTMLResponse)

def home():

    return HTML

# =========================================================

# 启动

# =========================================================

if __name__ == "__main__":

    import uvicorn

    port = int(

        os.getenv("PORT", "8000")

    )

    uvicorn.run(

        app,

        host="0.0.0.0",

        port=port

    )
