from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
import requests
import os
import re
import time

app = FastAPI(title="妖股雷达")

TZ = ZoneInfo("Asia/Shanghai")
TODAY = datetime.now(TZ).date()

EASTMONEY = "https://push2.eastmoney.com"
EASTMONEY_EX = "https://push2ex.eastmoney.com"
DATACENTER = "https://datacenter-web.eastmoney.com"

MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"

VERSION = "v4.0-score"

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://quote.eastmoney.com/"
})

CACHE = {}
CACHE_SECONDS = 60


# =========================================================
# 基础
# =========================================================

def num(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return default


def clean_code(v):
    m = re.search(r"(\d{6})", str(v or ""))
    return m.group(1) if m else ""


def secid(code):
    c = clean_code(code)

    if c.startswith(("6", "68")):
        return "1." + c

    return "0." + c


def request_json(url, params=None, timeout=8):
    try:
        r = session.get(
            url,
            params=params,
            timeout=timeout
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("REQUEST ERROR:", url, repr(e))
        return {}


# =========================================================
# 交易日
# =========================================================

def get_trading_dates(end_date, count=10):

    params = {
        "secid": "1.000001",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": (
            "f51,f52,f53,f54,f55,"
            "f56,f57,f58,f59,f60"
        ),
        "klt": 101,
        "fqt": 1,
        "beg": "0",
        "end": "20500101"
    }

    data = request_json(
        f"{EASTMONEY}/api/qt/stock/kline/get",
        params
    )

    rows = (
        (data.get("data") or {}).get("klines")
        or []
    )

    dates = []

    for row in rows:
        try:
            d = str(row).split(",")[0]

            dt = datetime.strptime(
                d,
                "%Y-%m-%d"
            ).date()

            if dt <= end_date:
                dates.append(dt)

        except Exception:
            pass

    return sorted(
        set(dates),
        reverse=True
    )[:count]


def get_actual_date(date):
    dates = get_trading_dates(
        date,
        3
    )

    if dates:
        return dates[0]

    return date


# =========================================================
# 涨停池
# =========================================================

def get_zt_pool(date):

    params = {
        "date": date.strftime("%Y%m%d"),
        "pageindex": 0,
        "pagesize": 200,
        "sort": "fbt:asc"
    }

    data = request_json(
        f"{EASTMONEY_EX}/getTopicZTPool",
        params
    )

    rows = (
        (data.get("data") or {}).get("pool")
        or []
    )

    result = set()

    for row in rows:

        code = clean_code(
            row.get("c")
            or row.get("code")
            or row.get("SECURITY_CODE")
        )

        if code:
            result.add(code)

    return result


# =========================================================
# A股市场
# =========================================================

def get_all_a():

    params = {
        "pn": 1,
        "pz": 5000,
        "po": 1,
        "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        "fs": (
            "m:0+t:6,"
            "m:0+t:80,"
            "m:1+t:2,"
            "m:1+t:23"
        ),
        "fields": (
            "f2,f3,f8,f12,f14,f20,f21"
        )
    }

    data = request_json(
        f"{EASTMONEY}/api/qt/clist/get",
        params,
        timeout=10
    )

    rows = (
        (data.get("data") or {}).get("diff")
        or []
    )

    result = []

    for row in rows:

        code = clean_code(
            row.get("f12")
        )

        name = str(
            row.get("f14") or ""
        )

        if not code:
            continue

        if "ST" in name.upper():
            continue

        if "退" in name:
            continue

        result.append({
            "code": code,
            "name": name,
            "price": num(row.get("f2")),
            "pct": num(row.get("f3")),
            "turnover": num(row.get("f8")),
            "market_cap": num(row.get("f20")) / 1e8,
            "float_cap": num(row.get("f21")) / 1e8
        })

    return result


# =========================================================
# 龙虎榜
# =========================================================

def get_lhb(begin_date, end_date):

    raw_filter = (
        f"(TRADE_DATE>='{begin_date:%Y-%m-%d}')"
        f"(TRADE_DATE<='{end_date:%Y-%m-%d}')"
    )

    params = {
        "sortColumns": "TRADE_DATE,SECURITY_CODE",
        "sortTypes": "-1,-1",
        "pageNumber": 1,
        "pageSize": 5000,
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": (
            "SECURITY_CODE,"
            "SECURITY_NAME_ABBR,"
            "TRADE_DATE,"
            "NET_BUY_AMT"
        ),
        "source": "WEB",
        "client": "WEB",
        "filter": quote(
            raw_filter,
            safe="()='"
        )
    }

    data = request_json(
        f"{DATACENTER}/api/data/v1/get",
        params,
        timeout=8
    )

    rows = (
        (data.get("result") or {}).get("data")
        or []
    )

    result = {}

    for row in rows:

        code = clean_code(
            row.get("SECURITY_CODE")
        )

        if not code:
            continue

        result[code] = {
            "net_buy": num(
                row.get("NET_BUY_AMT")
            ),
            "date": str(
                row.get("TRADE_DATE") or ""
            )[:10]
        }

    return result


# =========================================================
# 实时行情
# =========================================================

def get_quotes(codes):

    codes = list(
        dict.fromkeys(
            clean_code(x)
            for x in codes
            if clean_code(x)
        )
    )

    result = {}

    for i in range(
        0,
        len(codes),
        500
    ):

        batch = codes[
            i:i + 500
        ]

        params = {
            "secids": ",".join(
                secid(x)
                for x in batch
            ),
            "fields": (
                "f2,f3,f8,"
                "f12,f14,f20,f21"
            )
        }

        data = request_json(
            f"{EASTMONEY}/api/qt/ulist.np/get",
            params
        )

        rows = (
            (data.get("data") or {}).get("diff")
            or []
        )

        for row in rows:

            code = clean_code(
                row.get("f12")
            )

            if not code:
                continue

            result[code] = {
                "code": code,
                "name": row.get(
                    "f14"
                ) or code,
                "price": num(
                    row.get("f2")
                ),
                "pct": num(
                    row.get("f3")
                ),
                "turnover": num(
                    row.get("f8")
                ),
                "market_cap": (
                    num(row.get("f20"))
                    / 1e8
                ),
                "float_cap": (
                    num(row.get("f21"))
                    / 1e8
                )
            }

    return result


# =========================================================
# 当前盘口
# =========================================================

def get_order_book(code):

    params = {
        "secid": secid(code),
        "fields": (
            "f43,f44,f45,f46,f47,"
            "f49,f50,f51,f52,f53"
        )
    }

    data = request_json(
        f"{EASTMONEY}/api/qt/stock/get",
        params,
        timeout=5
    )

    row = data.get("data") or {}

    buy = sum(
        num(row.get(x))
        for x in (
            "f49",
            "f50",
            "f51",
            "f52",
            "f53"
        )
    )

    sell = sum(
        num(row.get(x))
        for x in (
            "f43",
            "f44",
            "f45",
            "f46",
            "f47"
        )
    )

    return {
        "buy": buy,
        "sell": sell
    }


# =========================================================
# 妙想增强
# =========================================================

def get_mx_codes():

    api_key = os.getenv(
        "MX_APIKEY"
    )

    if not api_key:
        return set()

    try:

        question = """
请筛选今天A股最强势的股票候选。
重点参考：
1. 近期涨停
2. 龙虎榜
3. 市值300亿元以内
4. 涨幅
5. 换手率
6. 市场活跃度

排除ST、退市。
尽量返回股票代码。
"""

        response = session.post(
            MX_URL,
            headers={
                "Content-Type":
                    "application/json",
                "apikey":
                    api_key
            },
            json={
                "keyword": question
            },
            timeout=5
        )

        text = response.text

        return set(
            re.findall(
                r"(?<!\d)(?:0|3|6)\d{5}(?!\d)",
                text
            )
        )

    except Exception as e:

        print(
            "MX ERROR:",
            repr(e)
        )

        return set()


# =========================================================
# 妖股评分模型
# =========================================================

def calculate_score(
    quote,
    zt_count,
    lhb_info,
    order_book=None,
    mx=False
):

    score = 0
    reasons = []

    # -----------------------------------------------------
    # 核心一：近3日涨停
    # -----------------------------------------------------

    if zt_count >= 3:

        score += 30

        reasons.append(
            "近3日3次涨停"
        )

    elif zt_count == 2:

        score += 24

        reasons.append(
            "近3日2次涨停"
        )

    elif zt_count == 1:

        score += 17

        reasons.append(
            "近3日有涨停"
        )

    # -----------------------------------------------------
    # 核心二：龙虎榜
    # -----------------------------------------------------

    if lhb_info:

        score += 18

        reasons.append(
            "上过龙虎榜"
        )

        net_buy = num(
            lhb_info.get(
                "net_buy"
            )
        )

        if net_buy >= 2e8:

            score += 10

            reasons.append(
                "龙虎榜净买入强"
            )

        elif net_buy >= 1e8:

            score += 7

            reasons.append(
                "龙虎榜净买入"
            )

        elif net_buy > 0:

            score += 3

    # -----------------------------------------------------
    # 核心三：市值
    # -----------------------------------------------------

    market_cap = num(
        quote.get(
            "market_cap"
        )
    )

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

        score += 9

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

    pct = num(
        quote.get("pct")
    )

    if pct >= 9:

        score += 14

        reasons.append(
            "涨幅接近涨停"
        )

    elif pct >= 7:

        score += 11

        reasons.append(
            "涨幅强势"
        )

    elif pct >= 5:

        score += 8

        reasons.append(
            "涨幅5%+"
        )

    elif pct >= 3:

        score += 4

    elif pct < -3:

        score -= 5

    # -----------------------------------------------------
    # 换手
    # -----------------------------------------------------

    turnover = num(
        quote.get(
            "turnover"
        )
    )

    if 15 <= turnover < 30:

        score += 10

        reasons.append(
            "换手活跃"
        )

    elif turnover >= 30:

        score += 12

        reasons.append(
            "超高换手"
        )

    elif 8 <= turnover < 15:

        score += 6

    elif turnover >= 5:

        score += 3

    # -----------------------------------------------------
    # 盘口
    # -----------------------------------------------------

    if order_book:

        buy = num(
            order_book.get(
                "buy"
            )
        )

        sell = num(
            order_book.get(
                "sell"
            )
        )

        if buy > 0 and sell > 0:

            ratio = sell / buy

            if ratio >= 1.5:

                score += 6

                reasons.append(
                    "卖盘明显强"
                )

            elif ratio >= 1.1:

                score += 3

                reasons.append(
                    "卖盘略强"
                )

            elif ratio < 0.8:

                score += 6

                reasons.append(
                    "买盘明显强"
                )

    # -----------------------------------------------------
    # 妙想
    # -----------------------------------------------------

    if mx:

        score += 5

        reasons.append(
            "妙想模型增强"
        )

    # -----------------------------------------------------
    # 概率
    # -----------------------------------------------------

    # 不再简单把分数硬套成概率，
    # 而是压缩到 1~99 区间。

    probability = round(
        100 /
        (
            1 +
            pow(
                2.71828,
                -(score - 45) / 14
            )
        )
    )

    probability = max(
        1,
        min(
            99,
            probability
        )
    )

    return (
        score,
        probability,
        reasons
    )


# =========================================================
# 主扫描
# =========================================================

def build_scan(target_date):

    actual = get_actual_date(
        target_date
    )

    dates = get_trading_dates(
        actual,
        5
    )

    last3 = dates[:3]

    if not last3:
        last3 = [actual]

    # -----------------------------------------------------
    # 涨停
    # -----------------------------------------------------

    zt_map = {}

    for d in last3:

        zt_map[d] = get_zt_pool(
            d
        )

    zt_codes = set()

    for codes in zt_map.values():

        zt_codes |= codes

    # -----------------------------------------------------
    # 龙虎榜
    # -----------------------------------------------------

    lhb_map = get_lhb(
        actual -
        timedelta(days=14),
        actual
    )

    lhb_codes = set(
        lhb_map.keys()
    )

    # -----------------------------------------------------
    # 全市场
    # -----------------------------------------------------

    stocks = get_all_a()

    stock_map = {
        x["code"]: x
        for x in stocks
    }

    # -----------------------------------------------------
    # 活跃股票
    # -----------------------------------------------------

    active = sorted(
        stocks,
        key=lambda x: (
            num(x.get("pct")),
            num(x.get("turnover"))
        ),
        reverse=True
    )

    # -----------------------------------------------------
    # 关键优化
    #
    # 不再使用：
    # 涨停 AND 龙虎榜 AND 市值≤300亿
    #
    # 而是：
    # 活跃股 OR 涨停股 OR 龙虎榜股
    #
    # 三项最后统一评分。
    # -----------------------------------------------------

    candidate_codes = set(
        x["code"]
        for x in active[:400]
    )

    candidate_codes |= zt_codes
    candidate_codes |= lhb_codes

    # 最多500只，防止 Render 超时

    candidate_codes = list(
        candidate_codes
    )[:500]

    # -----------------------------------------------------
    # 实时行情
    # -----------------------------------------------------

    quotes = get_quotes(
        candidate_codes
    )

    # -----------------------------------------------------
    # 妙想
    # -----------------------------------------------------

    mx = set()

    if actual == TODAY:

        mx = get_mx_codes()

    # -----------------------------------------------------
    # 第一轮评分
    # -----------------------------------------------------

    results = []

    for code, quote in quotes.items():

        zt_count = sum(
            code in zt_map.get(
                d,
                set()
            )
            for d in last3
        )

        lhb_info = lhb_map.get(
            code
        )

        score, probability, reasons = (
            calculate_score(
                quote=quote,
                zt_count=zt_count,
                lhb_info=lhb_info,
                order_book=None,
                mx=code in mx
            )
        )

        results.append({
            "code": code,
            "name": quote.get(
                "name",
                code
            ),
            "price": round(
                num(
                    quote.get(
                        "price"
                    )
                ),
                2
            ),
            "pct": round(
                num(
                    quote.get(
                        "pct"
                    )
                ),
                2
            ),
            "turnover": round(
                num(
                    quote.get(
                        "turnover"
                    )
                ),
                2
            ),
            "market_cap": round(
                num(
                    quote.get(
                        "market_cap"
                    )
                ),
                2
            ),
            "zt_count": zt_count,
            "lhb": bool(
                lhb_info
            ),
            "lhb_net_buy": round(
                num(
                    (lhb_info or {}).get(
                        "net_buy"
                    )
                ) / 1e8,
                2
            ),
            "score": score,
            "probability": probability,
            "reasons": reasons,
            "order_book": None
        })

    # -----------------------------------------------------
    # 第一轮排序
    # -----------------------------------------------------

    results.sort(
        key=lambda x: (
            x["score"],
            x["zt_count"],
            x["pct"],
            x["turnover"]
        ),
        reverse=True
    )

    # -----------------------------------------------------
    # 盘口只查前20
    # -----------------------------------------------------

    if actual == TODAY:

        top20 = results[:20]

        with ThreadPoolExecutor(
            max_workers=8
        ) as executor:

            future_map = {
                executor.submit(
                    get_order_book,
                    x["code"]
                ): x
                for x in top20
            }

            for future in as_completed(
                future_map
            ):

                item = future_map[
                    future
                ]

                try:

                    book = future.result()

                    item[
                        "order_book"
                    ] = book

                    s, p, r = (
                        calculate_score(
                            quote=quotes[
                                item["code"]
                            ],
                            zt_count=item[
                                "zt_count"
                            ],
                            lhb_info=lhb_map.get(
                                item["code"]
                            ),
                            order_book=book,
                            mx=item["code"] in mx
                        )
                    )

                    item[
                        "score"
                    ] = s

                    item[
                        "probability"
                    ] = p

                    item[
                        "reasons"
                    ] = r

                except Exception:
                    pass

    # -----------------------------------------------------
    # 最终排序
    # -----------------------------------------------------

    results.sort(
        key=lambda x: (
            x["score"],
            x["zt_count"],
            x["pct"],
            x["turnover"]
        ),
        reverse=True
    )

    # -----------------------------------------------------
    # TOP3
    # -----------------------------------------------------

    top3 = results[:3]

    return {
        "success": True,
        "version": VERSION,
        "date": str(actual),
        "requested_date": str(
            target_date
        ),
        "is_today": (
            actual == TODAY
        ),

        "message": (
            "三个核心指标均为评分项，"
            "不要求同时满足。"
        ),

        "core_rules": [
            "近3个交易日出现涨停：评分项",
            "上过龙虎榜：评分项",
            "总市值≤300亿：评分项"
        ],

        "top3": top3,

        "ranking": results[:50],

        "stats": {
            "candidate_count":
                len(candidate_codes),

            "result_count":
                len(results),

            "zt_count":
                len(zt_codes),

            "lhb_count":
                len(lhb_codes)
        }
    }


# =========================================================
# API
# =========================================================

@app.get("/api/health")
def health():

    return {
        "status": "ok",
        "version": VERSION,
        "time":
            datetime.now(
                TZ
            ).isoformat()
    }


@app.get("/api/scanner")
def scanner(
    date: str = Query(
        default=str(TODAY)
    )
):

    try:

        target = datetime.strptime(
            date[:10],
            "%Y-%m-%d"
        ).date()

        cache_key = str(
            target
        )

        now = time.time()

        if (
            cache_key in CACHE
            and
            now -
            CACHE[cache_key][0]
            < CACHE_SECONDS
        ):

            return CACHE[
                cache_key
            ][1]

        data = build_scan(
            target
        )

        CACHE[
            cache_key
        ] = (
            now,
            data
        )

        return data

    except Exception as e:

        print(
            "SCAN ERROR:",
            repr(e)
        )

        return {
            "success": False,
            "version": VERSION,
            "error": str(e),
            "top3": [],
            "ranking": []
        }


# =========================================================
# 页面
# =========================================================

HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">

<head>

<meta charset="UTF-8">

<meta name="viewport"
content="width=device-width,
initial-scale=1,
maximum-scale=1">

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
padding:16px;
}

.header{
display:flex;
justify-content:space-between;
align-items:center;
margin-bottom:15px;
}

.title{
font-size:25px;
font-weight:900;
}

.status{
font-size:12px;
color:#888;
}

.panel{
background:#111;
border:1px solid #292929;
border-radius:15px;
padding:15px;
margin-bottom:14px;
}

.controls{
display:flex;
gap:8px;
}

input{
flex:1;
background:#080808;
color:#fff;
border:1px solid #333;
border-radius:9px;
padding:11px;
font-size:16px;
}

button{
border:0;
border-radius:9px;
padding:11px 16px;
background:#e31313;
color:#fff;
font-weight:bold;
}

button.gray{
background:#333;
}

.rule{
padding:9px;
margin-top:7px;
background:#181818;
border-radius:8px;
font-size:14px;
color:#bbb;
}

.rule b{
color:#ff4b4b;
}

.note{
font-size:12px;
color:#888;
line-height:1.8;
margin-top:10px;
}

.top3{
display:grid;
grid-template-columns:
repeat(3,1fr);
gap:10px;
}

.card{
background:#171717;
border:1px solid #303030;
border-radius:12px;
padding:14px;
}

.rank{
font-size:12px;
color:#888;
}

.name{
font-size:19px;
font-weight:900;
margin-top:4px;
}

.code{
font-size:12px;
color:#888;
}

.prob{
font-size:30px;
font-weight:900;
color:#ff4141;
margin-top:7px;
}

.score{
font-size:13px;
color:#aaa;
}

.reason{
font-size:12px;
line-height:1.8;
color:#bbb;
margin-top:7px;
}

.table{
overflow-x:auto;
}

table{
width:100%;
min-width:720px;
border-collapse:collapse;
}

th,
td{
padding:9px 6px;
border-bottom:
1px solid #242424;
text-align:center;
font-size:13px;
}

th{
color:#888;
}

.red{
color:#ff4b4b;
}

.green{
color:#00d084;
}

.error{
background:#2a0d0d;
border:1px solid #572020;
color:#ff7777;
padding:12px;
border-radius:9px;
}

@media(max-width:600px){

.top3{
grid-template-columns:1fr;
}

.controls{
flex-wrap:wrap;
}

.title{
font-size:22px;
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
class="status">
v4.0
</div>

</div>


<div class="panel">

<div class="controls">

<input
id="date"
type="date">

<button
onclick="scan()">
开始扫描
</button>

<button
class="gray"
onclick="today()">
今天
</button>

</div>


<div class="rule">
① <b>近3个交易日出现涨停</b>
：评分项
</div>

<div class="rule">
② <b>上过龙虎榜</b>
：评分项
</div>

<div class="rule">
③ <b>总市值 ≤ 300亿</b>
：评分项
</div>


<div class="note">

三个核心指标不再要求同时满足。

系统综合：
涨停、龙虎榜、市值、
涨幅、换手、盘口、
妙想增强等因素进行排序。

</div>

</div>


<div
id="error">
</div>


<div class="panel">

<h3>
🔥 TOP 3 强势标的
</h3>

<div
id="top3"
class="top3">
</div>

</div>


<div class="panel">

<h3>
📊 妖股概率排行
</h3>

<div class="table">

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

<tbody
id="ranking">
</tbody>

</table>

</div>

</div>


<div class="note">

数据源：东方财富公开行情接口。
妖股雷达用于量化分析参考，
不构成投资建议。

</div>

</div>


<script>

function setToday(){

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

document
.getElementById("date")
.value =
y+"-"+m+"-"+day;

}


function today(){

setToday();

scan();

}


async function scan(){

const date =
document
.getElementById("date")
.value;

const status =
document
.getElementById("status");

const error =
document
.getElementById("error");

status.innerText =
"扫描中...";

error.innerHTML =
"";


try{

const response =
await fetch(
"/api/scanner?date="
+
encodeURIComponent(date)
);

const data =
await response.json();


if(!data.success){

throw new Error(
data.error ||
"接口返回失败"
);

}


status.innerText =
"数据成功 · "
+
data.version
+
" · "
+
data.date;


render(data);


}catch(e){

status.innerText =
"扫描失败";

error.innerHTML =
'<div class="error">'
+
'加载失败：'
+
e.message
+
'</div>';

}

}


function render(data){

const top =
document
.getElementById("top3");

const ranking =
document
.getElementById("ranking");

top.innerHTML =
"";

ranking.innerHTML =
"";


if(
!data.top3 ||
data.top3.length === 0
){

top.innerHTML =
'<div class="note">'
+
'暂无候选数据'
+
'</div>';

return;

}


data.top3.forEach(
function(x,i){

top.innerHTML +=

'<div class="card">'

+

'<div class="rank">'
+
'TOP '
+
(i+1)
+
'</div>'

+

'<div class="name">'
+
x.name
+
'</div>'

+

'<div class="code">'
+
x.code
+
'</div>'

+

'<div class="prob">'
+
x.probability
+
'%'
+
'</div>'

+

'<div class="score">'
+
'综合评分：'
+
x.score
+
'</div>'

+

'<div class="reason">'
+
x.reasons.join(
' · '
)
+
'</div>'

+

'</div>';

});


data.ranking.forEach(
function(x,i){

ranking.innerHTML +=

'<tr>'

+

'<td>'
+
(i+1)
+
'</td>'

+

'<td>'
+
'<b>'
+
x.name
+
'</b>'
+
'<br>'
+
x.code
+
'</td>'

+

'<td class="red">'
+
x.probability
+
'%'
+
'</td>'

+

'<td>'
+
x.score
+
'</td>'

+

'<td class="'
+
(
x.pct >= 0
?
'red'
:
'green'
)
+
'">'
+
x.pct
+
'%'
+
'</td>'

+

'<td>'
+
x.turnover
+
'%'
+
'</td>'

+

'<td>'
+
x.market_cap
+
'亿'
+
'</td>'

+

'<td>'
+
x.zt_count
+
'</td>'

+

'<td>'
+
(
x.lhb
?
'✓'
:
'-'
)
+
'</td>'

+

'</tr>';

});

}


setToday();

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
