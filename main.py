import os
import re
import time
import statistics
from datetime import datetime, timedelta

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse


app = FastAPI(title="妖股雷达")

# =========================================================
# 基础配置
# =========================================================

MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"
MX_KEY = os.getenv("MX_APIKEY", "").strip()

EASTMONEY_QUOTE = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_ZT = "https://push2.eastmoney.com/api/qt/ztpool/get"
EASTMONEY_LHB = "https://push2.eastmoney.com/api/qt/clist/get"

CACHE = {}
CACHE_SECONDS = 60

# =========================================================
# 五个硬条件
# =========================================================

HARD_RULES = [
    "连续5日上涨",
    "30日内有过涨停",
    "收盘不破5日线",
    "成交量堆量",
    "底部筹码不动",
]

# =========================================================
# 工具函数
# =========================================================

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_float(x, default=0.0):
    try:
        if x is None or x == "":
            return default
        if isinstance(x, str):
            x = x.replace(",", "").replace("%", "").strip()
        return float(x)
    except Exception:
        return default


def normalize_code(code):
    if code is None:
        return ""

    s = str(code).strip().upper()

    s = re.sub(r"^(SH|SZ|BJ)", "", s)

    if re.match(r"^\d{6}$", s):
        return s

    return ""


def market_prefix(code):
    code = normalize_code(code)

    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return "1"

    return "0"


def quote_secids(codes):
    return ",".join(
        f"{market_prefix(c)}.{c}"
        for c in codes
        if normalize_code(c)
    )


def request_json(url, params=None, headers=None, timeout=15):
    try:
        r = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=timeout
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


# =========================================================
# 妙想候选股
# =========================================================

QUERY = """
请筛选今天A股市场中最强势的股票，至少返回30只候选股。

只要沪深京A股，排除ST、*ST、退市股。

请返回：
代码、名称、最新价、涨跌幅、换手率、总市值。

重点优先：
1. 最近30个交易日出现过涨停
2. 最近有明显强势上涨
3. 成交活跃
4. 有龙虎榜或明显资金活跃迹象
5. 总市值尽量不超过300亿

请严格返回股票列表，不要长篇解释。
"""


def mx_query():
    if not MX_KEY:
        return []

    key = "mx_candidates"

    if key in CACHE:
        t, data = CACHE[key]
        if time.time() - t < CACHE_SECONDS:
            return data

    try:
        r = requests.post(
            MX_URL,
            headers={
                "Content-Type": "application/json",
                "apikey": MX_KEY,
                "Authorization": f"Bearer {MX_KEY}",
            },
            json={
                "keyword": QUERY
            },
            timeout=40
        )

        obj = r.json()

    except Exception:
        return []

    rows = []

    # 尝试常见返回结构
    paths = [
        ["data", "data", "allResults", "result", "dataList"],
        ["data", "allResults", "result", "dataList"],
        ["data", "dataList"],
        ["data", "rows"],
        ["rows"],
        ["data"],
    ]

    for path in paths:
        cur = obj

        try:
            for p in path:
                cur = cur[p]

            if isinstance(cur, list):
                rows = cur
                break

        except Exception:
            pass

    # 如果接口返回文本
    if not rows:
        text = str(obj)

        pattern = r'([036]\d{5})[^\n]{0,100}'

        for m in re.finditer(pattern, text):
            rows.append({
                "代码": m.group(1)
            })

    result = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        code = (
            row.get("代码")
            or row.get("code")
            or row.get("证券代码")
            or row.get("股票代码")
            or row.get("SECURITY_CODE")
        )

        name = (
            row.get("名称")
            or row.get("name")
            or row.get("证券名称")
            or row.get("股票名称")
            or row.get("SECURITY_NAME_ABBR")
        )

        code = normalize_code(code)

        if not code:
            continue

        result.append({
            "code": code,
            "name": str(name or code),
        })

    # 去重
    unique = {}

    for x in result:
        unique[x["code"]] = x

    result = list(unique.values())

    CACHE[key] = (time.time(), result)

    return result


# =========================================================
# 东方财富实时行情
# =========================================================

def get_quotes(codes):
    if not codes:
        return {}

    secids = quote_secids(codes)

    params = {
        "fltt": "2",
        "invt": "2",
        "fields": "f2,f3,f8,f12,f13,f14,f20,f21",
        "secids": secids,
    }

    obj = request_json(
        EASTMONEY_QUOTE,
        params=params,
        timeout=15
    )

    data = obj.get("data") or {}

    diff = data.get("diff") or []

    if isinstance(diff, dict):
        diff = list(diff.values())

    result = {}

    for row in diff:
        code = normalize_code(row.get("f12"))

        if not code:
            continue

        result[code] = {
            "price": safe_float(row.get("f2")),
            "pct": safe_float(row.get("f3")),
            "turnover": safe_float(row.get("f8")),
            "name": row.get("f14") or code,
            "market_cap": safe_float(row.get("f20")),
            "float_cap": safe_float(row.get("f21")),
        }

    return result


# =========================================================
# K线
# =========================================================

def get_kline(code, beg="19000101", end="29991231", limit=250):
    code = normalize_code(code)

    if not code:
        return []

    params = {
        "secid": f"{market_prefix(code)}.{code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": beg,
        "end": end,
        "lmt": limit,
    }

    obj = request_json(
        EASTMONEY_KLINE,
        params=params,
        timeout=15
    )

    data = obj.get("data") or {}

    klines = data.get("klines") or []

    result = []

    for item in klines:
        try:
            p = str(item).split(",")

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

        except Exception:
            continue

    return result


# =========================================================
# 五个条件计算
# =========================================================

def condition_1(k):
    """
    连续5日上涨
    连续5个交易日收盘价逐日抬高
    """

    if len(k) < 6:
        return False

    x = k[-6:]

    return all(
        x[i]["close"] > x[i - 1]["close"]
        for i in range(1, 6)
    )


def condition_2(k):
    """
    30日内有过涨停

    采用A股常规10%涨停代理。
    ST已经在候选股阶段排除。
    """

    if not k:
        return False

    recent = k[-30:]

    for x in recent:
        if x["pct"] >= 9.5:
            return True

    return False


def condition_3(k):
    """
    收盘不破5日线
    """

    if len(k) < 5:
        return False

    closes = [x["close"] for x in k[-5:]]

    ma5 = sum(closes) / 5

    return k[-1]["close"] >= ma5


def condition_4(k):
    """
    成交量堆量

    当前5日平均成交量 > 前5日平均成交量
    且最近一日成交量 >= 前一日成交量
    """

    if len(k) < 11:
        return False

    prev = statistics.mean(
        x["volume"]
        for x in k[-10:-5]
    )

    recent = statistics.mean(
        x["volume"]
        for x in k[-5:]
    )

    last = k[-1]["volume"]
    before = k[-2]["volume"]

    if prev <= 0:
        return False

    return (
        recent > prev * 1.15
        and last >= before
    )


def condition_5(k):
    """
    底部筹码不动

    普通东方财富公开K线接口无法完整取得历史筹码峰分布，
    因此使用透明K线代理：

    1. 最近20日价格仍处于相对底部区域
    2. 最近10日低点没有明显破坏
    3. 价格逐步向上

    页面明确标注为“K线代理”。
    """

    if len(k) < 20:
        return False

    recent20 = k[-20:]

    low20 = min(x["low"] for x in recent20)
    high20 = max(x["high"] for x in recent20)

    close = k[-1]["close"]

    if high20 <= low20:
        return False

    position = (close - low20) / (high20 - low20)

    lows10 = [x["low"] for x in k[-10:]]

    # 最近10日低点不能持续明显破坏
    low_change = lows10[-1] / max(lows10[0], 0.0001) - 1

    # 价格不能已经处于极端高位
    return (
        position <= 0.85
        and low_change >= -0.08
    )


def calculate_conditions(k):
    c1 = condition_1(k)
    c2 = condition_2(k)
    c3 = condition_3(k)
    c4 = condition_4(k)
    c5 = condition_5(k)

    conditions = [c1, c2, c3, c4, c5]

    return conditions, sum(1 for x in conditions if x)


# =========================================================
# 买点 / 持有 / 卖点
# =========================================================

def buy_signal(k):
    if len(k) < 5:
        return {
            "point": "等待",
            "reason": "K线不足"
        }

    today = k[-1]

    ma5 = sum(
        x["close"] for x in k[-5:]
    ) / 5

    # 反转阴低点附近 + 援军代理
    if (
        today["close"] < today["open"]
        and today["low"] >= ma5 * 0.97
        and today["volume"] >= k[-2]["volume"]
    ):
        return {
            "point": f"{today['low']:.2f}附近",
            "reason": "反转阴低点附近 + 成交量援军"
        }

    if today["close"] >= ma5:
        return {
            "point": f"{today['close']:.2f}附近",
            "reason": "收盘站上5日线"
        }

    return {
        "point": "等待",
        "reason": "等待重新站回5日线"
    }


def hold_signal(k):
    if len(k) < 3:
        return "观察"

    a = k[-3]
    b = k[-2]
    c = k[-1]

    if (
        c["high"] > b["high"]
        and b["high"] > a["high"]
        and c["low"] > b["low"]
        and c["close"] > b["close"]
    ):
        return "持有：高点高、低点高、收盘高"

    return "观察"


def sell_signal(k):
    if len(k) < 2:
        return "观察"

    a = k[-2]
    b = k[-1]

    if b["high"] < a["high"]:
        return "警戒：高点未创新高"

    if b["close"] <= a["close"]:
        return "警戒：收盘未站上前一日"

    if b["low"] <= a["low"]:
        return "警戒：低点未抬高"

    return "暂不卖出"


# =========================================================
# 单只股票
# =========================================================

def build_stock(candidate, quote):
    code = candidate["code"]
    name = quote.get("name") or candidate.get("name") or code

    k = get_kline(
        code,
        beg=(datetime.now() - timedelta(days=100)).strftime("%Y%m%d"),
        end=datetime.now().strftime("%Y%m%d"),
        limit=120
    )

    if len(k) < 20:
        return None

    conditions, score = calculate_conditions(k)

    # 0-2直接过滤
    if score < 3:
        return None

    missing = [
        HARD_RULES[i]
        for i, ok in enumerate(conditions)
        if not ok
    ]

    buy = buy_signal(k)

    stock = {
        "code": code,
        "name": name,
        "price": quote.get("price", k[-1]["close"]),
        "pct": quote.get("pct", k[-1]["pct"]),
        "turnover": quote.get("turnover", k[-1]["turnover"]),
        "market_cap": quote.get("market_cap", 0),

        "score": score,
        "score_text": f"{score}/5",

        "conditions": conditions,
        "missing": missing,

        "buy_point": buy["point"],
        "buy_reason": buy["reason"],

        "hold": hold_signal(k),
        "sell": sell_signal(k),

        "rules": [
            HARD_RULES[i]
            for i, ok in enumerate(conditions)
            if ok
        ],
    }

    return stock


# =========================================================
# 扫描
# =========================================================

def scan():
    candidates = mx_query()

    codes = [
        x["code"]
        for x in candidates
    ]

    if not codes:
        return {
            "strong": [],
            "near": [],
            "watch": [],
            "top3": [],
            "all": [],
            "time": now_text()
        }

    quotes = get_quotes(codes)

    all_stocks = []

    for candidate in candidates:

        code = candidate["code"]

        quote = quotes.get(code)

        if not quote:
            continue

        # 排除ST
        name = quote.get("name", "")

        if "ST" in str(name).upper():
            continue

        stock = build_stock(
            candidate,
            quote
        )

        if stock:
            all_stocks.append(stock)

        # 防止公开接口被打爆
        time.sleep(0.05)

    # 去重
    unique = {}

    for x in all_stocks:
        unique[x["code"]] = x

    all_stocks = list(unique.values())

    all_stocks.sort(
        key=lambda x: (
            x["score"],
            x["pct"],
            x["turnover"]
        ),
        reverse=True
    )

    strong = [
        x for x in all_stocks
        if x["score"] == 5
    ]

    near = [
        x for x in all_stocks
        if x["score"] == 4
    ]

    watch = [
        x for x in all_stocks
        if x["score"] == 3
    ]

    # TOP3
    if strong:
        top3 = strong[:3]
        top3_mode = "5/5强势池"
    else:
        top3 = near[:3]
        top3_mode = "今日无5/5，4/5替补"

    for i, x in enumerate(top3, 1):
        x["rank"] = i

    return {
        "strong": strong,
        "near": near,
        "watch": watch,
        "top3": top3,
        "top3_mode": top3_mode,
        "all": all_stocks,
        "time": now_text(),
        "rules": HARD_RULES,
        "chip_note": "底部筹码不动：公开K线接口无法完整取得筹码峰，当前使用K线代理指标。",
    }


# =========================================================
# API
# =========================================================

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "time": now_text(),
        "mx_key": bool(MX_KEY),
        "version": "5.0"
    }


@app.get("/api/scanner")
def scanner():
    try:
        return JSONResponse(scan())
    except Exception as e:
        return JSONResponse({
            "strong": [],
            "near": [],
            "watch": [],
            "top3": [],
            "all": [],
            "error": str(e),
            "time": now_text()
        })


# =========================================================
# 前端
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
    background:#080b10;
    color:#e8edf3;
    font-family:-apple-system,BlinkMacSystemFont,
    "PingFang SC","Microsoft YaHei",Arial,sans-serif;
}

.container{
    max-width:1100px;
    margin:auto;
    padding:18px 14px 50px;
}

.header{
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:12px;
    margin-bottom:15px;
}

h1{
    margin:0;
    font-size:25px;
}

.sub{
    color:#7e8997;
    font-size:12px;
    margin-top:5px;
}

button{
    border:0;
    border-radius:9px;
    padding:10px 15px;
    background:#e53935;
    color:white;
    font-weight:bold;
    cursor:pointer;
}

button:active{
    transform:scale(.97);
}

.date{
    background:#11161d;
    border:1px solid #252c35;
    border-radius:10px;
    padding:12px;
    margin-bottom:12px;
}

.rules{
    display:grid;
    grid-template-columns:repeat(5,1fr);
    gap:8px;
    margin:12px 0;
}

.rule{
    background:#11161d;
    border:1px solid #242b35;
    border-radius:9px;
    padding:10px 7px;
    text-align:center;
    font-size:12px;
}

.rule b{
    display:block;
    color:#ff5252;
    margin-bottom:5px;
}

.section{
    margin-top:18px;
}

.section-title{
    display:flex;
    justify-content:space-between;
    align-items:center;
    margin-bottom:9px;
}

.section-title h2{
    font-size:17px;
    margin:0;
}

.badge{
    font-size:11px;
    padding:4px 7px;
    border-radius:6px;
    background:#222831;
    color:#b9c2cc;
}

.cards{
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:10px;
}

.card{
    background:#11161d;
    border:1px solid #282f39;
    border-radius:12px;
    padding:13px;
}

.card.strong{
    border-color:#8b2525;
}

.card.near{
    border-color:#9a681b;
}

.card.watch{
    border-color:#77701e;
}

.card-head{
    display:flex;
    justify-content:space-between;
    align-items:center;
}

.stock-name{
    font-weight:bold;
    font-size:17px;
}

.code{
    color:#778391;
    font-size:11px;
}

.score{
    font-size:20px;
    font-weight:bold;
    color:#ff3b30;
}

.meta{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:6px;
    margin:11px 0;
}

.meta div{
    background:#0c1015;
    padding:7px;
    border-radius:6px;
    font-size:11px;
    color:#8e99a6;
}

.meta b{
    display:block;
    color:#eee;
    margin-top:2px;
    font-size:13px;
}

.up{
    color:#ff4d4f !important;
}

.down{
    color:#31d07c !important;
}

.signal{
    margin-top:7px;
    font-size:12px;
    line-height:1.6;
}

.signal b{
    color:#b9c2cc;
}

.missing{
    margin-top:9px;
    color:#ffb74d;
    font-size:11px;
}

.table-wrap{
    overflow-x:auto;
    background:#11161d;
    border:1px solid #252c35;
    border-radius:10px;
}

table{
    width:100%;
    min-width:800px;
    border-collapse:collapse;
}

th,td{
    padding:10px 8px;
    border-bottom:1px solid #20262f;
    font-size:12px;
    text-align:left;
}

th{
    color:#7f8a97;
    background:#0d1117;
}

.empty{
    padding:20px;
    text-align:center;
    color:#65707d;
}

.note{
    margin-top:10px;
    color:#697583;
    font-size:11px;
    line-height:1.6;
}

@media(max-width:700px){

    .container{
        padding:14px 10px 40px;
    }

    .header{
        align-items:flex-start;
    }

    h1{
        font-size:22px;
    }

    .rules{
        grid-template-columns:repeat(2,1fr);
    }

    .rules .rule:last-child{
        grid-column:span 2;
    }

    .cards{
        grid-template-columns:1fr;
    }

    button{
        white-space:nowrap;
    }
}

</style>
</head>

<body>

<div class="container">

<div class="header">

<div>
<h1>🔥 妖股雷达</h1>
<div class="sub">
5条硬条件 · 综合评分 · 东方财富公开行情数据
</div>
</div>

<button onclick="scan()">开始扫描</button>

</div>

<div class="date">
<b>扫描日期：</b>
<span id="date"></span>
</div>

<div class="rules">

<div class="rule">
<b>①</b>
连续5日上涨
</div>

<div class="rule">
<b>②</b>
30日内有过涨停
</div>

<div class="rule">
<b>③</b>
收盘不破5日线
</div>

<div class="rule">
<b>④</b>
成交量堆量
</div>

<div class="rule">
<b>⑤</b>
底部筹码不动
</div>

</div>


<div class="section">

<div class="section-title">
<h2>🏆 TOP3 强势标的</h2>
<span class="badge" id="topmode">等待扫描</span>
</div>

<div id="top3" class="cards">
<div class="empty">暂无数据，点击“开始扫描”</div>
</div>

</div>


<div class="section">

<div class="section-title">
<h2>🔥 5/5 强势池</h2>
<span class="badge">正式候选</span>
</div>

<div id="strong" class="cards">
<div class="empty">暂无符合条件的股票</div>
</div>

</div>


<div class="section">

<div class="section-title">
<h2>🟠 4/5 高度接近</h2>
<span class="badge">替补候选</span>
</div>

<div id="near" class="cards">
<div class="empty">暂无符合条件的股票</div>
</div>

</div>


<div class="section">

<div class="section-title">
<h2>🟡 3/5 观察池</h2>
<span class="badge">观察</span>
</div>

<div id="watch" class="cards">
<div class="empty">暂无符合条件的股票</div>
</div>

</div>


<div class="section">

<div class="section-title">
<h2>📊 综合评分排行</h2>
</div>

<div class="table-wrap">

<table>

<thead>
<tr>
<th>排名</th>
<th>股票</th>
<th>评分</th>
<th>涨幅</th>
<th>换手</th>
<th>市值</th>
<th>买点</th>
<th>持有</th>
<th>卖点</th>
<th>缺少条件</th>
</tr>
</thead>

<tbody id="ranking">
<tr>
<td colspan="10" class="empty">
暂无3/5以上股票
</td>
</tr>
</tbody>

</table>

</div>

</div>

<div class="note">
数据：东方财富公开行情接口。<br>
⑤“底部筹码不动”由于普通公开K线接口无法完整取得历史筹码峰，当前采用K线代理指标，避免虚构筹码数据。<br>
综合评分仅用于策略筛选，不构成投资建议。
</div>

</div>


<script>

function esc(s){
    if(s===null || s===undefined) return "";
    return String(s)
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;")
    .replaceAll('"',"&quot;");
}


function card(x, cls){

    const pct = Number(x.pct || 0);

    return `
    <div class="card ${cls || ""}">

        <div class="card-head">

            <div>
                <div class="stock-name">
                    ${esc(x.name)}
                </div>

                <div class="code">
                    ${esc(x.code)}
                </div>
            </div>

            <div class="score">
                ${esc(x.score_text)}
            </div>

        </div>

        <div class="meta">

            <div>
                最新价
                <b>${Number(x.price || 0).toFixed(2)}</b>
            </div>

            <div>
                涨跌幅
                <b class="${pct >= 0 ? "up" : "down"}">
                    ${pct.toFixed(2)}%
                </b>
            </div>

            <div>
                换手率
                <b>${Number(x.turnover || 0).toFixed(2)}%</b>
            </div>

            <div>
                市值
                <b>${formatCap(x.market_cap)}</b>
            </div>

        </div>

        <div class="signal">
            <b>买点：</b>
            ${esc(x.buy_point)}
            <br>
            ${esc(x.buy_reason)}
        </div>

        <div class="signal">
            <b>持有：</b>
            ${esc(x.hold)}
        </div>

        <div class="signal">
            <b>卖点：</b>
            ${esc(x.sell)}
        </div>

        <div class="signal">
            <b>触发：</b>
            ${x.rules.map(esc).join("、")}
        </div>

        ${
            x.missing && x.missing.length
            ?
            `<div class="missing">
                缺少：${x.missing.map(esc).join("、")}
            </div>`
            :
            ""
        }

    </div>
    `;
}


function formatCap(v){

    v = Number(v || 0);

    if(v <= 0){
        return "-";
    }

    if(v >= 100000000){
        return (v / 100000000).toFixed(1) + "亿";
    }

    return Math.round(v).toLocaleString();
}


function renderCards(id, arr, cls){

    const el = document.getElementById(id);

    if(!arr || !arr.length){

        el.innerHTML =
            `<div class="empty">暂无符合条件的股票</div>`;

        return;
    }

    el.innerHTML =
        arr.map(x => card(x, cls)).join("");
}


function renderRanking(arr){

    const el = document.getElementById("ranking");

    if(!arr || !arr.length){

        el.innerHTML =
            `<tr>
                <td colspan="10" class="empty">
                    暂无3/5以上股票
                </td>
            </tr>`;

        return;
    }

    el.innerHTML = arr.map((x,i)=>{

        const pct = Number(x.pct || 0);

        return `
        <tr>

            <td>${i+1}</td>

            <td>
                <b>${esc(x.name)}</b>
                <br>
                <span class="code">${esc(x.code)}</span>
            </td>

            <td>
                <b>${esc(x.score_text)}</b>
            </td>

            <td class="${pct >= 0 ? "up" : "down"}">
                ${pct.toFixed(2)}%
            </td>

            <td>
                ${Number(x.turnover || 0).toFixed(2)}%
            </td>

            <td>
                ${formatCap(x.market_cap)}
            </td>

            <td>
                ${esc(x.buy_point)}
            </td>

            <td>
                ${esc(x.hold)}
            </td>

            <td>
                ${esc(x.sell)}
            </td>

            <td>
                ${
                    x.missing && x.missing.length
                    ? esc(x.missing.join("、"))
                    : "无"
                }
            </td>

        </tr>
        `;

    }).join("");
}


async function scan(){

    document.getElementById("date").innerText =
        "正在扫描……";

    try{

        const r = await fetch(
            "/api/scanner?ts=" + Date.now()
        );

        const data = await r.json();

        document.getElementById("date").innerText =
            data.time || new Date().toLocaleString();

        document.getElementById("topmode").innerText =
            data.top3_mode || "扫描完成";

        renderCards(
            "top3",
            data.top3,
            "strong"
        );

        renderCards(
            "strong",
            data.strong,
            "strong"
        );

        renderCards(
            "near",
            data.near,
            "near"
        );

        renderCards(
            "watch",
            data.watch,
            "watch"
        );

        renderRanking(data.all);

    }catch(e){

        document.getElementById("date").innerText =
            "扫描失败";

        console.error(e);

    }
}


document.getElementById("date").innerText =
    new Date().toLocaleDateString("zh-CN");

</script>

</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)
