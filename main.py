import os
import re
import time
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode, quote

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from zoneinfo import ZoneInfo

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

    s = str(code).strip().upper()

    s = (
        s.replace("SH", "")
         .replace("SZ", "")
         .replace("BJ", "")
         .replace(".", "")
    )

    if not s.isdigit():
        m = re.search(r"(\d{6})", s)
        if m:
            s = m.group(1)

    return s.zfill(6) if s else ""


def secid(code):
    code = clean_code(code)

    if code.startswith(("6", "68", "69")):
        return f"1.{code}"

    return f"0.{code}"


def is_st(name):
    return "ST" in str(name or "").upper()


def normalize_date(value):
    if not value:
        return TODAY

    s = str(value).replace("/", "-")

    if "T" in s:
        s = s.split("T")[0]

    if " " in s:
        s = s.split(" ")[0]

    try:
        return datetime.strptime(
            s[:10],
            "%Y-%m-%d"
        ).strftime("%Y-%m-%d")
    except Exception:
        return TODAY


def get_json(url, params=None, timeout=TIMEOUT):
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

def get_trading_dates(end_date, count=15):

    try:
        dt = datetime.strptime(
            end_date,
            "%Y-%m-%d"
        )
    except Exception:
        dt = datetime.now(TZ)

    begin = (
        dt - timedelta(days=80)
    ).strftime("%Y%m%d")

    url = f"{BASE}/api/qt/stock/kline/get"

    params = {
        "secid": "1.000001",
        "klt": "101",
        "fqt": "0",
        "beg": begin,
        "end": dt.strftime("%Y%m%d"),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": (
            "f51,f52,f53,f54,f55,"
            "f56,f57,f58,f59,f60,f61"
        )
    }

    data = get_json(
        url,
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
            d = normalize_date(
                str(row).split(",")[0]
            )

            if d not in dates:
                dates.append(d)

        except Exception:
            pass

    dates.sort()

    return dates[-count:]


def get_actual_target_date(target_date):

    target_date = normalize_date(
        target_date
    )

    dates = get_trading_dates(
        target_date,
        15
    )

    if not dates:
        return target_date

    if target_date in dates:
        return target_date

    previous = [
        d for d in dates
        if d <= target_date
    ]

    if previous:
        return max(previous)

    return dates[-1]


# =========================================================
# K线
# =========================================================

def get_kline(
    code,
    limit=120,
    end_date=None
):

    code = clean_code(code)

    if not code:
        return []

    end_date = normalize_date(
        end_date or TODAY
    )

    try:
        dt = datetime.strptime(
            end_date,
            "%Y-%m-%d"
        )
    except Exception:
        dt = datetime.now(TZ)

    begin = (
        dt - timedelta(days=300)
    ).strftime("%Y%m%d")

    url = f"{BASE}/api/qt/stock/kline/get"

    params = {
        "secid": secid(code),
        "klt": "101",
        "fqt": "1",
        "beg": begin,
        "end": dt.strftime("%Y%m%d"),
        "lmt": str(limit),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": (
            "f51,f52,f53,f54,f55,"
            "f56,f57,f58,f59,f60,f61"
        )
    }

    data = get_json(
        url,
        params
    )

    rows = (
        data.get("data", {}).get("klines", [])
        if isinstance(data, dict)
        else []
    )

    result = []

    for row in rows:

        try:

            p = str(row).split(",")

            if len(p) < 7:
                continue

            result.append({
                "date": normalize_date(p[0]),
                "open": safe_float(p[1]),
                "close": safe_float(p[2]),
                "high": safe_float(p[3]),
                "low": safe_float(p[4]),
                "volume": safe_float(p[5]),
                "amount": safe_float(p[6])
            })

        except Exception:
            continue

    return result


# =========================================================
# 全A股股票池
# =========================================================

def get_all_a_stocks():

    url = f"{BASE}/api/qt/clist/get"

    params = {
        "pn": "1",
        "pz": "5000",
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",

        # 沪深京A股
        "fs": (
            "m:0+t:6,"
            "m:0+t:80,"
            "m:1+t:2,"
            "m:1+t:23"
        ),

        "fields": (
            "f2,f3,f8,f12,f14,"
            "f20,f21"
        )
    }

    data = get_json(
        url,
        params,
        timeout=8
    )

    diff = (
        data.get("data", {}).get("diff", [])
        if isinstance(data, dict)
        else []
    )

    if isinstance(diff, dict):
        diff = list(diff.values())

    result = {}

    for x in diff:

        code = clean_code(
            x.get("f12")
        )

        name = str(
            x.get("f14") or ""
        )

        if len(code) != 6:
            continue

        if is_st(name):
            continue

        # 只保留A股常见代码
        if not code.startswith((
            "000", "001", "002", "003",
            "300", "301",
            "600", "601", "603", "605",
            "688", "689",
            "8", "4"
        )):
            continue

        # 东方财富 f20/f21 通常为元
        # 转换为亿元
        market_cap = (
            safe_float(x.get("f20"))
            / 100000000
        )

        float_cap = (
            safe_float(x.get("f21"))
            / 100000000
        )

        result[code] = {

            "price":
                safe_float(x.get("f2")),

            "pct":
                safe_float(x.get("f3")),

            "turnover":
                safe_float(x.get("f8")),

            "name":
                name,

            "market_cap":
                market_cap,

            "float_cap":
                float_cap
        }

    return result


# =========================================================
# 行情
# =========================================================

def get_quotes(codes):

    codes = list(dict.fromkeys(
        clean_code(x)
        for x in codes
        if clean_code(x)
    ))

    if not codes:
        return {}

    result = {}

    url = f"{BASE}/api/qt/ulist.np/get"

    for i in range(
        0,
        len(codes),
        80
    ):

        batch = codes[
            i:i + 80
        ]

        params = {

            "fltt": "2",
            "invt": "2",
            "np": "1",

            "secids":
                ",".join(
                    secid(x)
                    for x in batch
                ),

            "fields":
                "f2,f3,f8,f12,f14,f20,f21"
        }

        data = get_json(
            url,
            params
        )

        diff = (
            data.get("data", {}).get("diff", [])
            if isinstance(data, dict)
            else []
        )

        if isinstance(diff, dict):
            diff = list(diff.values())

        for x in diff:

            code = clean_code(
                x.get("f12")
            )

            if not code:
                continue

            result[code] = {

                "price":
                    safe_float(x.get("f2")),

                "pct":
                    safe_float(x.get("f3")),

                "turnover":
                    safe_float(x.get("f8")),

                "name":
                    x.get("f14") or code,

                "market_cap":
                    safe_float(x.get("f20"))
                    / 100000000,

                "float_cap":
                    safe_float(x.get("f21"))
                    / 100000000
            }

    return result


# =========================================================
# 涨停池
# =========================================================

def get_zt_pool(date):

    date = normalize_date(date)

    url = f"{BASE_EX}/getTopicZTPool"

    params = {

        "ut":
            "7eea3edcaed734bea9cbfcf3c6a2c7f1",

        "dpt":
            "wz.ztzt",

        "Pageindex":
            "0",

        # 不再使用6000
        "pagesize":
            "200",

        "sort":
            "fbt:asc",

        "date":
            date.replace("-", ""),

        "type":
            "zt",

        "zttj":
            "st",

        "iszt":
            "1"
    }

    data = get_json(
        url,
        params
    )

    pool = (
        data.get("data", {}).get("pool", [])
        if isinstance(data, dict)
        else []
    )

    return (
        pool
        if isinstance(pool, list)
        else []
    )


def get_zt_info(dates):

    counts = {}
    codes = set()

    for d in dates:

        pool = get_zt_pool(d)

        for x in pool:

            code = clean_code(
                x.get("c")
                or x.get("code")
                or x.get("SECURITY_CODE")
            )

            if not code:
                continue

            codes.add(code)

            counts[code] = (
                counts.get(code, 0)
                + 1
            )

    return codes, counts


# =========================================================
# 龙虎榜
# =========================================================

def get_lhb(
    codes=None,
    dates=None
):

    if dates is None:
        dates = [TODAY]

    dates = [
        normalize_date(x)
        for x in dates
    ]

    begin = min(dates)
    end = max(dates)

    filter_raw = (
        f"(TRADE_DATE>='{begin}')"
        f"(TRADE_DATE<='{end}')"
    )

    params = {

        "reportName":
            "RPT_DAILYBILLBOARD_DETAILSNEW",

        "columns":
            "ALL",

        "pageNumber":
            "1",

        "pageSize":
            "500",

        "sortColumns":
            "TRADE_DATE",

        "sortTypes":
            "-1",

        "source":
            "WEB",

        "client":
            "WEB"
    }

    try:

        url = (
            f"{DATA}/api/data/v1/get?"
            f"{urlencode(params)}"
            f"&filter="
            f"{quote(filter_raw, safe=\"()'=\")}"
        )

        data = get_json(
            url,
            timeout=8
        )

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

        result[code]["dates"].add(
            trade_date
        )

        result[code]["net"] += net

    return result


# =========================================================
# 盘口
# =========================================================

def get_order_book(code):

    code = clean_code(code)

    url = f"{BASE}/api/qt/stock/get"

    params = {

        "secid":
            secid(code),

        "fields":
            (
                "f43,f44,f45,f46,f47,f48,"
                "f57,f58,f59,f60,f61,f62,"
                "f63,f64,f65,f66,f67,f68"
            )
    }

    data = get_json(
        url,
        params
    )

    d = (
        data.get("data", {})
        if isinstance(data, dict)
        else {}
    )

    if not d:
        return {
            "buy": 0,
            "sell": 0,
            "ratio": 0
        }

    buy = sum(
        safe_float(d.get(k))
        for k in (
            "f58",
            "f60",
            "f62",
            "f64",
            "f66"
        )
    )

    sell = sum(
        safe_float(d.get(k))
        for k in (
            "f57",
            "f59",
            "f61",
            "f63",
            "f65"
        )
    )

    total = buy + sell

    return {

        "buy":
            buy,

        "sell":
            sell,

        "ratio":
            sell / total
            if total > 0
            else 0
    }


# =========================================================
# 妙想增强
# =========================================================

def get_mx_candidates():

    api_key = os.getenv(
        "MX_APIKEY",
        ""
    ).strip()

    if not api_key:
        return set()

    headers = {

        "Authorization":
            f"Bearer {api_key}",

        "Content-Type":
            "application/json"
    }

    payload = {

        "query":
            "今日A股强势股票，关注涨停、龙虎榜、短线强势股票"
    }

    try:

        r = session.post(
            MX_URL,
            headers=headers,
            json=payload,
            timeout=6
        )

        if r.status_code != 200:
            return set()

        data = r.json()

    except Exception:
        return set()

    result = set()

    def walk(obj):

        if isinstance(obj, dict):

            for k, v in obj.items():

                if str(k).lower() in (
                    "code",
                    "stock_code",
                    "security_code",
                    "symbol"
                ):

                    code = clean_code(v)

                    if len(code) == 6:
                        result.add(code)

                walk(v)

        elif isinstance(obj, list):

            for x in obj:
                walk(x)

        elif isinstance(obj, str):

            for code in re.findall(
                r"\b[036]\d{5}\b",
                obj
            ):
                result.add(code)

    walk(data)

    return result


# =========================================================
# 评分模型
#
# 注意：
# 这三个不是硬过滤条件。
# 是评分项。
#
# ① 近3日涨停
# ② 龙虎榜
# ③ 市值≤300亿
#
# 即使只满足其中1项，也可以进入排名。
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
    # ① 近3日涨停
    # -----------------------------------------------------

    if zt_count >= 3:

        score += 25

        reasons.append(
            "3日3次涨停"
        )

    elif zt_count == 2:

        score += 20

        reasons.append(
            "3日2次涨停"
        )

    elif zt_count == 1:

        score += 15

        reasons.append(
            "3日内涨停"
        )

    # -----------------------------------------------------
    # ② 龙虎榜
    # -----------------------------------------------------

    if lhb_info:

        score += 15

        reasons.append(
            "上过龙虎榜"
        )

        net = safe_float(
            lhb_info.get("net")
        )

        if net > 5000:

            score += 10

            reasons.append(
                "龙虎榜大额净买"
            )

        elif net > 0:

            score += 6

            reasons.append(
                "龙虎榜净买"
            )

        elif net < -5000:

            score -= 4

            reasons.append(
                "龙虎榜净卖"
            )

    # -----------------------------------------------------
    # ③ 市值
    # -----------------------------------------------------

    market_cap = safe_float(
        quote.get("market_cap")
    )

    if market_cap > 0:

        if market_cap <= 50:

            score += 15

            reasons.append(
                "市值≤50亿"
            )

        elif market_cap <= 100:

            score += 12

            reasons.append(
                "市值≤100亿"
            )

        elif market_cap <= 200:

            score += 8

            reasons.append(
                "市值≤200亿"
            )

        elif market_cap <= 300:

            score += 5

            reasons.append(
                "市值≤300亿"
            )

    # -----------------------------------------------------
    # 涨幅
    # -----------------------------------------------------

    pct = safe_float(
        quote.get("pct")
    )

    if pct >= 9.5:

        score += 15

        reasons.append(
            "接近涨停"
        )

    elif pct >= 7:

        score += 12

        reasons.append(
            "强势上涨"
        )

    elif pct >= 5:

        score += 8

        reasons.append(
            "涨幅>5%"
        )

    elif pct >= 3:

        score += 4

    elif pct < -3:

        score -= 5

    # -----------------------------------------------------
    # 换手
    # -----------------------------------------------------

    turnover = safe_float(
        quote.get("turnover")
    )

    if turnover >= 29.25:

        score += 15

        reasons.append(
            "高换手29.25%+"
        )

    elif turnover >= 20:

        score += 11

        reasons.append(
            "换手较高"
        )

    elif turnover >= 10:

        score += 7

        reasons.append(
            "换手活跃"
        )

    elif turnover >= 5:

        score += 3

    # -----------------------------------------------------
    # 盘口
    # -----------------------------------------------------

    if order_book:

        buy = safe_float(
            order_book.get("buy")
        )

        sell = safe_float(
            order_book.get("sell")
        )

        if sell > buy and sell > 0:

            score += 5

            reasons.append(
                "委卖>委买"
            )

        elif buy > sell and buy > 0:

            score += 3

            reasons.append(
                "委买较强"
            )

    # -----------------------------------------------------
    # 妙想
    # -----------------------------------------------------

    if mx:

        score += 5

        reasons.append(
            "妙想强势"
        )

    score = max(
        0,
        min(100, score)
    )

    return {

        "score":
            round(score, 1),

        "zt_count":
            zt_count,

        "lhb":
            bool(lhb_info),

        "market_cap":
            market_cap,

        "pct":
            pct,

        "turnover":
            turnover,

        "lhb_net":
            round(
                safe_float(
                    lhb_info.get("net")
                    if lhb_info
                    else 0
                ),
                2
            ),

        "reasons":
            reasons
    }


# =========================================================
# 单只股票分析
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

    name = (
        quote.get("name")
        or code
    )

    if is_st(name):
        return None

    # 历史日期用历史K线修正价格和涨幅
    if (
        target_date != TODAY
        and history_map
    ):

        rows = history_map.get(
            code,
            []
        )

        target_row = None
        previous_close = None

        for row in rows:

            if row["date"] == target_date:
                target_row = row

            if row["date"] < target_date:
                previous_close = row["close"]

        if (
            target_row
            and previous_close
            and previous_close > 0
        ):

            quote = dict(quote)

            quote["price"] = (
                target_row["close"]
            )

            quote["pct"] = (
                target_row["close"]
                / previous_close
                - 1
            ) * 100

    zt_count = zt_counts.get(
        code,
        0
    )

    lhb_info = lhb_map.get(
        code
    )

    scoring = calculate_score(
        code,
        quote,
        zt_count,
        lhb_info
    )

    return {

        "code":
            code,

        "name":
            name,

        "price":
            round(
                safe_float(
                    quote.get("price")
                ),
                2
            ),

        "pct":
            round(
                safe_float(
                    quote.get("pct")
                ),
                2
            ),

        "turnover":
            round(
                safe_float(
                    quote.get("turnover")
                ),
                2
            ),

        "market_cap":
            round(
                safe_float(
                    quote.get("market_cap")
                ),
                2
            ),

        "zt_count":
            zt_count,

        "lhb":
            bool(lhb_info),

        "lhb_net":
            scoring["lhb_net"],

        "score":
            scoring["score"],

        "reasons":
            scoring["reasons"],

        "order_book":
            None
    }


# =========================================================
# 核心扫描
# =========================================================

def build_scan(
    target_date=None
):

    requested_date = normalize_date(
        target_date or TODAY
    )

    target_date = get_actual_target_date(
        requested_date
    )

    trading_dates = get_trading_dates(
        target_date,
        10
    )

    if not trading_dates:
        trading_dates = [
            target_date
        ]

    # 最近3个交易日
    last3 = trading_dates[-3:]

    # -----------------------------------------------------
    # 涨停
    # -----------------------------------------------------

    zt_codes, zt_counts = get_zt_info(
        last3
    )

    # -----------------------------------------------------
    # 龙虎榜
    # -----------------------------------------------------

    lhb_map = get_lhb(
        codes=None,
        dates=trading_dates
    )

    lhb_codes = set(
        lhb_map.keys()
    )

    # -----------------------------------------------------
    # 关键修改：
    #
    # 全A股作为基础候选池
    #
    # 不再：
    #
    #     涨停 AND 龙虎榜
    #
    # 才能入选。
    #
    # 三项全部变成评分项。
    # -----------------------------------------------------

    all_quotes = get_all_a_stocks()

    # 接口失败时降级
    if not all_quotes:

        all_quotes = get_quotes(
            list(
                zt_codes
                | lhb_codes
            )
        )

    if not all_quotes:

        return {

            "success":
                True,

            "date":
                target_date,

            "requested_date":
                requested_date,

            "updated":
                now_text(),

            "message":
                "行情接口暂时没有返回股票数据，请稍后重新扫描。",

            "top3":
                [],

            "ranking":
                [],

            "stats": {

                "zt_count":
                    len(zt_codes),

                "lhb_count":
                    len(lhb_codes),

                "candidate_count":
                    0,

                "result_count":
                    0
            }
        }

    # -----------------------------------------------------
    # 先选活跃股票
    #
    # 350只普通候选
    # +
    # 所有涨停/龙虎榜股票
    # -----------------------------------------------------

    normal_codes = list(
        all_quotes.keys()
    )

    normal_codes.sort(
        key=lambda c: (
            safe_float(
                all_quotes[c].get("pct")
            ),
            safe_float(
                all_quotes[c].get("turnover")
            )
        ),
        reverse=True
    )

    normal_codes = normal_codes[:350]

    special_codes = [

        c for c in (
            zt_codes
            | lhb_codes
        )

        if c in all_quotes
    ]

    candidate_codes = list(
        dict.fromkeys(
            special_codes
            + normal_codes
        )
    )[:500]

    quotes = {

        code:
            all_quotes[code]

        for code in candidate_codes

        if code in all_quotes
    }

    # -----------------------------------------------------
    # 历史日期K线
    # -----------------------------------------------------

    history_map = {}

    if target_date != TODAY:

        hist_codes = candidate_codes[:120]

        def load_history(code):

            return (
                code,
                get_kline(
                    code,
                    120,
                    target_date
                )
            )

        with ThreadPoolExecutor(
            max_workers=8
        ) as executor:

            futures = [

                executor.submit(
                    load_history,
                    code
                )

                for code in hist_codes
            ]

            for future in as_completed(
                futures
            ):

                try:

                    code, rows = (
                        future.result()
                    )

                    if rows:
                        history_map[code] = rows

                except Exception:
                    pass

    # -----------------------------------------------------
    # 评分
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
    # 妙想
    # -----------------------------------------------------

    mx_codes = set()

    if target_date == TODAY:

        mx_codes = get_mx_candidates()

    for item in results:

        if item["code"] in mx_codes:

            item["score"] = min(
                100,
                item["score"] + 5
            )

            item["reasons"].append(
                "妙想强势"
            )

    # -----------------------------------------------------
    # 第一次排序
    # -----------------------------------------------------

    results.sort(
        key=lambda x: (
            x["score"],
            x["pct"],
            x["zt_count"],
            x["turnover"]
        ),
        reverse=True
    )

    # -----------------------------------------------------
    # 当前日期才获取盘口
    #
    # 只取前15只
    # 防止Render超时
    # -----------------------------------------------------

    if (
        target_date == TODAY
        and results
    ):

        top_for_order = results[:15]

        def load_order(item):

            return (
                item["code"],
                get_order_book(
                    item["code"]
                )
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

            for future in as_completed(
                futures
            ):

                try:

                    code, order = (
                        future.result()
                    )

                    for item in results:

                        if item["code"] == code:

                            item["order_book"] = order

                            buy = safe_float(
                                order.get("buy")
                            )

                            sell = safe_float(
                                order.get("sell")
                            )

                            if (
                                sell > buy
                                and sell > 0
                            ):

                                item["score"] = min(
                                    100,
                                    item["score"] + 5
                                )

                                if (
                                    "委卖>委买"
                                    not in item["reasons"]
                                ):

                                    item["reasons"].append(
                                        "委卖>委买"
                                    )

                            elif (
                                buy > sell
                                and buy > 0
                            ):

                                item["score"] = min(
                                    100,
                                    item["score"] + 3
                                )

                                if (
                                    "委买较强"
                                    not in item["reasons"]
                                ):

                                    item["reasons"].append(
                                        "委买较强"
                                    )

                            break

                except Exception:
                    pass

        results.sort(
            key=lambda x: (
                x["score"],
                x["pct"],
                x["zt_count"],
                x["turnover"]
            ),
            reverse=True
        )

    # -----------------------------------------------------
    # TOP3
    # -----------------------------------------------------

    top3 = results[:3]

    ranking = results[:30]

    if requested_date != target_date:

        message = (
            f"{requested_date} 非交易日，"
            f"已自动切换至最近交易日 "
            f"{target_date}；"
            f"三项核心指标采用加权评分，"
            f"不要求全部满足。"
        )

    else:

        message = (
            "三项核心指标采用加权评分，"
            "不要求全部满足；"
            "综合评分越高，妖股潜力越高。"
        )

    return {

        "success":
            True,

        "date":
            target_date,

        "requested_date":
            requested_date,

        "updated":
            now_text(),

        "message":
            message,

        "conditions": [

            "近3个交易日涨停",

            "上过龙虎榜",

            "总市值≤300亿"
        ],

        "top3":
            top3,

        "ranking":
            ranking,

        "stats": {

            "zt_count":
                len(zt_codes),

            "lhb_count":
                len(lhb_codes),

            "candidate_count":
                len(candidate_codes),

            "result_count":
                len(results)
        }
    }


# =========================================================
# API
# =========================================================

@app.get("/api/health")
def health():

    return {

        "success":
            True,

        "status":
            "ok",

        "time":
            now_text()
    }


@app.get("/api/scanner")
def scanner(
    date: str = None
):

    requested_date = normalize_date(
        date or TODAY
    )

    target = get_actual_target_date(
        requested_date
    )

    cache_key = target

    with CACHE_LOCK:

        cached = CACHE.get(
            cache_key
        )

        if (
            cached
            and time.time()
            - cached["time"]
            < 60
        ):

            return cached["data"]

    try:

        result = build_scan(
            target
        )

        with CACHE_LOCK:

            CACHE[cache_key] = {

                "time":
                    time.time(),

                "data":
                    result
            }

        return result

    except Exception as e:

        return JSONResponse(

            status_code=200,

            content={

                "success":
                    False,

                "error":
                    str(e),

                "date":
                    target,

                "requested_date":
                    requested_date,

                "updated":
                    now_text(),

                "top3":
                    [],

                "ranking":
                    []
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

<meta
name="viewport"
content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"
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

    font-size:27px;

    font-weight:800;
}

.status{

    color:#888;

    font-size:12px;
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

    min-width:0;

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

.section-title{

    font-size:18px;

    font-weight:800;

    margin-bottom:10px;
}

.conditions{

    line-height:1.9;

    font-size:14px;
}

.red{

    color:#ff3945;
}

.green{

    color:#19d66b;
}

.yellow{

    color:#ffcc00;
}

.muted{

    color:#888;

    font-size:12px;
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

    font-size:21px;

    font-weight:900;
}

.data{

    display:flex;

    flex-wrap:wrap;

    gap:7px;

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

.loading{

    text-align:center;

    color:#888;

    padding:22px;
}

.empty{

    text-align:center;

    color:#777;

    padding:25px 5px;
}

.error{

    color:#ff4650;

    background:#22090b;

    border-color:#5b1419;
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

<div class="title">
🔥 妖股雷达
</div>

<div
id="status"
class="status"
>
准备就绪
</div>

</div>


<div class="panel">

<div class="controls">

<input
id="date"
type="date"
>

<button onclick="scan()">
开始扫描
</button>

<button
class="secondary"
onclick="today()"
>
今天
</button>

</div>

</div>


<div class="panel">

<div class="section-title">
核心选股指标
</div>

<div class="conditions">

<div>
①
<span class="red">
近3个交易日涨停
</span>
</div>

<div>
②
<span class="red">
上过龙虎榜
</span>
</div>

<div>
③
<span class="green">
总市值≤300亿
</span>
</div>

</div>

<div
class="muted"
style="margin-top:8px;"
>

以上三项全部为
<strong>
加分项
</strong>
，
不要求全部满足。

<br>

同时参考涨幅、换手、盘口等指标综合评分。

</div>

</div>


<div id="message">
</div>


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

数据：东方财富公开行情接口

<br>

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

    return `${y}-${m}-${day}`;
}


function today(){

    dateInput.value =
        todayText();

    scan();
}


function money(v){

    v =
        Number(v || 0);

    return v.toFixed(1)
        + "亿";
}


function pct(v){

    v =
        Number(v || 0);

    const s =
        v >= 0 ? "+" : "";

    return s +
        v.toFixed(2) +
        "%";
}


function card(
    item,
    index
){

    const p =
        Number(
            item.pct || 0
        );

    const pctClass =
        p >= 0
        ? "red"
        : "green";

    const reasons =
        (
            item.reasons || []
        ).join(" · ");

    return `

    <div
        class="card ${
            index < 3
            ? "top"
            : ""
        }"
    >

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
                ${Number(
                    item.score || 0
                ).toFixed(0)}
            </div>

        </div>


        <div class="data">

            <span class="tag">
                评分
                <b class="yellow">
                    ${Number(
                        item.score || 0
                    ).toFixed(0)}
                </b>
            </span>


            <span class="tag">
                涨幅
                <b class="${pctClass}">
                    ${pct(p)}
                </b>
            </span>


            <span class="tag">
                3日涨停
                ${item.zt_count || 0}次
            </span>


            <span class="tag">
                换手
                ${Number(
                    item.turnover || 0
                ).toFixed(2)}%
            </span>


            <span class="tag">
                市值
                ${money(
                    item.market_cap
                )}
            </span>


            <span class="tag">
                龙虎榜
                ${item.lhb ? "✓" : "—"}
            </span>

        </div>


        <div class="reason">
            ${reasons || "综合评分"}
        </div>

    </div>

    `;
}


function render(data){

    if(!data){

        topEl.innerHTML =
            '<div class="empty">没有返回数据</div>';

        return;
    }


    if(
        data.success === false
    ){

        messageEl.innerHTML = `

        <div class="panel error">

            加载失败：
            ${data.error || "接口异常"}

        </div>

        `;

        topEl.innerHTML =
            '<div class="empty">暂无数据</div>';

        rankingEl.innerHTML =
            '<div class="empty">暂无数据</div>';

        return;
    }


    messageEl.innerHTML = `

    <div class="panel">

        <div class="muted">

            扫描交易日：
            ${data.date || ""}

            · 更新时间：
            ${data.updated || ""}

        </div>

        <div style="margin-top:7px;">

            ${data.message || ""}

        </div>

    </div>

    `;


    const top =
        data.top3 || [];


    topEl.innerHTML =
        top.length

        ? top.map(
            (x,i) =>
            card(x,i)
        ).join("")

        : `

        <div class="empty">

            当前没有有效候选股

        </div>

        `;


    const ranking =
        data.ranking || [];


    rankingEl.innerHTML =
        ranking.length

        ? ranking.map(
            (x,i) =>
            card(x,i)
        ).join("")

        : `

        <div class="empty">

            暂无候选数据

        </div>

        `;


    const stats =
        data.stats || {};


    statusEl.innerText =
        `候选 ${
            stats.result_count || 0
        } 只`;

}


async function scan(){

    const date =
        dateInput.value
        || todayText();


    statusEl.innerText =
        "数据读取中...";


    messageEl.innerHTML =
        "";


    topEl.innerHTML = `

        <div class="loading">

            正在扫描，请稍候...

        </div>

    `;


    rankingEl.innerHTML = `

        <div class="loading">

            正在计算...

        </div>

    `;


    try{

        const controller =
            new AbortController();


        const timer =
            setTimeout(
                () =>
                controller.abort(),
                30000
            );


        const res =
            await fetch(

                `/api/scanner?date=${
                    encodeURIComponent(date)
                }`,

                {
                    cache:
                        "no-store",

                    signal:
                        controller.signal
                }

            );


        clearTimeout(timer);


        if(!res.ok){

            throw new Error(
                "服务器 HTTP "
                + res.status
            );

        }


        const data =
            await res.json();


        render(data);


    }catch(e){

        statusEl.innerText =
            "数据读取失败";


        messageEl.innerHTML = `

        <div class="panel error">

            加载失败：
            ${
                e.message
                || "Load failed"
            }

        </div>

        `;


        topEl.innerHTML = `

        <div class="empty">

            请稍后重新扫描

        </div>

        `;


        rankingEl.innerHTML = `

        <div class="empty">

            暂无数据

        </div>

        `;

    }

}


dateInput.value =
    todayText();

scan();

</script>

</body>

</html>
"""


@app.get(
    "/",
    response_class=HTMLResponse
)
def home():

    return HTML


# =========================================================
# 本地运行
# =========================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.getenv(
            "PORT",
            "8000"
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
