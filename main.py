import os

import math

import time

import json

from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

from urllib.parse import quote, urlencode

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from fastapi import FastAPI, Query

from fastapi.responses import HTMLResponse, JSONResponse

from fastapi.middleware.cors import CORSMiddleware

# ============================================================

# 基础配置

# ============================================================

app = FastAPI(title="妖股雷达", version="2.0")

app.add_middleware(

    CORSMiddleware,

    allow_origins=["*"],

    allow_credentials=True,

    allow_methods=["*"],

    allow_headers=["*"],

)

TZ = ZoneInfo("Asia/Shanghai")

TIMEOUT = 8

DATA = "https://push2.eastmoney.com"

DATA2 = "https://push2his.eastmoney.com"

# 妙想 API Key：只从 Render 环境变量读取

MX_APIKEY = os.getenv("MX_APIKEY", "").strip()

# ============================================================

# HTTP

# ============================================================

SESSION = requests.Session()

SESSION.headers.update({

    "User-Agent": (

        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "

        "AppleWebKit/605.1.15 (KHTML, like Gecko) "

        "Version/17.0 Mobile/15E148 Safari/604.1"

    ),

    "Accept": "application/json,text/plain,*/*",

    "Referer": "https://quote.eastmoney.com/",

})

def http_get(url, params=None, timeout=TIMEOUT):

    try:

        r = SESSION.get(

            url,

            params=params,

            timeout=timeout,

        )

        r.raise_for_status()

        return r.json()

    except Exception:

        return None

# ============================================================

# 工具函数

# ============================================================

def safe_float(v, default=0.0):

    try:

        if v is None:

            return default

        if isinstance(v, str):

            v = v.replace(",", "").strip()

            if not v:

                return default

        x = float(v)

        if math.isnan(x) or math.isinf(x):

            return default

        return x

    except Exception:

        return default

def safe_int(v, default=0):

    try:

        if v is None:

            return default

        return int(float(v))

    except Exception:

        return default

def clamp(v, low, high):

    return max(low, min(high, v))

def fmt_num(v):

    try:

        v = float(v)

        if abs(v) >= 100000000:

            return f"{v / 100000000:.2f}亿"

        if abs(v) >= 10000:

            return f"{v / 10000:.2f}万"

        return f"{v:.0f}"

    except Exception:

        return "-"

def get_secid(code):

    code = str(code)

    if code.startswith(("6", "68")):

        return f"1.{code}"

    if code.startswith(("0", "3")):

        return f"0.{code}"

    return f"0.{code}"

def is_a_share(code):

    code = str(code)

    return (

        code.startswith("600")

        or code.startswith("601")

        or code.startswith("603")

        or code.startswith("605")

        or code.startswith("688")

        or code.startswith("000")

        or code.startswith("001")

        or code.startswith("002")

        or code.startswith("003")

        or code.startswith("300")

        or code.startswith("301")

    )

# ============================================================

# 交易日

# ============================================================

def get_trading_dates(end_date, days=10):

    """

    使用东方财富交易日历。

    如果接口失败，则使用工作日近似。

    """

    end_date = str(end_date)[:10]

    url = f"{DATA2}/api/qt/stock/get"

    params = {

        "secid": "1.000001",

        "fields": "f57,f58",

    }

    # 东方财富交易日历接口不稳定时，直接采用工作日回退。

    dates = []

    try:

        d = datetime.strptime(end_date, "%Y-%m-%d")

        for _ in range(30):

            if d.weekday() < 5:

                dates.append(d.strftime("%Y-%m-%d"))

            if len(dates) >= days:

                break

            d -= timedelta(days=1)

    except Exception:

        pass

    return dates

def get_actual_target_date(date_str):

    """

    周末/节假日自动回退到最近一个工作日。

    """

    if not date_str:

        date_str = datetime.now(TZ).strftime("%Y-%m-%d")

    date_str = str(date_str)[:10]

    try:

        d = datetime.strptime(date_str, "%Y-%m-%d")

        while d.weekday() >= 5:

            d -= timedelta(days=1)

        return d.strftime("%Y-%m-%d")

    except Exception:

        return datetime.now(TZ).strftime("%Y-%m-%d")

# ============================================================

# K线

# ============================================================

def get_kline(code, beg="0", end="20500101", lmt=10):

    secid = get_secid(code)

    url = f"{DATA2}/api/qt/stock/kline/get"

    params = {

        "secid": secid,

        "ut": "fa5fd1943c7b386f172d6893dbfba10b",

        "fields1": "f1,f2,f3,f4,f5,f6",

        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",

        "klt": "101",

        "fqt": "1",

        "beg": beg,

        "end": end,

        "smplmt": str(lmt),

        "lmt": str(lmt),

    }

    data = http_get(url, params)

    try:

        return data["data"]["klines"]

    except Exception:

        return []

# ============================================================

# 全A股

# ============================================================

def get_all_a_stocks():

    """

    获取A股股票池。

    """

    url = f"{DATA}/api/qt/clist/get"

    params = {

        "pn": "1",

        "pz": "6000",

        "po": "1",

        "np": "1",

        "fltt": "2",

        "invt": "2",

        "fid": "f3",

        "fs": (

            "m:0+t:6,m:0+t:80,"

            "m:1+t:2,m:1+t:23,"

            "m:0+t:81+s:2048"

        ),

        "fields": (

            "f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,"

            "f10,f15,f16,f17,f18,f20,f21"

        ),

    }

    data = http_get(url, params)

    result = []

    try:

        diff = data["data"]["diff"]

        if isinstance(diff, dict):

            diff = list(diff.values())

        for x in diff:

            code = str(x.get("f12", ""))

            if not is_a_share(code):

                continue

            market_cap = safe_float(x.get("f20")) / 100000000

            result.append({

                "code": code,

                "name": x.get("f14", ""),

                "price": safe_float(x.get("f2")),

                "pct": safe_float(x.get("f3")),

                "change": safe_float(x.get("f4")),

                "volume": safe_float(x.get("f5")),

                "amount": safe_float(x.get("f6")),

                "amplitude": safe_float(x.get("f7")),

                "turnover": safe_float(x.get("f8")),

                "pe": safe_float(x.get("f9")),

                "high": safe_float(x.get("f15")),

                "low": safe_float(x.get("f16")),

                "open": safe_float(x.get("f17")),

                "preclose": safe_float(x.get("f18")),

                "market_cap": market_cap,

            })

    except Exception:

        return []

    return result

# ============================================================

# 实时行情

# ============================================================

def get_quotes(codes):

    if not codes:

        return {}

    url = f"{DATA}/api/qt/ulist.np/get"

    secids = ",".join(get_secid(c) for c in codes)

    params = {

        "fltt": "2",

        "invt": "2",

        "fields": (

            "f12,f14,f2,f3,f4,f5,f6,f7,f8,"

            "f9,f10,f15,f16,f17,f18,f20,f21"

        ),

        "secids": secids,

    }

    data = http_get(url, params)

    result = {}

    try:

        diff = data["data"]["diff"]

        if isinstance(diff, dict):

            diff = list(diff.values())

        for x in diff:

            code = str(x.get("f12", ""))

            result[code] = {

                "code": code,

                "name": x.get("f14", ""),

                "price": safe_float(x.get("f2")),

                "pct": safe_float(x.get("f3")),

                "change": safe_float(x.get("f4")),

                "volume": safe_float(x.get("f5")),

                "amount": safe_float(x.get("f6")),

                "amplitude": safe_float(x.get("f7")),

                "turnover": safe_float(x.get("f8")),

                "pe": safe_float(x.get("f9")),

                "market_cap": safe_float(x.get("f20")) / 100000000,

            }

    except Exception:

        pass

    return result

# ============================================================

# 涨停池

# ============================================================

def get_zt_pool(date_str):

    """

    获取指定交易日涨停池。

    """

    url = f"{DATA}/api/data/v1/get"

    params = {

        "pn": "1",

        "pz": "200",

        "po": "1",

        "np": "1",

        "fltt": "2",

        "invt": "2",

        "fid": "f3",

        "fs": "m:90+t:3",

        "fields": (

            "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,"

            "f12,f13,f14,f15,f16,f17,f18,f20,f21,"

            "f22,f23,f24,f25,f26,f62,f100"

        ),

        "fid0": "f400",

        "fid1": "f3",

        "fid2": "f8",

        "fid3": "f20",

        "fid4": "f21",

        "fid5": "f22",

        "fid6": "f23",

        "fid7": "f24",

        "fid8": "f25",

        "fid9": "f26",

        "date": date_str.replace("-", ""),

    }

    data = http_get(url, params)

    result = {}

    try:

        diff = data["data"]["diff"]

        if isinstance(diff, dict):

            diff = list(diff.values())

        for x in diff:

            code = str(x.get("f12", ""))

            if not code:

                continue

            result[code] = {

                "code": code,

                "name": x.get("f14", ""),

                "pct": safe_float(x.get("f3")),

                "turnover": safe_float(x.get("f8")),

                "market_cap": safe_float(x.get("f20")) / 100000000,

            }

    except Exception:

        pass

    return result

def get_zt_info(code, dates):

    """

    判断近3个交易日是否出现过涨停。

    """

    count = 0

    latest = None

    for d in dates[:3]:

        pool = get_zt_pool(d)

        if code in pool:

            count += 1

            latest = d

    return {

        "zt_count": count,

        "zt_latest": latest,

    }

# ============================================================

# 龙虎榜

# ============================================================

def get_lhb(date_str):

    """

    获取指定交易日龙虎榜。

    这里特别修复 filter 参数的编码问题。

    """

    url = f"{DATA}/api/data/v1/get"

    filter_raw = (

        "(SECURITY_TYPE_CODE=\"05800101\")"

    )

    encoded_filter = quote(

        filter_raw,

        safe="()'="

    )

    params = {

        "pn": "1",

        "pz": "200",

        "po": "1",

        "np": "1",

        "ut": "fa5fd1943c7b386f172d6893dbfba10b",

        "fltt": "2",

        "invt": "2",

        "fid": "f3",

        "fs": "m:90+t:1",

        "fields": (

            "SECURITY_CODE,SECURITY_NAME_ABBR,"

            "TRADE_DATE,CLOSE_PRICE,"

            "CHANGE_RATE,"

            "ACCUM_AMOUNT,"

            "BUY_AMOUNT,"

            "SELL_AMOUNT,"

            "NET_BUY_AMOUNT,"

            "EXPLANATION"

        ),

    }

    try:

        query = urlencode(params)

        full_url = (

            f"{url}?"

            f"{query}"

            f"&filter={encoded_filter}"

        )

        data = http_get(full_url)

        result = {}

        if not data:

            return result

        diff = data.get("data", {}).get("diff", [])

        if isinstance(diff, dict):

            diff = list(diff.values())

        for x in diff:

            code = str(

                x.get("SECURITY_CODE")

                or x.get("f12")

                or ""

            )

            if not code:

                continue

            result[code] = {

                "code": code,

                "name": (

                    x.get("SECURITY_NAME_ABBR")

                    or x.get("f14")

                    or ""

                ),

                "change": safe_float(

                    x.get("CHANGE_RATE")

                    or x.get("f3")

                ),

                "buy": safe_float(

                    x.get("BUY_AMOUNT")

                    or x.get("f20")

                ),

                "sell": safe_float(

                    x.get("SELL_AMOUNT")

                    or x.get("f21")

                ),

                "net": safe_float(

                    x.get("NET_BUY_AMOUNT")

                    or x.get("f22")

                ),

                "explanation": x.get("EXPLANATION", ""),

            }

        return result

    except Exception:

        return {}

# ============================================================

# 集合竞价 / 五档

# ============================================================

def get_order_book(code):

    """

    获取盘口数据。

    注意：

    历史日期无法可靠恢复当日集合竞价盘口，

    所以只对当前交易日附近的数据作为辅助评分。

    """

    secid = get_secid(code)

    url = f"{DATA}/api/qt/stock/get"

    params = {

        "secid": secid,

        "fields": (

            "f57,f58,f43,f44,f45,f46,f47,f48,"

            "f50,f51,f52,f53,f54,f55,f56,f57,"

            "f58,f59,f60,f61,f62,f63,f64,f65,"

            "f66,f67,f68,f69,f70,f71,f72,f73,"

            "f74,f75,f76,f77,f78,f79,f80"

        ),

    }

    data = http_get(url, params, timeout=5)

    if not data:

        return {}

    try:

        x = data.get("data", {})

        return {

            "bid1": safe_float(x.get("f19")),

            "ask1": safe_float(x.get("f20")),

            "bid_vol": safe_float(x.get("f21")),

            "ask_vol": safe_float(x.get("f22")),

        }

    except Exception:

        return {}

# ============================================================

# 妙想接口，可选

# ============================================================

def get_mx_candidates():

    """

    妙想接口是可选增强项。

    没有 KEY 或接口失败，不影响主扫描。

    """

    if not MX_APIKEY:

        return set()

    # 不依赖妙想接口才能运行。

    # 这里保留为空集合，避免外部接口拖慢整个扫描。

    return set()

# ============================================================

# 预选评分

# ============================================================

def pre_score(stock, zt_map, lhb_map):

    """

    备选池预评分。

    这里非常重要：

    不再要求：

        涨停 AND 龙虎榜 AND 市值<=300亿

    而是：

        这些指标全部变成加分项。

    因此只满足一个、两个甚至都不满足，

    只要当前行情足够强，也可以进入备选池。

    """

    code = stock["code"]

    pct = stock.get("pct", 0)

    turnover = stock.get("turnover", 0)

    amount = stock.get("amount", 0)

    market_cap = stock.get("market_cap", 0)

    score = 0

    # --------------------------------------------------------

    # 1. 涨幅

    # --------------------------------------------------------

    if pct >= 9.5:

        score += 35

    elif pct >= 7:

        score += 28

    elif pct >= 5:

        score += 20

    elif pct >= 3:

        score += 12

    elif pct >= 0:

        score += 5

    # --------------------------------------------------------

    # 2. 换手

    # --------------------------------------------------------

    if turnover >= 30:

        score += 30

    elif turnover >= 20:

        score += 24

    elif turnover >= 12:

        score += 18

    elif turnover >= 8:

        score += 12

    elif turnover >= 5:

        score += 6

    # --------------------------------------------------------

    # 3. 成交额

    # --------------------------------------------------------

    if amount >= 5e8:

        score += 15

    elif amount >= 2e8:

        score += 12

    elif amount >= 1e8:

        score += 9

    elif amount >= 5e7:

        score += 5

    # --------------------------------------------------------

    # 4. 近3日涨停 —— 加分项

    # --------------------------------------------------------

    zt_count = zt_map.get(code, {}).get("zt_count", 0)

    if zt_count >= 3:

        score += 25

    elif zt_count == 2:

        score += 20

    elif zt_count == 1:

        score += 15

    # --------------------------------------------------------

    # 5. 龙虎榜 —— 加分项

    # --------------------------------------------------------

    if code in lhb_map:

        score += 18

        net = lhb_map.get(code, {}).get("net", 0)

        if net > 0:

            score += 8

    # --------------------------------------------------------

    # 6. 市值≤300亿 —— 加分项

    # --------------------------------------------------------

    if 0 < market_cap <= 300:

        score += 18

        if market_cap <= 100:

            score += 5

    elif market_cap <= 500:

        score += 7

    # --------------------------------------------------------

    # 7. 小盘 + 高换手 + 高涨幅组合

    # --------------------------------------------------------

    if (

        0 < market_cap <= 300

        and turnover >= 15

        and pct >= 5

    ):

        score += 15

    return score

# ============================================================

# 最终妖股概率评分

# ============================================================

def calculate_score(stock, zt_info, lhb_info, order_book=None, mx=False):

    """

    最终评分。

    满分约100+，最后转成概率。

    """

    pct = stock.get("pct", 0)

    turnover = stock.get("turnover", 0)

    amount = stock.get("amount", 0)

    market_cap = stock.get("market_cap", 0)

    zt_count = zt_info.get("zt_count", 0)

    score = 0

    # ========================================================

    # A. 当前强度

    # ========================================================

    if pct >= 9.5:

        score += 25

    elif pct >= 7:

        score += 21

    elif pct >= 5:

        score += 17

    elif pct >= 3:

        score += 12

    elif pct >= 1:

        score += 6

    elif pct >= 0:

        score += 2

    # ========================================================

    # B. 换手

    # ========================================================

    if turnover >= 29.25:

        score += 20

    elif turnover >= 20:

        score += 16

    elif turnover >= 12:

        score += 12

    elif turnover >= 8:

        score += 8

    elif turnover >= 5:

        score += 4

    # ========================================================

    # C. 成交额

    # ========================================================

    if amount >= 5e8:

        score += 10

    elif amount >= 2e8:

        score += 8

    elif amount >= 1e8:

        score += 6

    elif amount >= 5e7:

        score += 4

    # ========================================================

    # D. 近3日涨停

    # ========================================================

    if zt_count >= 3:

        score += 20

    elif zt_count == 2:

        score += 16

    elif zt_count == 1:

        score += 12

    # ========================================================

    # E. 龙虎榜

    # ========================================================

    if lhb_info:

        score += 12

        net = safe_float(lhb_info.get("net"))

        if net > 0:

            score += 5

    # ========================================================

    # F. 市值

    # ========================================================

    if 0 < market_cap <= 300:

        score += 15

        if market_cap <= 100:

            score += 4

    elif market_cap <= 500:

        score += 5

    # ========================================================

    # G. 竞价/盘口辅助

    # ========================================================

    if order_book:

        bid_vol = safe_float(order_book.get("bid_vol"))

        ask_vol = safe_float(order_book.get("ask_vol"))

        if ask_vol > bid_vol and ask_vol > 0:

            score += 3

        if bid_vol > ask_vol and bid_vol > 0:

            score += 2

    # ========================================================

    # H. 妙想增强

    # ========================================================

    if mx:

        score += 5

    # ========================================================

    # 概率映射

    # ========================================================

    probability = 35 + score * 0.62

    # 强势股票最高显示99%

    probability = clamp(probability, 35, 99)

    return {

        "score": round(score, 1),

        "probability": round(probability, 1),

    }

# ============================================================

# 单只股票分析

# ============================================================

def analyze_one(stock, zt_map, lhb_map, order_books, mx_codes):

    code = stock["code"]

    zt_info = zt_map.get(

        code,

        {

            "zt_count": 0,

            "zt_latest": None,

        }

    )

    lhb_info = lhb_map.get(code)

    order_book = order_books.get(code)

    result_score = calculate_score(

        stock,

        zt_info,

        lhb_info,

        order_book,

        code in mx_codes,

    )

    reasons = []

    pct = stock.get("pct", 0)

    turnover = stock.get("turnover", 0)

    market_cap = stock.get("market_cap", 0)

    # 当前强势

    if pct >= 5:

        reasons.append(f"涨幅{pct:.1f}%")

    elif pct >= 3:

        reasons.append(f"涨幅{pct:.1f}%")

    # 换手

    if turnover >= 29.25:

        reasons.append(f"高换手{turnover:.1f}%")

    elif turnover >= 15:

        reasons.append(f"换手{turnover:.1f}%")

    # 涨停

    if zt_info["zt_count"] > 0:

        reasons.append(f"近3日涨停{zt_info['zt_count']}次")

    # 龙虎榜

    if lhb_info:

        reasons.append("龙虎榜")

        if safe_float(lhb_info.get("net")) > 0:

            reasons.append("龙虎榜净买")

    # 市值

    if 0 < market_cap <= 300:

        reasons.append(f"市值{market_cap:.0f}亿")

    # 小市值高换手

    if (

        0 < market_cap <= 300

        and turnover >= 15

        and pct >= 5

    ):

        reasons.append("小市值+高换手")

    if not reasons:

        reasons.append("当前资金强度较高")

    return {

        **stock,

        "zt_count": zt_info["zt_count"],

        "zt_latest": zt_info["zt_latest"],

        "lhb": bool(lhb_info),

        "lhb_net": (

            safe_float(lhb_info.get("net"))

            if lhb_info

            else 0

        ),

        "score": result_score["score"],

        "probability": result_score["probability"],

        "reasons": reasons,

    }

# ============================================================

# 扫描主程序

# ============================================================

def build_scan(requested_date=None):

    start_time = time.time()

    if not requested_date:

        requested_date = datetime.now(TZ).strftime("%Y-%m-%d")

    actual_date = get_actual_target_date(requested_date)

    # --------------------------------------------------------

    # 最近3个交易日

    # --------------------------------------------------------

    trading_dates = get_trading_dates(

        actual_date,

        days=5

    )

    if actual_date not in trading_dates:

        trading_dates.insert(0, actual_date)

    trading_dates = trading_dates[:3]

    # --------------------------------------------------------

    # 股票全集

    # --------------------------------------------------------

    stocks = get_all_a_stocks()

    if not stocks:

        return {

            "success": False,

            "message": "东方财富行情接口暂时没有返回股票数据",

            "requested_date": requested_date,

            "actual_date": actual_date,

            "count": 0,

            "top3": [],

            "ranking": [],

        }

    # --------------------------------------------------------

    # 获取近3日涨停池

    # --------------------------------------------------------

    zt_by_date = {}

    with ThreadPoolExecutor(max_workers=3) as executor:

        futures = {

            executor.submit(get_zt_pool, d): d

            for d in trading_dates

        }

        for future in as_completed(futures):

            d = futures[future]

            try:

                zt_by_date[d] = future.result() or {}

            except Exception:

                zt_by_date[d] = {}

    # --------------------------------------------------------

    # 合并涨停数据

    # --------------------------------------------------------

    zt_map = {}

    for d, pool in zt_by_date.items():

        for code in pool.keys():

            if code not in zt_map:

                zt_map[code] = {

                    "zt_count": 0,

                    "zt_latest": None,

                }

            zt_map[code]["zt_count"] += 1

            if (

                zt_map[code]["zt_latest"] is None

                or d > zt_map[code]["zt_latest"]

            ):

                zt_map[code]["zt_latest"] = d

    # --------------------------------------------------------

    # 龙虎榜

    # --------------------------------------------------------

    lhb_map = get_lhb(actual_date)

    # --------------------------------------------------------

    # 预选

    #

    # 关键：

    # 不再使用：

    #

    #     涨停 AND 龙虎榜 AND 市值<=300

    #

    # 而是从全部A股中按照综合强度取备选。

    # --------------------------------------------------------

    scored_universe = []

    for stock in stocks:

        score = pre_score(

            stock,

            zt_map,

            lhb_map

        )

        stock["_pre_score"] = score

        scored_universe.append(stock)

    scored_universe.sort(

        key=lambda x: (

            x.get("_pre_score", 0),

            x.get("pct", 0),

            x.get("turnover", 0),

            x.get("amount", 0),

        ),

        reverse=True

    )

    # --------------------------------------------------------

    # 取前350作为主要候选

    # --------------------------------------------------------

    candidates = scored_universe[:350]

    # --------------------------------------------------------

    # 把所有涨停股和龙虎榜股加入候选池

    #

    # 防止特殊强势股因为实时行情排名暂时下降而漏掉。

    # --------------------------------------------------------

    special_codes = set(zt_map.keys()) | set(lhb_map.keys())

    existing_codes = {

        x["code"]

        for x in candidates

    }

    stock_map = {

        x["code"]: x

        for x in stocks

    }

    for code in special_codes:

        if code in existing_codes:

            continue

        if code in stock_map:

            candidates.append(

                stock_map[code]

            )

    # --------------------------------------------------------

    # 重新按预评分排序

    # --------------------------------------------------------

    candidates.sort(

        key=lambda x: (

            x.get("_pre_score", 0),

            x.get("pct", 0),

            x.get("turnover", 0),

        ),

        reverse=True

    )

    # 防止候选池过大

    candidates = candidates[:380]

    # --------------------------------------------------------

    # 盘口只查最强前20

    # --------------------------------------------------------

    order_books = {}

    order_targets = candidates[:20]

    with ThreadPoolExecutor(max_workers=10) as executor:

        futures = {

            executor.submit(

                get_order_book,

                x["code"]

            ): x["code"]

            for x in order_targets

        }

        for future in as_completed(futures):

            code = futures[future]

            try:

                order_books[code] = future.result() or {}

            except Exception:

                order_books[code] = {}

    # --------------------------------------------------------

    # 妙想

    # --------------------------------------------------------

    mx_codes = get_mx_candidates()

    # --------------------------------------------------------

    # 最终分析

    # --------------------------------------------------------

    results = []

    for stock in candidates:

        result = analyze_one(

            stock,

            zt_map,

            lhb_map,

            order_books,

            mx_codes

        )

        # 内部字段不返回

        result.pop("_pre_score", None)

        results.append(result)

    # --------------------------------------------------------

    # 最终排序

    # --------------------------------------------------------

    results.sort(

        key=lambda x: (

            x.get("probability", 0),

            x.get("score", 0),

            x.get("pct", 0),

            x.get("turnover", 0),

        ),

        reverse=True

    )

    # --------------------------------------------------------

    # TOP3

    # --------------------------------------------------------

    top3 = results[:3]

    elapsed = round(

        time.time() - start_time,

        2

    )

    return {

        "success": True,

        "requested_date": requested_date,

        "actual_date": actual_date,

        "trading_dates": trading_dates,

        "count": len(results),

        "top3": top3,

        "ranking": results[:50],

        "elapsed": elapsed,

        "message": (

            "备选池模式：涨停、龙虎榜、市值≤300亿均为加分项，"

            "不是硬性条件。"

        ),

        "rules": {

            "zt_3day": "加分项",

            "lhb": "加分项",

            "market_cap_300": "加分项",

            "candidate_mode": True,

        },

    }

# ============================================================

# API

# ============================================================

@app.get("/api/health")

def health():

    return {

        "success": True,

        "service": "妖股雷达",

        "version": "2.0",

        "time": datetime.now(TZ).isoformat(),

    }

@app.get("/api/scanner")

def scanner(

    date: str = Query(

        default=None,

        description="YYYY-MM-DD"

    )

):

    try:

        result = build_scan(date)

        return JSONResponse(

            content=result

        )

    except Exception as e:

        return JSONResponse(

            status_code=500,

            content={

                "success": False,

                "message": f"扫描失败：{str(e)}",

                "count": 0,

                "top3": [],

                "ranking": [],

            }

        )

# ============================================================

# 手机网页

# ============================================================

HTML = r"""

<!DOCTYPE html>

<html lang="zh-CN">

<head>

<meta charset="UTF-8">

<meta

name="viewport"

content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"

>

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

        Arial,

        sans-serif;

}

.container{

    width:100%;

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

    font-size:25px;

    font-weight:800;

}

.status{

    font-size:13px;

    color:#aaa;

}

.panel{

    background:#101010;

    border:1px solid #242424;

    border-radius:14px;

    padding:14px;

    margin-bottom:12px;

}

.controls{

    display:flex;

    gap:8px;

    flex-wrap:wrap;

}

input,

button{

    border-radius:9px;

    border:1px solid #333;

    background:#181818;

    color:#fff;

    padding:10px 13px;

    font-size:15px;

}

button{

    cursor:pointer;

}

button.primary{

    background:#c62828;

    border-color:#d32f2f;

}

button.secondary{

    background:#222;

}

.rules{

    margin-top:12px;

    display:grid;

    gap:7px;

}

.rule{

    background:#171717;

    border-radius:9px;

    padding:9px;

    font-size:13px;

}

.rule b{

    color:#ffcc66;

}

.note{

    color:#999;

    font-size:12px;

    margin-top:10px;

    line-height:1.6;

}

.top3{

    display:grid;

    gap:10px;

}

.card{

    background:#151515;

    border:1px solid #292929;

    border-radius:13px;

    padding:13px;

}

.card.hot{

    border-color:#8b2525;

}

.card-head{

    display:flex;

    justify-content:space-between;

    align-items:center;

}

.stock-name{

    font-size:18px;

    font-weight:800;

}

.code{

    color:#888;

    font-size:12px;

    margin-left:6px;

}

.prob{

    color:#ff4d4d;

    font-size:20px;

    font-weight:900;

}

.up{

    color:#ff4d4d;

}

.down{

    color:#36d77b;

}

.metrics{

    display:grid;

    grid-template-columns:repeat(3,1fr);

    gap:7px;

    margin-top:10px;

}

.metric{

    background:#0c0c0c;

    border-radius:8px;

    padding:8px;

}

.metric-title{

    font-size:11px;

    color:#777;

}

.metric-value{

    font-size:14px;

    margin-top:3px;

    font-weight:700;

}

.tags{

    display:flex;

    flex-wrap:wrap;

    gap:6px;

    margin-top:10px;

}

.tag{

    background:#242424;

    color:#bbb;

    border-radius:6px;

    padding:4px 7px;

    font-size:11px;

}

.tag.red{

    color:#ff6565;

}

.tag.green{

    color:#45db8a;

}

.empty{

    padding:30px 10px;

    text-align:center;

    color:#777;

}

.loading{

    padding:20px;

    text-align:center;

    color:#aaa;

}

.rank-title{

    font-size:18px;

    font-weight:800;

    margin-bottom:10px;

}

.rank{

    display:grid;

    grid-template-columns:32px 1fr auto;

    gap:8px;

    align-items:center;

    padding:11px 0;

    border-bottom:1px solid #222;

}

.rank:last-child{

    border-bottom:0;

}

.rank-no{

    color:#777;

    font-weight:700;

}

.rank-name{

    font-weight:700;

}

.rank-sub{

    color:#777;

    font-size:11px;

    margin-top:3px;

}

.rank-prob{

    font-size:16px;

    color:#ff5555;

    font-weight:800;

}

.footer{

    color:#555;

    font-size:11px;

    text-align:center;

    line-height:1.6;

    padding:15px 0 30px;

}

@media(max-width:500px){

    .container{

        padding:10px;

    }

    .title{

        font-size:22px;

    }

    .metrics{

        grid-template-columns:repeat(3,1fr);

    }

    input,

    button{

        flex:1;

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

        <div class="status" id="status">

            候选 0 只

        </div>

    </div>

    <div class="panel">

        <div class="controls">

            <input

                id="date"

                type="date"

            >

            <button

                class="primary"

                onclick="scan()"

            >

                开始扫描

            </button>

            <button

                class="secondary"

                onclick="today()"

            >

                今天

            </button>

        </div>

        <div class="rules">

            <div class="rule">

                ① <b>近3个交易日出现涨停</b>

                → 加分项

            </div>

            <div class="rule">

                ② <b>上过龙虎榜</b>

                → 加分项

            </div>

            <div class="rule">

                ③ <b>总市值 ≤ 300亿</b>

                → 加分项

            </div>

        </div>

        <div class="note">

            ⚠️ 以上三项全部为加分项，不要求全部满足。

            备选池会同时综合涨幅、换手率、成交额、

            涨停次数、龙虎榜及市值等指标进行排名。

        </div>

    </div>

    <div class="panel">

        <div class="rank-title">

            🏆 TOP 3 强势备选

        </div>

        <div

            id="top3"

            class="top3"

        >

            <div class="empty">

                点击“开始扫描”

            </div>

        </div>

    </div>

    <div class="panel">

        <div class="rank-title">

            📊 妖股概率排行

        </div>

        <div id="ranking">

            <div class="empty">

                暂无数据

            </div>

        </div>

    </div>

    <div class="footer">

        数据来源：东方财富公开行情接口<br>

        本工具仅用于数据分析，不构成投资建议

    </div>

</div>

<script>

function escapeHtml(text){

    if(text === null || text === undefined){

        return "";

    }

    return String(text)

        .replaceAll("&","&amp;")

        .replaceAll("<","&lt;")

        .replaceAll(">","&gt;")

        .replaceAll('"',"&quot;")

        .replaceAll("'","&#039;");

}

function today(){

    const d = new Date();

    const y = d.getFullYear();

    const m = String(

        d.getMonth()+1

    ).padStart(2,"0");

    const day = String(

        d.getDate()

    ).padStart(2,"0");

    document.getElementById("date").value =

        `${y}-${m}-${day}`;

    scan();

}

function renderCard(stock, index){

    const pct =

        Number(stock.pct || 0);

    const pctClass =

        pct >= 0 ? "up" : "down";

    const tags =

        (stock.reasons || [])

        .map(x =>

            `<span class="tag red">

                ${escapeHtml(x)}

            </span>`

        )

        .join("");

    return `

        <div class="card ${index === 0 ? "hot" : ""}">

            <div class="card-head">

                <div>

                    <span class="stock-name">

                        ${escapeHtml(stock.name || "-")}

                    </span>

                    <span class="code">

                        ${escapeHtml(stock.code || "")}

                    </span>

                </div>

                <div class="prob">

                    ${Number(stock.probability || 0).toFixed(1)}%

                </div>

            </div>

            <div class="metrics">

                <div class="metric">

                    <div class="metric-title">

                        涨幅

                    </div>

                    <div class="metric-value ${pctClass}">

                        ${pct.toFixed(2)}%

                    </div>

                </div>

                <div class="metric">

                    <div class="metric-title">

                        换手

                    </div>

                    <div class="metric-value">

                        ${Number(stock.turnover || 0).toFixed(2)}%

                    </div>

                </div>

                <div class="metric">

                    <div class="metric-title">

                        市值

                    </div>

                    <div class="metric-value">

                        ${Number(stock.market_cap || 0).toFixed(0)}亿

                    </div>

                </div>

            </div>

            <div class="tags">

                ${tags}

            </div>

        </div>

    `;

}

function renderRank(stock, index){

    const pct =

        Number(stock.pct || 0);

    return `

        <div class="rank">

            <div class="rank-no">

                ${index + 1}

            </div>

            <div>

                <div class="rank-name">

                    ${escapeHtml(stock.name || "-")}

                    <span class="code">

                        ${escapeHtml(stock.code || "")}

                    </span>

                </div>

                <div class="rank-sub">

                    涨幅 ${pct.toFixed(2)}%

                    ·

                    换手 ${Number(stock.turnover || 0).toFixed(2)}%

                    ·

                    涨停 ${Number(stock.zt_count || 0)}次

                    ·

                    ${stock.lhb ? "龙虎榜" : "未上龙虎榜"}

                </div>

            </div>

            <div class="rank-prob">

                ${Number(stock.probability || 0).toFixed(1)}%

            </div>

        </div>

    `;

}

async function scan(){

    const date =

        document.getElementById("date").value;

    const top3 =

        document.getElementById("top3");

    const ranking =

        document.getElementById("ranking");

    const status =

        document.getElementById("status");

    top3.innerHTML =

        `<div class="loading">

            正在扫描，请稍候…

        </div>`;

    ranking.innerHTML =

        `<div class="loading">

            正在计算…

        </div>`;

    try{

        let url =

            "/api/scanner";

        if(date){

            url +=

                "?date=" +

                encodeURIComponent(date);

        }

        const response =

            await fetch(url);

        const data =

            await response.json();

        if(!data.success){

            top3.innerHTML =

                `<div class="empty">

                    ${escapeHtml(data.message || "扫描失败")}

                </div>`;

            ranking.innerHTML =

                `<div class="empty">

                    暂无数据

                </div>`;

            status.innerText =

                "扫描失败";

            return;

        }

        status.innerText =

            `候选 ${data.count || 0} 只`;

        const arr =

            data.top3 || [];

        if(arr.length === 0){

            top3.innerHTML =

                `<div class="empty">

                    当前没有足够的候选股票

                </div>`;

        }else{

            top3.innerHTML =

                arr

                .map((x,i) =>

                    renderCard(x,i)

                )

                .join("");

        }

        const rank =

            data.ranking || [];

        if(rank.length === 0){

            ranking.innerHTML =

                `<div class="empty">

                    暂无符合条件的股票

                </div>`;

        }else{

            ranking.innerHTML =

                rank

                .map((x,i) =>

                    renderRank(x,i)

                )

                .join("");

        }

    }catch(e){

        top3.innerHTML =

            `<div class="empty">

                网络或服务器错误

            </div>`;

        ranking.innerHTML =

            `<div class="empty">

                请稍后重试

            </div>`;

        status.innerText =

            "连接失败";

    }

}

function setDefaultDate(){

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

    document.getElementById("date").value =

        `${y}-${m}-${day}`;

}

setDefaultDate();

</script>

</body>

</html>

"""

# ============================================================

# 首页

# ============================================================

@app.get("/", response_class=HTMLResponse)

def index():

    return HTMLResponse(

        content=HTML

    )

# ============================================================

# 启动

# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(

        app,

        host="0.0.0.0",

        port=int(

            os.getenv(

                "PORT",

                "8000"

            )

        ),

    )
