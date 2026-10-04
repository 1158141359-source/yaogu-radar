import os
import re
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

app = FastAPI(title="妖股雷达")

TZ = ZoneInfo("Asia/Shanghai")
TODAY = datetime.now(TZ).strftime("%Y%m%d")

TIMEOUT = 12

MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 Mobile/15E148"
    )
})

CACHE: Dict[str, Dict[str, Any]] = {}


# ============================================================
# 基础工具
# ============================================================

def get_json(url: str, params=None):
    try:
        r = session.get(
            url,
            params=params,
            timeout=TIMEOUT
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def safe_float(value, default=0.0):
    if value is None:
        return default

    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()

    s = (
        s.replace(",", "")
        .replace("%", "")
        .replace("亿", "")
        .replace("万", "")
    )

    try:
        return float(s)
    except Exception:
        return default


def clean_code(value):
    m = re.search(
        r"(?<!\d)(?:0|3|6)\d{5}(?!\d)",
        str(value or "")
    )
    return m.group(0) if m else ""


def normalize_date(value):
    if not value:
        return TODAY

    s = str(value).strip()
    s = s.replace("-", "").replace("/", "")

    if not re.fullmatch(r"\d{8}", s):
        return TODAY

    try:
        datetime.strptime(s, "%Y%m%d")
        return s
    except Exception:
        return TODAY


def secid(code):
    if code.startswith(("5", "6", "68", "9")):
        return "1." + code
    return "0." + code


def is_st(name):
    name = str(name or "").upper()
    return "ST" in name or "*ST" in name


# ============================================================
# 交易日
# ============================================================

def get_trading_dates(end_date: str, count: int = 10):
    end_date = normalize_date(end_date)

    params = {
        "secid": "1.000001",
        "klt": "101",
        "fqt": "0",
        "beg": "19900101",
        "end": end_date,
        "lmt": "1000",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2":
            "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    }

    data = get_json(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params
    )

    rows = (
        ((data or {}).get("data") or {})
        .get("klines") or []
    )

    dates = []

    for row in rows:
        parts = str(row).split(",")

        if not parts:
            continue

        d = parts[0].replace("-", "")

        if (
            re.fullmatch(r"\d{8}", d)
            and d <= end_date
        ):
            dates.append(d)

    dates = sorted(
        set(dates),
        reverse=True
    )

    if len(dates) >= count:
        return dates[:count]

    # API失败时，仅用工作日兜底
    result = list(dates)

    try:
        dt = datetime.strptime(
            end_date,
            "%Y%m%d"
        )
    except Exception:
        dt = datetime.now(TZ)

    while len(result) < count:
        d = dt.strftime("%Y%m%d")

        if (
            dt.weekday() < 5
            and d not in result
        ):
            result.append(d)

        dt -= timedelta(days=1)

    return sorted(
        result,
        reverse=True
    )[:count]


def get_actual_target_date(requested_date: str):
    """
    如果用户选择周末/节假日，
    自动回退到最近一个实际交易日。
    """
    requested_date = normalize_date(requested_date)

    dates = get_trading_dates(
        requested_date,
        10
    )

    if not dates:
        return requested_date, []

    valid = [
        d for d in dates
        if d <= requested_date
    ]

    if valid:
        return valid[0], dates

    return dates[0], dates


# ============================================================
# 历史K线
# ============================================================

def get_kline(
    code: str,
    end_date: str,
    limit: int = 120
):
    params = {
        "secid": secid(code),
        "klt": "101",
        "fqt": "0",
        "beg": "19900101",
        "end": normalize_date(end_date),
        "lmt": str(limit),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2":
            "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    }

    data = get_json(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params
    )

    rows = (
        ((data or {}).get("data") or {})
        .get("klines") or []
    )

    result = []

    for row in rows:
        parts = str(row).split(",")

        if len(parts) < 11:
            continue

        result.append({
            "date": parts[0].replace("-", ""),
            "open": safe_float(parts[1]),
            "close": safe_float(parts[2]),
            "high": safe_float(parts[3]),
            "low": safe_float(parts[4]),
            "volume": safe_float(parts[5]),
            "amount": safe_float(parts[6]),
            "amplitude": safe_float(parts[7]),
            "pct": safe_float(parts[8]),
            "change": safe_float(parts[9]),
            "turnover": safe_float(parts[10])
        })

    return result


# ============================================================
# 实时行情
# ============================================================

def get_quotes(codes: List[str]):
    codes = list(
        dict.fromkeys(
            clean_code(x)
            for x in codes
            if clean_code(x)
        )
    )

    result = {}

    if not codes:
        return result

    for start in range(0, len(codes), 80):
        batch = codes[start:start + 80]

        fs = ",".join(
            (
                "m:1~" + code
                if code.startswith(("6", "68"))
                else "m:0~" + code
            )
            for code in batch
        )

        params = {
            "pn": "1",
            "pz": str(len(batch)),
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": fs,
            "fields": "f2,f3,f8,f12,f14,f20,f21"
        }

        data = get_json(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            params
        )

        diff = (
            ((data or {}).get("data") or {})
            .get("diff") or []
        )

        if isinstance(diff, dict):
            diff = list(diff.values())

        for item in diff:
            code = clean_code(item.get("f12"))

            if not code:
                continue

            result[code] = {
                "price": safe_float(item.get("f2")),
                "pct": safe_float(item.get("f3")),
                "turnover": safe_float(item.get("f8")),
                "name": str(item.get("f14") or ""),
                "market_cap": safe_float(item.get("f20")) / 1e8,
                "float_cap": safe_float(item.get("f21")) / 1e8
            }

    return result


# ============================================================
# 东方财富涨停池
# ============================================================

def get_zt_pool(date):
    params = {
        "ut":
            "7eea3edcaed734bea9cbfc24409ed989",
        "dpt":
            "wz.ztzt",
        "Pageindex":
            "0",
        "pagesize":
            "6000",
        "sort":
            "fbt:asc",
        "date":
            date
    }

    data = get_json(
        "https://push2ex.eastmoney.com/getTopicZTPool",
        params
    )

    pool = (
        ((data or {}).get("data") or {})
        .get("pool") or []
    )

    return pool if isinstance(pool, list) else []


def get_limit_up_counts(
    codes,
    trading_dates
):
    wanted = set(codes)

    counts = {
        code: 0
        for code in codes
    }

    for date in trading_dates[:3]:
        pool = get_zt_pool(date)

        for item in pool:
            code = clean_code(
                item.get("c")
                or item.get("code")
            )

            if code in wanted:
                counts[code] += 1

    return counts


# ============================================================
# 龙虎榜
# ============================================================

def get_lhb(codes, trading_dates):
    result = {
        code: {
            "day_net": 0.0,
            "net10": 0.0,
            "days": 0,
            "on_board": False
        }
        for code in codes
    }

    if not codes or not trading_dates:
        return result

    wanted = set(codes)

    begin = trading_dates[-1]
    end = trading_dates[0]

    params = {
        "reportName":
            "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns":
            "ALL",
        "quoteColumns":
            "",
        "filter":
            f"(TRADE_DATE>='{begin}')"
            f"(TRADE_DATE<='{end}')",
        "pageNumber":
            "1",
        "pageSize":
            "5000",
        "sortTypes":
            "-1",
        "sortColumns":
            "TRADE_DATE",
        "source":
            "DataCenter",
        "client":
            "web"
    }

    data = get_json(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params
    )

    rows = (
        ((data or {}).get("result") or {})
        .get("data") or []
    )

    seen = {
        code: set()
        for code in codes
    }

    for item in rows:
        code = clean_code(
            item.get("SECURITY_CODE")
        )

        if code not in wanted:
            continue

        trade_date = str(
            item.get("TRADE_DATE") or ""
        )[:10].replace("-", "")

        net = (
            safe_float(
                item.get("BILLBOARD_NET_AMT")
            ) / 1e8
        )

        result[code]["net10"] += net

        if trade_date == end:
            result[code]["day_net"] += net

        if trade_date:
            seen[code].add(trade_date)

    for code in codes:
        result[code]["days"] = len(
            seen[code]
        )

        result[code]["on_board"] = (
            result[code]["days"] > 0
        )

    return result


# ============================================================
# 当前五档盘口
# ============================================================

def get_order_book(code):
    params = {
        "secid": secid(code),
        "fields":
            "f2,"
            "f19,f20,"
            "f17,f18,"
            "f15,f16,"
            "f13,f14,"
            "f11,f12,"
            "f39,f40,"
            "f37,f38,"
            "f35,f36,"
            "f33,f34,"
            "f31,f32"
    }

    data = get_json(
        "https://push2.eastmoney.com/api/qt/stock/get",
        params
    )

    item = (
        (data or {}).get("data")
        or {}
    )

    if not item:
        return {
            "buy": 0.0,
            "sell": 0.0,
            "imbalance": 0.0
        }

    buy_pairs = [
        ("f19", "f20"),
        ("f17", "f18"),
        ("f15", "f16"),
        ("f13", "f14"),
        ("f11", "f12")
    ]

    sell_pairs = [
        ("f39", "f40"),
        ("f37", "f38"),
        ("f35", "f36"),
        ("f33", "f34"),
        ("f31", "f32")
    ]

    buy = sum(
        safe_float(item.get(price))
        * safe_float(item.get(volume))
        * 100
        for price, volume in buy_pairs
    )

    sell = sum(
        safe_float(item.get(price))
        * safe_float(item.get(volume))
        * 100
        for price, volume in sell_pairs
    )

    total = buy + sell

    imbalance = (
        (buy - sell) / total
        if total > 0
        else 0
    )

    return {
        "buy": buy / 1e8,
        "sell": sell / 1e8,
        "imbalance": imbalance
    }


# ============================================================
# 妙想候选
# ============================================================

MX_QUERY = """
筛选中国A股强势股票。
要求：
1. 近3个交易日出现过涨停；
2. 近期上过龙虎榜；
3. 总市值不超过300亿元；
4. 排除ST、*ST；
5. 尽量寻找涨幅、换手率、资金强度较高的股票。

尽量返回至少30只股票。
只返回股票代码和名称，不要解释。
"""


def get_mx_candidates():
    api_key = os.getenv(
        "MX_APIKEY",
        ""
    ).strip()

    if not api_key:
        return []

    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json"
    }

    payload = {
        "query": MX_QUERY
    }

    try:
        response = session.post(
            MX_URL,
            headers=headers,
            json=payload,
            timeout=20
        )

        if response.status_code >= 400:
            return []

        data = response.json()

        text = json.dumps(
            data,
            ensure_ascii=False
        )

        candidates = []

        if isinstance(data, dict):
            for key in (
                "data",
                "result",
                "items",
                "stocks",
                "rows"
            ):
                value = data.get(key)

                if isinstance(value, list):
                    candidates = value
                    break

        elif isinstance(data, list):
            candidates = data

        result = []

        for item in candidates:
            if not isinstance(item, dict):
                continue

            code = clean_code(
                item.get("code")
                or item.get("stock_code")
                or item.get("SECURITY_CODE")
            )

            name = (
                item.get("name")
                or item.get("stock_name")
                or item.get("SECURITY_NAME_ABBR")
                or ""
            )

            if code and not is_st(name):
                result.append({
                    "code": code,
                    "name": str(name)
                })

        if not result:
            codes = re.findall(
                r"(?<!\d)(?:0|3|6)\d{5}(?!\d)",
                text
            )

            for code in codes:
                if not any(
                    x["code"] == code
                    for x in result
                ):
                    result.append({
                        "code": code,
                        "name": ""
                    })

        return result

    except Exception:
        return []


# ============================================================
# 评分
# ============================================================

def calculate_score(
    target_date,
    quote,
    history,
    lhb,
    zt_count,
    order_book,
    mx_match
):
    if target_date == TODAY:
        pct = safe_float(
            quote.get("pct")
        )
        turnover = safe_float(
            quote.get("turnover")
        )
        price = safe_float(
            quote.get("price")
        )
    else:
        pct = safe_float(
            history.get("pct")
        )
        turnover = safe_float(
            history.get("turnover")
        )
        price = safe_float(
            history.get("close")
        )

    current_cap = safe_float(
        quote.get("market_cap")
    )

    market_cap_estimated = False

    if target_date == TODAY:
        market_cap = current_cap
    else:
        current_price = safe_float(
            quote.get("price")
        )

        if (
            current_cap > 0
            and current_price > 0
            and price > 0
        ):
            market_cap = (
                current_cap
                * price
                / current_price
            )
            market_cap_estimated = True
        else:
            market_cap = 0

    # 三个硬条件
    hard_conditions = []

    if zt_count >= 1:
        hard_conditions.append("3日内涨停")

    if lhb.get("on_board"):
        hard_conditions.append("龙虎榜")

    if (
        market_cap > 0
        and market_cap <= 300
    ):
        hard_conditions.append("市值≤300亿")

    score = 0

    # 1. 近3日涨停 20分
    if zt_count >= 3:
        score += 20
    elif zt_count == 2:
        score += 14
    elif zt_count == 1:
        score += 7

    # 2. 龙虎榜 15分
    if lhb.get("on_board"):
        days = int(
            lhb.get("days", 0)
        )

        if days >= 3:
            score += 15
        elif days == 2:
            score += 12
        else:
            score += 8

    # 3. 市值 10分
    if (
        market_cap > 0
        and market_cap <= 300
    ):
        if market_cap <= 50:
            score += 10
        elif market_cap <= 100:
            score += 9
        elif market_cap <= 200:
            score += 8
        else:
            score += 6

    # 4. 涨幅 15分
    if pct >= 9:
        score += 15
    elif pct >= 7:
        score += 13
    elif pct >= 5:
        score += 10
    elif pct >= 3:
        score += 7
    elif pct > 0:
        score += 3

    # 5. 换手率 15分
    if turnover >= 29.25:
        score += 15
    elif turnover >= 20:
        score += 13
    elif turnover >= 15:
        score += 11
    elif turnover >= 10:
        score += 8
    elif turnover >= 5:
        score += 5

    # 6. 龙虎榜资金 10分
    net10 = safe_float(
        lhb.get("net10")
    )

    if net10 >= 5:
        score += 10
    elif net10 >= 2:
        score += 8
    elif net10 > 0:
        score += 6
    elif net10 > -2:
        score += 3

    # 7. 盘口 10分
    # 历史日期不伪造历史盘口
    order_score = 0

    if (
        target_date == TODAY
        and order_book
    ):
        imbalance = safe_float(
            order_book.get("imbalance")
        )

        if imbalance >= 0.30:
            order_score = 10
        elif imbalance >= 0.15:
            order_score = 8
        elif imbalance > 0:
            order_score = 5
        elif imbalance > -0.15:
            order_score = 2
        else:
            order_score = 0

    score += order_score

    # 8. 妙想增强 5分
    if mx_match:
        score += 5

    score = max(
        0,
        min(100, int(round(score)))
    )

    if (
        len(hard_conditions) == 3
        and score >= 80
    ):
        tier = "S"
    elif score >= 65:
        tier = "A"
    elif score >= 50:
        tier = "B"
    else:
        tier = "C"

    return {
        "score": score,
        "tier": tier,
        "hard": hard_conditions,
        "pct": pct,
        "turnover": turnover,
        "price": price,
        "market_cap": market_cap,
        "market_cap_estimated":
            market_cap_estimated,
        "order_score": order_score
    }


# ============================================================
# 扫描主程序
# ============================================================

def build_scan(target_date):
    requested_date = normalize_date(
        target_date
    )

    # 非交易日自动回退到最近交易日
    actual_date, trading_dates = (
        get_actual_target_date(
            requested_date
        )
    )

    if not trading_dates:
        return {
            "requested_date": requested_date,
            "date": actual_date,
            "count": 0,
            "hard_count": 0,
            "top3": [],
            "ranking": [],
            "source": "无数据",
            "note": "没有获取到交易日数据"
        }

    # 最近3个交易日
    last3 = trading_dates[:3]

    # --------------------------------------------------------
    # 涨停池
    # --------------------------------------------------------

    zt_codes = set()

    for date in last3:
        pool = get_zt_pool(date)

        for item in pool:
            code = clean_code(
                item.get("c")
                or item.get("code")
            )

            if code:
                zt_codes.add(code)

    # --------------------------------------------------------
    # 龙虎榜代码
    # --------------------------------------------------------

    begin = trading_dates[-1]
    end = trading_dates[0]

    lhb_params = {
        "reportName":
            "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns":
            "SECURITY_CODE,SECURITY_NAME_ABBR,"
            "BILLBOARD_NET_AMT,TRADE_DATE",
        "quoteColumns":
            "",
        "filter":
            f"(TRADE_DATE>='{begin}')"
            f"(TRADE_DATE<='{end}')",
        "pageNumber":
            "1",
        "pageSize":
            "5000",
        "sortTypes":
            "-1",
        "sortColumns":
            "TRADE_DATE",
        "source":
            "DataCenter",
        "client":
            "web"
    }

    lhb_data = get_json(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        lhb_params
    )

    lhb_rows = (
        ((lhb_data or {}).get("result") or {})
        .get("data") or []
    )

    lhb_codes = set()

    for item in lhb_rows:
        code = clean_code(
            item.get("SECURITY_CODE")
        )

        if code:
            lhb_codes.add(code)

    # --------------------------------------------------------
    # 妙想
    # --------------------------------------------------------

    mx_list = []

    if actual_date == TODAY:
        mx_list = get_mx_candidates()

    mx_codes = {
        item["code"]
        for item in mx_list
        if item.get("code")
    }

    mx_names = {
        item["code"]:
            item.get("name", "")
        for item in mx_list
    }

    # --------------------------------------------------------
    # 候选池
    # --------------------------------------------------------

    candidate_codes = sorted(
        zt_codes
        | lhb_codes
        | mx_codes
    )

    if not candidate_codes:
        return {
            "requested_date": requested_date,
            "date": actual_date,
            "trading_dates": trading_dates,
            "count": 0,
            "hard_count": 0,
            "top3": [],
            "ranking": [],
            "source": "东方财富",
            "note": "当前日期没有获取到候选股票"
        }

    # --------------------------------------------------------
    # 行情
    # --------------------------------------------------------

    quotes = get_quotes(
        candidate_codes
    )

    # --------------------------------------------------------
    # 龙虎榜
    # --------------------------------------------------------

    lhb = get_lhb(
        candidate_codes,
        trading_dates
    )

    # --------------------------------------------------------
    # 3日涨停次数
    # --------------------------------------------------------

    zt_counts = get_limit_up_counts(
        candidate_codes,
        last3
    )

    results = []

    # --------------------------------------------------------
    # 逐股
    # --------------------------------------------------------

    for code in candidate_codes:
        quote = quotes.get(
            code,
            {}
        )

        if not quote:
            continue

        name = (
            quote.get("name")
            or mx_names.get(code)
            or code
        )

        if is_st(name):
            continue

        # 历史K线
        history_rows = get_kline(
            code,
            actual_date,
            10
        )

        history = next(
            (
                row
                for row in history_rows
                if row["date"] == actual_date
            ),
            None
        )

        # 历史日期必须有K线
        if (
            actual_date != TODAY
            and not history
        ):
            continue

        # 今日数据
        if not history:
            history = {
                "date": actual_date,
                "close":
                    quote.get("price", 0),
                "pct":
                    quote.get("pct", 0),
                "turnover":
                    quote.get("turnover", 0)
            }

        # 当前盘口
        order_book = None

        if actual_date == TODAY:
            order_book = get_order_book(code)

        lhb_item = lhb.get(
            code,
            {
                "day_net": 0.0,
                "net10": 0.0,
                "days": 0,
                "on_board": False
            }
        )

        scoring = calculate_score(
            actual_date,
            quote,
            history,
            lhb_item,
            zt_counts.get(code, 0),
            order_book,
            code in mx_codes
        )

        result = {
            "code": code,
            "name": name,
            "date": actual_date,

            "price":
                scoring["price"],

            "pct":
                scoring["pct"],

            "turnover":
                scoring["turnover"],

            "market_cap":
                scoring["market_cap"],

            "market_cap_estimated":
                scoring[
                    "market_cap_estimated"
                ],

            "zt3":
                zt_counts.get(code, 0),

            # 前端兼容字段
            "zt_count":
                zt_counts.get(code, 0),

            "lhb_on":
                lhb_item.get(
                    "on_board",
                    False
                ),

            "lhb_days":
                lhb_item.get(
                    "days",
                    0
                ),

            "lhb_day_net":
                lhb_item.get(
                    "day_net",
                    0
                ),

            "lhb_net":
                lhb_item.get(
                    "day_net",
                    0
                ),

            "lhb_net10":
                lhb_item.get(
                    "net10",
                    0
                ),

            "order_buy":
                (
                    order_book.get("buy")
                    if order_book
                    else None
                ),

            "order_sell":
                (
                    order_book.get("sell")
                    if order_book
                    else None
                ),

            "order_imbalance":
                (
                    order_book.get("imbalance")
                    if order_book
                    else None
                ),

            "order_score":
                scoring["order_score"],

            "score":
                scoring["score"],

            "tier":
                scoring["tier"],

            "hard":
                scoring["hard"],

            "hard_ok":
                len(scoring["hard"]) == 3,

            "mx_match":
                code in mx_codes
        }

        results.append(result)

    # --------------------------------------------------------
    # 排序
    # --------------------------------------------------------

    results.sort(
        key=lambda x: (
            x["score"],
            len(x["hard"]),
            x["zt3"],
            x["lhb_net10"],
            x["pct"]
        ),
        reverse=True
    )

    # --------------------------------------------------------
    # 三个硬条件
    # --------------------------------------------------------

    hard_results = [
        x
        for x in results
        if len(x["hard"]) == 3
    ]

    # TOP3
    top3 = hard_results[:3]

    return {
        "requested_date":
            requested_date,

        "date":
            actual_date,

        "trading_dates":
            trading_dates,

        "count":
            len(results),

        "hard_count":
            len(hard_results),

        "top3":
            top3,

        "ranking":
            results[:50],

        "source":
            (
                "东方财富公开行情"
                + (
                    " + 妙想增强"
                    if mx_codes
                    else ""
                )
            ),

        "note":
            (
                "历史日期：市值为估算值；"
                "历史日期不伪造集合竞价。"
                "今日盘口为当前五档委买委卖。"
            )
    }


# ============================================================
# API
# ============================================================

@app.get("/api/scanner")
def scanner(
    date: str = Query(default="")
):
    try:
        target_date = normalize_date(date)

        if target_date > TODAY:
            target_date = TODAY

        now = time.time()

        cached = CACHE.get(target_date)

        if (
            cached
            and now - cached["time"] < 60
        ):
            return cached["data"]

        data = build_scan(target_date)

        CACHE[target_date] = {
            "time": now,
            "data": data
        }

        # 最多保留20个日期
        if len(CACHE) > 20:
            oldest = sorted(
                CACHE.items(),
                key=lambda x:
                    x[1]["time"]
            )[0][0]

            CACHE.pop(
                oldest,
                None
            )

        return data

    except Exception as e:
        return {
            "error": str(e),
            "date": date
        }


# ============================================================
# 网页
# ============================================================

HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">

<meta name="viewport"
content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">

<title>妖股雷达</title>

<style>

*{
    box-sizing:border-box;
}

body{
    margin:0;
    background:#080a0f;
    color:#e8edf5;
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
    max-width:1180px;
    margin:auto;
    padding:14px;
}

.header{
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:10px;
    margin-bottom:14px;
}

.logo{
    font-size:25px;
    font-weight:800;
    color:#fff;
}

.logo span{
    color:#ff3b30;
}

.status{
    font-size:12px;
    color:#8d98aa;
    text-align:right;
}

.toolbar{
    background:#10141c;
    border:1px solid #202735;
    border-radius:12px;
    padding:12px;
    display:flex;
    flex-wrap:wrap;
    gap:9px;
    align-items:center;
    margin-bottom:12px;
}

.toolbar label{
    color:#9da7b7;
    font-size:13px;
}

input[type="date"]{
    background:#080a0f;
    color:#fff;
    border:1px solid #303949;
    border-radius:8px;
    padding:8px 10px;
    font-size:14px;
}

button{
    border:0;
    border-radius:8px;
    padding:9px 15px;
    background:#e53935;
    color:white;
    font-weight:700;
    cursor:pointer;
}

.conditions{
    background:#11161f;
    border:1px solid #202735;
    border-radius:10px;
    padding:11px 13px;
    margin-bottom:12px;
    color:#aeb8c7;
    font-size:13px;
    line-height:1.7;
}

.conditions b{
    color:#fff;
}

.notice{
    background:#11161f;
    border-left:3px solid #ff453a;
    border-radius:7px;
    padding:9px 11px;
    color:#8f9aac;
    font-size:12px;
    margin-bottom:12px;
}

.loading{
    text-align:center;
    padding:35px 10px;
    color:#8d98aa;
}

.error{
    background:#321518;
    border:1px solid #6e252b;
    color:#ff8d8d;
    padding:13px;
    border-radius:9px;
    margin-bottom:12px;
}

.section-title{
    font-size:18px;
    font-weight:800;
    margin:16px 0 10px;
}

.cards{
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:10px;
}

.card{
    background:#11161f;
    border:1px solid #252d3b;
    border-radius:13px;
    padding:14px;
    position:relative;
    overflow:hidden;
}

.card.top1{
    border-color:#ff453a;
}

.rank{
    position:absolute;
    top:10px;
    right:10px;
    font-size:12px;
    color:#7f8999;
}

.name{
    font-size:18px;
    font-weight:800;
    margin-bottom:3px;
}

.code{
    color:#778294;
    font-size:12px;
    margin-bottom:12px;
}

.score{
    font-size:30px;
    line-height:1;
    font-weight:900;
    color:#ff453a;
}

.score-label{
    font-size:11px;
    color:#7f8999;
    margin-top:3px;
}

.up{
    color:#ff453a !important;
}

.down{
    color:#22c55e !important;
}

.neutral{
    color:#c7ced9 !important;
}

.price-row{
    display:flex;
    justify-content:space-between;
    margin-top:14px;
    font-size:15px;
}

.metrics{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:7px;
    margin-top:13px;
}

.metric{
    background:#0b0e14;
    border-radius:7px;
    padding:8px;
}

.metric-title{
    color:#697487;
    font-size:11px;
}

.metric-value{
    margin-top:3px;
    font-size:13px;
    font-weight:700;
}

.badges{
    display:flex;
    flex-wrap:wrap;
    gap:5px;
    margin-top:12px;
}

.badge{
    background:#202735;
    border-radius:5px;
    padding:4px 7px;
    color:#aeb8c7;
    font-size:11px;
}

.badge.red{
    color:#ff716b;
    background:#32171a;
}

.badge.green{
    color:#54d98a;
    background:#10271b;
}

.table-wrap{
    overflow-x:auto;
    background:#11161f;
    border:1px solid #202735;
    border-radius:12px;
}

table{
    width:100%;
    border-collapse:collapse;
    min-width:850px;
}

th{
    background:#161c26;
    color:#7f8999;
    font-size:12px;
    text-align:left;
    padding:11px 9px;
    white-space:nowrap;
}

td{
    border-top:1px solid #202735;
    padding:10px 9px;
    font-size:13px;
    white-space:nowrap;
}

td:first-child{
    font-weight:800;
}

footer{
    color:#586273;
    text-align:center;
    padding:25px 5px 35px;
    font-size:11px;
}

@media(max-width:760px){

    .container{
        padding:10px;
    }

    .header{
        align-items:flex-start;
    }

    .logo{
        font-size:22px;
    }

    .cards{
        grid-template-columns:1fr;
    }

    .toolbar{
        display:grid;
        grid-template-columns:auto 1fr;
    }

    .toolbar button{
        grid-column:1 / -1;
    }
}

</style>
</head>

<body>

<div class="container">

<div class="header">

    <div class="logo">
        妖股<span>雷达</span>
    </div>

    <div class="status" id="status">
        正在加载...
    </div>

</div>

<div class="toolbar">

    <label>交易日</label>

    <input
        type="date"
        id="date"
    >

    <button onclick="loadData()">
        查询
    </button>

    <button
        onclick="setToday()"
        style="background:#252d3b;"
    >
        今天
    </button>

</div>

<div class="conditions">

    <b>核心硬条件：</b>

    近3个交易日出现涨停
    ＋
    进入龙虎榜
    ＋
    市值 ≤ 300亿

    <br>

    <b>综合评分：</b>

    涨幅、换手率、龙虎榜资金、
    近3日涨停次数、盘口强弱等。

</div>

<div class="notice">

    ⚠️ 历史日期可以回看。
    历史日期没有保存的集合竞价快照，
    不伪造历史竞价数据。
    今日盘口为当前五档委买委卖。

</div>

<div id="error"></div>

<div class="section-title">
    🔥 强势 TOP3
</div>

<div id="cards" class="cards">

    <div class="loading">
        正在读取行情...
    </div>

</div>

<div class="section-title">
    📊 综合评分排行
</div>

<div class="table-wrap">

<table>

<thead>

<tr>
    <th>排名</th>
    <th>股票</th>
    <th>代码</th>
    <th>综合评分</th>
    <th>涨幅</th>
    <th>换手</th>
    <th>市值</th>
    <th>3日涨停</th>
    <th>龙虎榜</th>
    <th>盘口</th>
    <th>评级</th>
</tr>

</thead>

<tbody id="table"></tbody>

</table>

</div>

<footer>
    数据来源：东方财富公开行情接口 + 妙想增强
    <br>
    本工具仅用于信息整理与量化筛选，不构成投资建议
</footer>

</div>


<script>

function todayCN(){

    const d = new Date();

    const parts =
        new Intl.DateTimeFormat(
            "en-CA",
            {
                timeZone:"Asia/Shanghai",
                year:"numeric",
                month:"2-digit",
                day:"2-digit"
            }
        ).formatToParts(d);

    let y="",m="",day="";

    for(const p of parts){
        if(p.type==="year") y=p.value;
        if(p.type==="month") m=p.value;
        if(p.type==="day") day=p.value;
    }

    return y+"-"+m+"-"+day;
}


function setToday(){

    document.getElementById("date").value =
        todayCN();

    loadData();
}


function num(v){

    if(
        v === null ||
        v === undefined ||
        v === ""
    ){
        return 0;
    }

    const n = Number(v);

    return Number.isFinite(n) ? n : 0;
}


function pct(v){

    const n = num(v);

    return (
        n >= 0 ? "+" : ""
    ) + n.toFixed(2) + "%";
}


function money(v){

    const n = num(v);

    if(Math.abs(n) >= 1){
        return n.toFixed(2)+"亿";
    }

    return (n*10000).toFixed(0)+"万";
}


function scoreClass(v){

    if(num(v)>=70) return "up";

    if(num(v)>=50) return "neutral";

    return "down";
}


function rating(score){

    score=num(score);

    if(score>=85) return "S+ 强妖";
    if(score>=75) return "S 强势";
    if(score>=65) return "A 强";
    if(score>=55) return "B 观察";

    return "C";
}


function renderCards(rows){

    const box =
        document.getElementById("cards");

    if(
        !rows ||
        !rows.length
    ){

        box.innerHTML =
            '<div class="loading">' +
            '当前日期没有符合三项硬条件的股票' +
            '</div>';

        return;
    }

    box.innerHTML="";

    rows.slice(0,3).forEach(
        (r,i)=>{

        const score=num(r.score);
        const pctv=num(r.pct);

        const card =
            document.createElement("div");

        card.className =
            "card " +
            (i===0 ? "top1" : "");

        const hardOk =
            r.hard_ok ||
            (
                Array.isArray(r.hard) &&
                r.hard.length === 3
            );

        card.innerHTML = `

            <div class="rank">
                TOP ${i+1}
            </div>

            <div class="name">
                ${r.name || "-"}
            </div>

            <div class="code">
                ${r.code || "-"}
            </div>

            <div class="score ${scoreClass(score)}">
                ${score}
            </div>

            <div class="score-label">
                妖股综合评分
            </div>

            <div class="price-row">

                <span class="${pctv>=0?"up":"down"}">
                    ${pct(pctv)}
                </span>

                <span>
                    ${
                        r.price
                        ? num(r.price).toFixed(2)
                        : "-"
                    }
                </span>

            </div>

            <div class="metrics">

                <div class="metric">

                    <div class="metric-title">
                        市值
                    </div>

                    <div class="metric-value">
                        ${money(r.market_cap)}
                    </div>

                </div>

                <div class="metric">

                    <div class="metric-title">
                        换手
                    </div>

                    <div class="metric-value">
                        ${num(r.turnover).toFixed(2)}%
                    </div>

                </div>

                <div class="metric">

                    <div class="metric-title">
                        近3日涨停
                    </div>

                    <div class="metric-value">
                        ${r.zt3 || 0} 次
                    </div>

                </div>

                <div class="metric">

                    <div class="metric-title">
                        龙虎榜资金
                    </div>

                    <div class="metric-value">
                        ${money(r.lhb_day_net)}
                    </div>

                </div>

            </div>

            <div class="badges">

                <span class="badge red">
                    ${rating(score)}
                </span>

                ${
                    hardOk
                    ?
                    '<span class="badge green">三项硬条件通过</span>'
                    :
                    '<span class="badge">观察</span>'
                }

                <span class="badge">
                    盘口 ${num(r.order_score)}/10
                </span>

            </div>
        `;

        box.appendChild(card);

    });
}


function renderTable(rows){

    const tbody =
        document.getElementById("table");

    tbody.innerHTML="";

    if(
        !rows ||
        !rows.length
    ){

        tbody.innerHTML = `
            <tr>
                <td
                    colspan="11"
                    style="
                        text-align:center;
                        color:#697487;
                        padding:25px;
                    "
                >
                    没有符合条件的数据
                </td>
            </tr>
        `;

        return;
    }

    rows.forEach(
        (r,i)=>{

        const score=num(r.score);
        const pctv=num(r.pct);

        const tr =
            document.createElement("tr");

        tr.innerHTML = `

            <td>
                ${i+1}
            </td>

            <td>
                ${r.name || "-"}
            </td>

            <td>
                ${r.code || "-"}
            </td>

            <td
                class="${scoreClass(score)}"
                style="font-weight:900;"
            >
                ${score}
            </td>

            <td
                class="${pctv>=0?"up":"down"}"
            >
                ${pct(pctv)}
            </td>

            <td>
                ${num(r.turnover).toFixed(2)}%
            </td>

            <td>
                ${money(r.market_cap)}
            </td>

            <td>
                ${r.zt3 || 0}
            </td>

            <td
                class="${num(r.lhb_day_net)>=0?"up":"down"}"
            >
                ${money(r.lhb_day_net)}
            </td>

            <td>
                ${num(r.order_score).toFixed(1)}
            </td>

            <td>
                ${rating(score)}
            </td>

        `;

        tbody.appendChild(tr);

    });
}


async function loadData(){

    const date =
        document.getElementById("date").value;

    const cards =
        document.getElementById("cards");

    const error =
        document.getElementById("error");

    const status =
        document.getElementById("status");

    error.innerHTML="";

    cards.innerHTML =
        '<div class="loading">' +
        '正在读取东方财富数据...' +
        '</div>';

    try{

        const url =
            "/api/scanner?date=" +
            encodeURIComponent(date);

        const response =
            await fetch(url);

        const data =
            await response.json();

        if(!response.ok){

            throw new Error(
                data.detail ||
                "服务器返回错误"
            );
        }

        if(data.error){

            throw new Error(
                data.error
            );
        }

        /*
         * 这里是本次最重要的修复：
         *
         * 后端返回的是：
         * data.top3
         * data.ranking
         *
         * 不是 data.rows
         */

        renderCards(
            data.top3 || []
        );

        renderTable(
            data.ranking || []
        );

        let dateText =
            data.date || date;

        if(
            data.requested_date &&
            data.requested_date !== data.date
        ){
            dateText =
                data.requested_date +
                " → 实际交易日 " +
                data.date;
        }

        status.innerText =
            "数据源：" +
            (
                data.source ||
                "东方财富"
            ) +
            " ｜ " +
            dateText +
            " ｜ 硬条件 " +
            (
                data.hard_count || 0
            );

    }catch(e){

        cards.innerHTML="";

        document.getElementById(
            "table"
        ).innerHTML="";

        error.innerHTML =
            '<div class="error">' +
            '加载失败：' +
            String(
                e.message || e
            ) +
            '</div>';

        status.innerText =
            "数据读取失败";
    }
}


document.addEventListener(
    "DOMContentLoaded",
    function(){

        document.getElementById(
            "date"
        ).value = todayCN();

        loadData();
    }
);

</script>

</body>
</html>
"""


# ============================================================
# 首页
# ============================================================

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)


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
                "10000"
            )
        )
    )
