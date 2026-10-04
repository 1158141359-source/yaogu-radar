import os

import math

from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

from urllib.parse import quote, urlencode

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from fastapi import FastAPI, Query

from fastapi.responses import HTMLResponse, JSONResponse

from fastapi.middleware.cors import CORSMiddleware

# ============================================================

# 基础

# ============================================================

app = FastAPI(

    title="妖股雷达",

    version="3.0"

)

app.add_middleware(

    CORSMiddleware,

    allow_origins=["*"],

    allow_credentials=True,

    allow_methods=["*"],

    allow_headers=["*"],

)

TZ = ZoneInfo("Asia/Shanghai")

DATA = "https://push2.eastmoney.com"

DATA2 = "https://push2his.eastmoney.com"

TIMEOUT = 8

MX_APIKEY = os.getenv(

    "MX_APIKEY",

    ""

).strip()

# ============================================================

# HTTP

# ============================================================

SESSION = requests.Session()

SESSION.headers.update({

    "User-Agent":

        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "

        "AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",

    "Referer":

        "https://quote.eastmoney.com/"

})

def http_get(

    url,

    params=None,

    timeout=TIMEOUT

):

    try:

        r = SESSION.get(

            url,

            params=params,

            timeout=timeout

        )

        r.raise_for_status()

        return r.json()

    except Exception:

        return None

# ============================================================

# 工具

# ============================================================

def safe_float(

    value,

    default=0.0

):

    try:

        if value is None:

            return default

        text = str(value).strip()

        if not text:

            return default

        value = float(

            text.replace(",", "")

        )

        if math.isnan(value):

            return default

        if math.isinf(value):

            return default

        return value

    except Exception:

        return default

def clamp(

    value,

    low,

    high

):

    return max(

        low,

        min(high, value)

    )

def is_a_share(code):

    code = str(code)

    return code.startswith((

        "600",

        "601",

        "603",

        "605",

        "688",

        "000",

        "001",

        "002",

        "003",

        "300",

        "301"

    ))

def get_secid(code):

    code = str(code)

    if code.startswith((

        "6",

        "68"

    )):

        return "1." + code

    return "0." + code

# ============================================================

# 交易日

# ============================================================

def get_trading_dates(

    end_date,

    days=20

):

    try:

        d = datetime.strptime(

            str(end_date)[:10],

            "%Y-%m-%d"

        )

    except Exception:

        d = datetime.now(TZ)

    result = []

    for _ in range(500):

        if d.weekday() < 5:

            result.append(

                d.strftime("%Y-%m-%d")

            )

        if len(result) >= days:

            break

        d -= timedelta(days=1)

    return result

def get_actual_target_date(

    date_str=None

):

    if not date_str:

        date_str = datetime.now(

            TZ

        ).strftime("%Y-%m-%d")

    try:

        d = datetime.strptime(

            str(date_str)[:10],

            "%Y-%m-%d"

        )

    except Exception:

        d = datetime.now(TZ)

    while d.weekday() >= 5:

        d -= timedelta(days=1)

    return d.strftime("%Y-%m-%d")

def get_next_trading_date(

    date_str

):

    d = datetime.strptime(

        date_str,

        "%Y-%m-%d"

    )

    for i in range(1, 10):

        x = d + timedelta(days=i)

        if x.weekday() < 5:

            return x.strftime(

                "%Y-%m-%d"

            )

    return None

# ============================================================

# 全A股实时行情

# ============================================================

def get_all_a_stocks():

    url = (

        f"{DATA}/api/qt/clist/get"

    )

    params = {

        "pn": "1",

        "pz": "6000",

        "po": "1",

        "np": "1",

        "fltt": "2",

        "invt": "2",

        "fid": "f3",

        "fs":

            "m:0+t:6,"

            "m:0+t:80,"

            "m:1+t:2,"

            "m:1+t:23",

        "fields":

            "f12,f14,f2,f3,f4,f5,"

            "f6,f7,f8,f9,f15,f16,"

            "f17,f18,f20,f21"

    }

    data = http_get(

        url,

        params,

        10

    )

    result = []

    try:

        diff = data[

            "data"

        ][

            "diff"

        ]

        if isinstance(

            diff,

            dict

        ):

            diff = list(

                diff.values()

            )

    except Exception:

        return result

    for x in diff:

        code = str(

            x.get(

                "f12",

                ""

            )

        )

        if not is_a_share(code):

            continue

        result.append({

            "code": code,

            "name":

                x.get(

                    "f14",

                    ""

                ),

            "price":

                safe_float(

                    x.get("f2")

                ),

            "pct":

                safe_float(

                    x.get("f3")

                ),

            "volume":

                safe_float(

                    x.get("f5")

                ),

            "amount":

                safe_float(

                    x.get("f6")

                ),

            "turnover":

                safe_float(

                    x.get("f8")

                ),

            "market_cap":

                safe_float(

                    x.get("f20")

                ) / 100000000

        })

    return result

# ============================================================

# 历史行情

# ============================================================

def get_history_quotes(

    date_str

):

    url = (

        f"{DATA}/api/data/v1/get"

    )

    params = {

        "pn": "1",

        "pz": "6000",

        "po": "1",

        "np": "1",

        "fltt": "2",

        "invt": "2",

        "fid": "f3",

        "fs":

            "m:0+t:6,"

            "m:0+t:80,"

            "m:1+t:2,"

            "m:1+t:23",

        "fields":

            "f12,f14,f2,f3,"

            "f5,f6,f7,f8,"

            "f20,f21",

        "date":

            date_str.replace(

                "-",

                ""

            )

    }

    data = http_get(

        url,

        params,

        10

    )

    result = {}

    try:

        diff = data[

            "data"

        ][

            "diff"

        ]

        if isinstance(

            diff,

            dict

        ):

            diff = list(

                diff.values()

            )

    except Exception:

        return result

    for x in diff:

        code = str(

            x.get(

                "f12",

                ""

            )

        )

        if not is_a_share(code):

            continue

        result[code] = {

            "code": code,

            "name":

                x.get(

                    "f14",

                    ""

                ),

            "price":

                safe_float(

                    x.get("f2")

                ),

            "pct":

                safe_float(

                    x.get("f3")

                ),

            "amount":

                safe_float(

                    x.get("f6")

                ),

            "turnover":

                safe_float(

                    x.get("f8")

                ),

            "market_cap":

                safe_float(

                    x.get("f20")

                ) / 100000000

        }

    return result

# ============================================================

# 涨停池

# ============================================================

def get_zt_pool(

    date_str

):

    url = (

        f"{DATA}/api/data/v1/get"

    )

    params = {

        "pn": "1",

        "pz": "200",

        "po": "1",

        "np": "1",

        "fltt": "2",

        "invt": "2",

        "fid": "f3",

        "fs":

            "m:90+t:3",

        "fields":

            "f12,f14,f3,f8,f20",

        "date":

            date_str.replace(

                "-",

                ""

            )

    }

    data = http_get(

        url,

        params,

        8

    )

    result = {}

    try:

        diff = data[

            "data"

        ][

            "diff"

        ]

        if isinstance(

            diff,

            dict

        ):

            diff = list(

                diff.values()

            )

    except Exception:

        return result

    for x in diff:

        code = str(

            x.get(

                "f12",

                ""

            )

        )

        if code:

            result[code] = {

                "name":

                    x.get(

                        "f14",

                        ""

                    ),

                "pct":

                    safe_float(

                        x.get("f3")

                    )

            }

    return result

# ============================================================

# 龙虎榜

# ============================================================

def get_lhb(

    date_str

):

    url = (

        f"{DATA}/api/data/v1/get"

    )

    filter_raw = (

        '(SECURITY_TYPE_CODE="05800101")'

    )

    encoded_filter = quote(

        filter_raw,

        safe="()='"

    )

    params = {

        "pn": "1",

        "pz": "200",

        "po": "1",

        "np": "1",

        "fltt": "2",

        "invt": "2",

        "fs":

            "m:90+t:1",

        "fields":

            "SECURITY_CODE,"

            "SECURITY_NAME_ABBR,"

            "CHANGE_RATE,"

            "BUY_AMOUNT,"

            "SELL_AMOUNT,"

            "NET_BUY_AMOUNT"

    }

    try:

        query = urlencode(

            params

        )

        full_url = (

            url

            + "?"

            + query

            + "&filter="

            + encoded_filter

        )

        data = http_get(

            full_url,

            None,

            8

        )

        diff = data[

            "data"

        ][

            "diff"

        ]

        if isinstance(

            diff,

            dict

        ):

            diff = list(

                diff.values()

            )

    except Exception:

        return {}

    result = {}

    for x in diff:

        code = str(

            x.get(

                "SECURITY_CODE"

            )

            or x.get(

                "f12"

            )

            or ""

        )

        if not code:

            continue

        result[code] = {

            "name":

                x.get(

                    "SECURITY_NAME_ABBR",

                    ""

                ),

            "change":

                safe_float(

                    x.get(

                        "CHANGE_RATE"

                    )

                ),

            "buy":

                safe_float(

                    x.get(

                        "BUY_AMOUNT"

                    )

                ),

            "sell":

                safe_float(

                    x.get(

                        "SELL_AMOUNT"

                    )

                ),

            "net":

                safe_float(

                    x.get(

                        "NET_BUY_AMOUNT"

                    )

                )

        }

    return result

# ============================================================

# 备选池预评分

# ============================================================

def pre_score(

    stock,

    zt_map,

    lhb_map

):

    code = stock["code"]

    pct = stock["pct"]

    turnover = stock["turnover"]

    amount = stock["amount"]

    market_cap = stock[

        "market_cap"

    ]

    score = 0

    # 涨幅

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

    # 换手

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

    # 成交额

    if amount >= 500000000:

        score += 15

    elif amount >= 200000000:

        score += 12

    elif amount >= 100000000:

        score += 9

    elif amount >= 50000000:

        score += 5

    # 近3日涨停

    if code in zt_map:

        score += 20

    # 龙虎榜

    if code in lhb_map:

        score += 18

        if lhb_map[

            code

        ].get(

            "net",

            0

        ) > 0:

            score += 8

    # 市值

    if 0 < market_cap <= 300:

        score += 18

        if market_cap <= 100:

            score += 5

    elif market_cap <= 500:

        score += 7

    # 强组合

    if (

        0 < market_cap <= 300

        and turnover >= 15

        and pct >= 5

    ):

        score += 15

    return score

# ============================================================

# 最终概率

# ============================================================

def final_probability(

    stock,

    zt_map,

    lhb_map

):

    code = stock["code"]

    pct = stock["pct"]

    turnover = stock[

        "turnover"

    ]

    amount = stock[

        "amount"

    ]

    market_cap = stock[

        "market_cap"

    ]

    score = 0

    # 涨幅

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

    # 换手

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

    # 成交额

    if amount >= 500000000:

        score += 10

    elif amount >= 200000000:

        score += 8

    elif amount >= 100000000:

        score += 6

    elif amount >= 50000000:

        score += 4

    # 涨停

    if code in zt_map:

        score += 20

    # 龙虎榜

    if code in lhb_map:

        score += 12

        if lhb_map[

            code

        ].get(

            "net",

            0

        ) > 0:

            score += 5

    # 市值

    if 0 < market_cap <= 300:

        score += 15

        if market_cap <= 100:

            score += 4

    elif market_cap <= 500:

        score += 5

    probability = (

        35

        + score * 0.62

    )

    return round(

        clamp(

            probability,

            35,

            99

        ),

        1

    )

# ============================================================

# 当前扫描

# ============================================================

def build_scan(

    requested_date=None

):

    requested_date = (

        requested_date

        or datetime.now(

            TZ

        ).strftime(

            "%Y-%m-%d"

        )

    )

    actual_date = (

        get_actual_target_date(

            requested_date

        )

    )

    trading_dates = (

        get_trading_dates(

            actual_date,

            5

        )[:3]

    )

    stocks = get_all_a_stocks()

    if not stocks:

        return {

            "success": False,

            "message":

                "东方财富接口暂时没有返回股票数据",

            "count": 0,

            "top3": [],

            "ranking": []

        }

    # --------------------------------------------------------

    # 三个交易日涨停池

    # --------------------------------------------------------

    pools = {}

    with ThreadPoolExecutor(

        max_workers=3

    ) as executor:

        futures = {

            executor.submit(

                get_zt_pool,

                d

            ): d

            for d in trading_dates

        }

        for future in as_completed(

            futures

        ):

            d = futures[

                future

            ]

            try:

                pools[d] = (

                    future.result()

                    or {}

                )

            except Exception:

                pools[d] = {}

    zt_map = {}

    for d, pool in pools.items():

        for code in pool:

            if code not in zt_map:

                zt_map[code] = {

                    "count": 0,

                    "latest": None

                }

            zt_map[

                code

            ][

                "count"

            ] += 1

            zt_map[

                code

            ][

                "latest"

            ] = max(

                zt_map[

                    code

                ][

                    "latest"

                ] or d,

                d

            )

    # --------------------------------------------------------

    # 龙虎榜

    # --------------------------------------------------------

    lhb_map = get_lhb(

        actual_date

    )

    # --------------------------------------------------------

    # 备选池

    # --------------------------------------------------------

    for stock in stocks:

        stock["_pre_score"] = (

            pre_score(

                stock,

                zt_map,

                lhb_map

            )

        )

    stocks.sort(

        key=lambda x: (

            x["_pre_score"],

            x["pct"],

            x["turnover"],

            x["amount"]

        ),

        reverse=True

    )

    candidates = stocks[:350]

    stock_map = {

        x["code"]: x

        for x in stocks

    }

    special_codes = (

        set(zt_map.keys())

        | set(lhb_map.keys())

    )

    exists = {

        x["code"]

        for x in candidates

    }

    for code in special_codes:

        if code in exists:

            continue

        if code in stock_map:

            candidates.append(

                stock_map[code]

            )

    candidates = candidates[:380]

    # --------------------------------------------------------

    # 最终评分

    # --------------------------------------------------------

    results = []

    for stock in candidates:

        code = stock[

            "code"

        ]

        probability = (

            final_probability(

                stock,

                zt_map,

                lhb_map

            )

        )

        reasons = []

        if stock["pct"] >= 3:

            reasons.append(

                f"涨幅{stock['pct']:.1f}%"

            )

        if stock["turnover"] >= 8:

            reasons.append(

                f"换手{stock['turnover']:.1f}%"

            )

        if code in zt_map:

            reasons.append(

                "近3日涨停"

                f"{zt_map[code]['count']}次"

            )

        if code in lhb_map:

            reasons.append(

                "龙虎榜"

            )

        if (

            code in lhb_map

            and

            lhb_map[

                code

            ].get(

                "net",

                0

            ) > 0

        ):

            reasons.append(

                "龙虎榜净买"

            )

        if (

            0

            < stock["market_cap"]

            <= 300

        ):

            reasons.append(

                f"市值"

                f"{stock['market_cap']:.0f}"

                "亿"

            )

        if not reasons:

            reasons.append(

                "当前资金强度较高"

            )

        results.append({

            **stock,

            "zt_count":

                zt_map.get(

                    code,

                    {}

                ).get(

                    "count",

                    0

                ),

            "zt_latest":

                zt_map.get(

                    code,

                    {}

                ).get(

                    "latest"

                ),

            "lhb":

                code in lhb_map,

            "lhb_net":

                lhb_map.get(

                    code,

                    {}

                ).get(

                    "net",

                    0

                ),

            "score":

                stock[

                    "_pre_score"

                ],

            "probability":

                probability,

            "reasons":

                reasons

        })

    results.sort(

        key=lambda x: (

            x["probability"],

            x["score"],

            x["pct"],

            x["turnover"]

        ),

        reverse=True

    )

    for x in results:

        x.pop(

            "_pre_score",

            None

        )

    return {

        "success": True,

        "requested_date":

            requested_date,

        "actual_date":

            actual_date,

        "trading_dates":

            trading_dates,

        "count":

            len(results),

        "top3":

            results[:3],

        "ranking":

            results[:50],

        "message":

            "备选池模式："

            "涨停、龙虎榜、"

            "市值≤300亿均为加分项，"

            "不是硬性条件。"

    }

# ============================================================

# 单日历史回测

# ============================================================

def backtest_one_day(

    date_str,

    next_date_str

):

    today_quotes = (

        get_history_quotes(

            date_str

        )

    )

    next_quotes = (

        get_history_quotes(

            next_date_str

        )

    )

    if (

        not today_quotes

        or not next_quotes

    ):

        return []

    zt_pool = (

        get_zt_pool(

            date_str

        )

    )

    lhb_map = (

        get_lhb(

            date_str

        )

    )

    # 当天评分

    for stock in (

        today_quotes.values()

    ):

        stock["_pre_score"] = (

            pre_score(

                stock,

                zt_pool,

                lhb_map

            )

        )

    # 只能使用当天信息

    candidates = sorted(

        today_quotes.values(),

        key=lambda x: (

            x["_pre_score"],

            x["pct"],

            x["turnover"],

            x["amount"]

        ),

        reverse=True

    )[:3]

    results = []

    for stock in candidates:

        code = stock[

            "code"

        ]

        next_stock = (

            next_quotes.get(

                code

            )

        )

        if not next_stock:

            continue

        buy_price = (

            stock["price"]

        )

        sell_price = (

            next_stock["price"]

        )

        if buy_price <= 0:

            continue

        return_pct = (

            sell_price

            / buy_price

            - 1

        ) * 100

        probability = (

            final_probability(

                stock,

                zt_pool,

                lhb_map

            )

        )

        results.append({

            "date":

                date_str,

            "next_date":

                next_date_str,

            "code":

                code,

            "name":

                stock["name"],

            "score":

                stock["_pre_score"],

            "probability":

                probability,

            "buy_price":

                round(

                    buy_price,

                    3

                ),

            "next_price":

                round(

                    sell_price,

                    3

                ),

            "next_pct":

                round(

                    next_stock["pct"],

                    2

                ),

            "return":

                round(

                    return_pct,

                    2

                ),

            "win":

                return_pct > 0,

            "hit_limit":

                next_stock["pct"]

                >= 9.5,

            "zt":

                code in zt_pool,

            "lhb":

                code in lhb_map,

            "market_cap":

                stock["market_cap"],

            "turnover":

                stock["turnover"]

        })

    return results

# ============================================================

# 历史回测

# ============================================================

def run_backtest(

    days=20

):

    days = max(

        5,

        min(

            int(days),

            120

        )

    )

    all_dates = (

        get_trading_dates(

            datetime.now(

                TZ

            ).strftime(

                "%Y-%m-%d"

            ),

            days + 5

        )

    )

    all_dates = list(

        reversed(

            all_dates

        )

    )

    if len(all_dates) < days + 1:

        return {

            "success": False,

            "message":

                "历史交易日不足"

        }

    test_dates = (

        all_dates[

            -(days + 1):

        ]

    )

    all_results = []

    # --------------------------------------------------------

    # 逐日回测

    # --------------------------------------------------------

    for i in range(

        len(test_dates) - 1

    ):

        current_date = (

            test_dates[i]

        )

        next_date_str = (

            test_dates[i + 1]

        )

        try:

            day_results = (

                backtest_one_day(

                    current_date,

                    next_date_str

                )

            )

            all_results.extend(

                day_results

            )

        except Exception:

            continue

    if not all_results:

        return {

            "success": False,

            "message":

                "没有获得有效历史数据"

        }

    # --------------------------------------------------------

    # 基础统计

    # --------------------------------------------------------

    sample_count = len(

        all_results

    )

    win_count = sum(

        1

        for x in all_results

        if x["win"]

    )

    limit_count = sum(

        1

        for x in all_results

        if x["hit_limit"]

    )

    win_rate = (

        win_count

        / sample_count

        * 100

    )

    limit_rate = (

        limit_count

        / sample_count

        * 100

    )

    avg_return = (

        sum(

            x["return"]

            for x in all_results

        )

        / sample_count

    )

    # --------------------------------------------------------

    # 资金曲线

    # --------------------------------------------------------

    capital = 100000.0

    equity = capital

    peak = capital

    max_drawdown = 0

    for x in all_results:

        equity *= (

            1

            + x["return"]

            / 100

        )

        if equity > peak:

            peak = equity

        drawdown = (

            equity

            / peak

            - 1

        ) * 100

        if (

            drawdown

            < max_drawdown

        ):

            max_drawdown = drawdown

    cumulative_return = (

        equity

        / capital

        - 1

    ) * 100

    # --------------------------------------------------------

    # TOP1 胜率

    # --------------------------------------------------------

    by_date = {}

    for x in all_results:

        by_date.setdefault(

            x["date"],

            []

        ).append(x)

    top1_results = []

    daily_results = []

    for date_str, items in (

        by_date.items()

    ):

        items.sort(

            key=lambda x:

                x["probability"],

            reverse=True

        )

        top1_results.append(

            items[0]

        )

        daily_results.append({

            "date":

                date_str,

            "count":

                len(items),

            "avg_return":

                round(

                    sum(

                        x["return"]

                        for x in items

                    )

                    / len(items),

                    2

                ),

            "win_count":

                sum(

                    1

                    for x in items

                    if x["win"]

                )

        })

    top1_win_rate = (

        sum(

            1

            for x in top1_results

            if x["win"]

        )

        / len(top1_results)

        * 100

        if top1_results

        else 0

    )

    return {

        "success":

            True,

        "days":

            days,

        "sample_count":

            sample_count,

        "win_rate":

            round(

                win_rate,

                2

            ),

        "top1_win_rate":

            round(

                top1_win_rate,

                2

            ),

        "limit_rate":

            round(

                limit_rate,

                2

            ),

        "avg_return":

            round(

                avg_return,

                2

            ),

        "cumulative_return":

            round(

                cumulative_return,

                2

            ),

        "max_return":

            round(

                max(

                    x["return"]

                    for x in all_results

                ),

                2

            ),

        "min_return":

            round(

                min(

                    x["return"]

                    for x in all_results

                ),

                2

            ),

        "max_drawdown":

            round(

                max_drawdown,

                2

            ),

        "daily":

            sorted(

                daily_results,

                key=lambda x:

                    x["date"],

                reverse=True

            ),

        "details":

            all_results[:300]

    }

# ============================================================

# API

# ============================================================

@app.get(

    "/api/health"

)

def health():

    return {

        "success":

            True,

        "service":

            "妖股雷达",

        "version":

            "3.0",

        "time":

            datetime.now(

                TZ

            ).isoformat()

    }

@app.get(

    "/api/scanner"

)

def scanner(

    date: str = Query(

        default=None

    )

):

    try:

        return JSONResponse(

            build_scan(

                date

            )

        )

    except Exception as e:

        return JSONResponse(

            status_code=500,

            content={

                "success":

                    False,

                "message":

                    f"扫描失败：{e}",

                "count":

                    0,

                "top3":

                    [],

                "ranking":

                    []

            }

        )

@app.get(

    "/api/backtest"

)

def backtest(

    days: int = Query(

        default=20,

        ge=5,

        le=120

    )

):

    try:

        return JSONResponse(

            run_backtest(

                days

            )

        )

    except Exception as e:

        return JSONResponse(

            status_code=500,

            content={

                "success":

                    False,

                "message":

                    f"回测失败：{e}"

            }

        )

# ============================================================

# 网页

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

        Arial;

}

.container{

    width:100%;

    max-width:900px;

    margin:auto;

    padding:10px;

}

.header{

    display:flex;

    justify-content:space-between;

    align-items:center;

    margin-bottom:12px;

}

.title{

    font-size:24px;

    font-weight:900;

}

.status{

    color:#999;

    font-size:12px;

}

.panel{

    background:#101010;

    border:1px solid #252525;

    border-radius:14px;

    padding:13px;

    margin-bottom:12px;

}

.controls{

    display:flex;

    gap:7px;

    flex-wrap:wrap;

}

input,

button{

    border:1px solid #333;

    background:#181818;

    color:#fff;

    border-radius:9px;

    padding:10px 12px;

    font-size:14px;

}

button{

    cursor:pointer;

}

button.primary{

    background:#b52222;

}

.rules{

    display:grid;

    gap:6px;

    margin-top:10px;

}

.rule{

    background:#171717;

    padding:8px;

    border-radius:8px;

    font-size:12px;

}

.rule b{

    color:#ffd166;

}

.note{

    color:#888;

    font-size:11px;

    line-height:1.6;

    margin-top:8px;

}

.rank-title{

    font-size:18px;

    font-weight:800;

    margin-bottom:10px;

}

.top3{

    display:grid;

    gap:9px;

}

.card{

    background:#151515;

    border:1px solid #292929;

    border-radius:12px;

    padding:12px;

}

.card.hot{

    border-color:#8b2525;

}

.card-head{

    display:flex;

    justify-content:space-between;

    align-items:center;

}

.name{

    font-size:17px;

    font-weight:800;

}

.code{

    font-size:11px;

    color:#777;

    margin-left:5px;

}

.prob{

    font-size:20px;

    color:#ff5050;

    font-weight:900;

}

.metrics{

    display:grid;

    grid-template-columns:repeat(3,1fr);

    gap:6px;

    margin-top:9px;

}

.metric{

    background:#0b0b0b;

    border-radius:7px;

    padding:7px;

}

.metric-title{

    font-size:10px;

    color:#777;

}

.metric-value{

    font-size:13px;

    font-weight:700;

    margin-top:2px;

}

.up{

    color:#ff5050;

}

.down{

    color:#35d17b;

}

.tags{

    display:flex;

    gap:5px;

    flex-wrap:wrap;

    margin-top:8px;

}

.tag{

    background:#252525;

    color:#bbb;

    border-radius:5px;

    padding:4px 6px;

    font-size:10px;

}

.rank{

    display:grid;

    grid-template-columns:32px 1fr auto;

    gap:7px;

    align-items:center;

    padding:10px 0;

    border-bottom:1px solid #222;

}

.rank-no{

    color:#666;

}

.rank-name{

    font-weight:700;

}

.rank-sub{

    font-size:10px;

    color:#777;

    margin-top:2px;

}

.rank-prob{

    font-weight:800;

    color:#ff5555;

}

.empty,

.loading{

    text-align:center;

    color:#777;

    padding:25px 5px;

}

.footer{

    text-align:center;

    color:#555;

    font-size:10px;

    padding:12px 0 25px;

}

@media(max-width:500px){

    .container{

        padding:9px;

    }

    .metrics{

        grid-template-columns:

            repeat(3,1fr);

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

<div

id="status"

class="status"

>

候选 0 只

</div>

</div>

<!-- 扫描 -->

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

onclick="today()"

>

今天

</button>

</div>

<div class="rules">

<div class="rule">

① <b>近3个交易日涨停</b>

→ 加分项

</div>

<div class="rule">

② <b>龙虎榜</b>

→ 加分项

</div>

<div class="rule">

③ <b>市值≤300亿</b>

→ 加分项

</div>

</div>

<div class="note">

以上三项不是硬条件。

满足其中一项、两项、

或者全部不满足，只要综合强度较高，

都可以进入备选池。

</div>

</div>

<!-- TOP3 -->

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

<!-- 排行 -->

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

<!-- 回测 -->

<div class="panel">

<div class="rank-title">

📈 历史回测

</div>

<div class="controls">

<button

onclick="backtest(5)"

>

5日

</button>

<button

onclick="backtest(20)"

>

20日

</button>

<button

onclick="backtest(60)"

>

60日

</button>

<button

onclick="backtest(120)"

>

120日

</button>

</div>

<div

id="backtestResult"

class="empty"

>

选择回测周期

</div>

</div>

<div class="footer">

数据来源：

东方财富公开行情接口

<br>

历史回测为模型历史模拟结果，

不构成投资建议

</div>

</div>

<script>

function esc(

    text

){

    return String(

        text ?? ""

    )

    .replaceAll(

        "&",

        "&amp;"

    )

    .replaceAll(

        "<",

        "&lt;"

    )

    .replaceAll(

        ">",

        "&gt;"

    )

    .replaceAll(

        '"',

        "&quot;"

    )

    .replaceAll(

        "'",

        "&#039;"

    );

}

function today(){

    const d =

        new Date();

    const value =

        d.toISOString()

        .slice(

            0,

            10

        );

    document

        .getElementById(

            "date"

        )

        .value = value;

    scan();

}

function renderCard(

    stock,

    index

){

    return `

<div class="card ${

    index === 0

    ? "hot"

    : ""

}">

<div class="card-head">

<div>

<span class="name">

${esc(stock.name)}

</span>

<span class="code">

${esc(stock.code)}

</span>

</div>

<div class="prob">

${Number(

    stock.probability || 0

).toFixed(1)}%

</div>

</div>

<div class="metrics">

<div class="metric">

<div class="metric-title">

涨幅

</div>

<div class="metric-value ${

    Number(stock.pct || 0) >= 0

    ? "up"

    : "down"

}">

${Number(

    stock.pct || 0

).toFixed(2)}%

</div>

</div>

<div class="metric">

<div class="metric-title">

换手

</div>

<div class="metric-value">

${Number(

    stock.turnover || 0

).toFixed(2)}%

</div>

</div>

<div class="metric">

<div class="metric-title">

市值

</div>

<div class="metric-value">

${Number(

    stock.market_cap || 0

).toFixed(0)}亿

</div>

</div>

</div>

<div class="tags">

${

    (stock.reasons || [])

    .map(

        x =>

        `<span class="tag">

        ${esc(x)}

        </span>`

    )

    .join("")

}

</div>

</div>

`;

}

function renderRank(

    stock,

    index

){

    return `

<div class="rank">

<div class="rank-no">

${index + 1}

</div>

<div>

<div class="rank-name">

${esc(stock.name)}

<span class="code">

${esc(stock.code)}

</span>

</div>

<div class="rank-sub">

涨幅

${Number(

    stock.pct || 0

).toFixed(2)}%

·

换手

${Number(

    stock.turnover || 0

).toFixed(2)}%

·

涨停

${stock.zt_count || 0}次

·

${

    stock.lhb

    ? "龙虎榜"

    : "未上龙虎榜"

}

</div>

</div>

<div class="rank-prob">

${Number(

    stock.probability || 0

).toFixed(1)}%

</div>

</div>

`;

}

async function scan(){

    const date =

        document

        .getElementById(

            "date"

        )

        .value;

    const top3 =

        document

        .getElementById(

            "top3"

        );

    const ranking =

        document

        .getElementById(

            "ranking"

        );

    top3.innerHTML =

        `

        <div class="loading">

        正在扫描……

        </div>

        `;

    ranking.innerHTML =

        `

        <div class="loading">

        正在计算……

        </div>

        `;

    try{

        let url =

            "/api/scanner";

        if(date){

            url +=

                "?date="

                +

                encodeURIComponent(

                    date

                );

        }

        const response =

            await fetch(

                url

            );

        const data =

            await response.json();

        if(

            !data.success

        ){

            throw new Error(

                data.message

                || "扫描失败"

            );

        }

        document

        .getElementById(

            "status"

        )

        .innerText =

            `候选 ${

                data.count || 0

            } 只`;

        top3.innerHTML =

            (

                data.top3 || []

            )

            .map(

                renderCard

            )

            .join("")

            ||

            `

            <div class="empty">

            暂无候选

            </div>

            `;

        ranking.innerHTML =

            (

                data.ranking || []

            )

            .map(

                renderRank

            )

            .join("")

            ||

            `

            <div class="empty">

            暂无数据

            </div>

            `;

    }

    catch(error){

        top3.innerHTML =

            `

            <div class="empty">

            扫描失败，请稍后重试

            </div>

            `;

        ranking.innerHTML =

            `

            <div class="empty">

            数据接口暂时不可用

            </div>

            `;

    }

}

async function backtest(

    days

){

    const box =

        document

        .getElementById(

            "backtestResult"

        );

    box.innerHTML =

        `

        <div class="loading">

        正在回测

        ${days}

        个交易日……

        <br><br>

        历史数据较多，

        请稍候

        </div>

        `;

    try{

        const response =

            await fetch(

                "/api/backtest?days="

                + days

            );

        const data =

            await response.json();

        if(

            !data.success

        ){

            throw new Error(

                data.message

                || "回测失败"

            );

        }

        box.innerHTML = `

<div class="metrics">

<div class="metric">

<div class="metric-title">

样本

</div>

<div class="metric-value">

${data.sample_count}

</div>

</div>

<div class="metric">

<div class="metric-title">

胜率

</div>

<div class="metric-value up">

${data.win_rate}%

</div>

</div>

<div class="metric">

<div class="metric-title">

TOP1胜率

</div>

<div class="metric-value up">

${data.top1_win_rate}%

</div>

</div>

<div class="metric">

<div class="metric-title">

平均收益

</div>

<div class="metric-value ${

    data.avg_return >= 0

    ? "up"

    : "down"

}">

${data.avg_return}%

</div>

</div>

<div class="metric">

<div class="metric-title">

累计收益

</div>

<div class="metric-value ${

    data.cumulative_return >= 0

    ? "up"

    : "down"

}">

${data.cumulative_return}%

</div>

</div>

<div class="metric">

<div class="metric-title">

涨停命中

</div>

<div class="metric-value up">

${data.limit_rate}%

</div>

</div>

</div>

<div class="note">

最大单次收益：

<b class="up">

${data.max_return}%

</b>

<br>

最大单次亏损：

<b class="down">

${data.min_return}%

</b>

<br>

最大回撤：

<b class="down">

${data.max_drawdown}%

</b>

</div>

<div

style="

margin-top:15px;

font-weight:800;

"

>

最近回测样本

</div>

${

    (data.details || [])

    .slice(

        0,

        20

    )

    .map(

        x => `

<div class="rank">

<div class="rank-no">

${esc(

    x.date

)}

</div>

<div>

<div class="rank-name">

${esc(

    x.name

)}

<span class="code">

${esc(

    x.code

)}

</span>

</div>

<div class="rank-sub">

下一交易日：

${esc(

    x.next_date

)}

·

${

    x.hit_limit

    ? "涨停"

    : "未涨停"

}

</div>

</div>

<div class="${

    x.return >= 0

    ? "up"

    : "down"

}">

${

    x.return >= 0

    ? "+"

    : ""

}

${x.return}%

</div>

</div>

`

    )

    .join("")

}

`;

    }

    catch(error){

        box.innerHTML =

            `

            <div class="empty">

            回测失败。

            <br><br>

            东方财富历史接口

            当前没有返回足够数据，

            请稍后再试。

            </div>

            `;

    }

}

// 默认日期

(function(){

    const d =

        new Date();

    document

    .getElementById(

        "date"

    )

    .value =

        d.toISOString()

        .slice(

            0,

            10

        );

})();

</script>

</body>

</html>

"""

# ============================================================

# 首页

# ============================================================

@app.get(

    "/",

    response_class=HTMLResponse

)

def index():

    return HTMLResponse(

        HTML

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

        )

    )
