# -*- coding: utf-8 -*-

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn


app = FastAPI(title="视频量价选股器")


# =========================================================
# 东方财富妙想
# =========================================================

MX_URL = (
    "https://mkapi2.dfcfs.com/"
    "finskillshub/api/claw/stock-screen"
)


# =========================================================
# 东方财富历史K线
# =========================================================

KLINE_URL = (
    "https://push2his.eastmoney.com/"
    "api/qt/stock/kline/get"
)


# =========================================================
# MX API KEY
# =========================================================

def get_key():

    key = os.getenv("MX_APIKEY", "").strip()

    if not key:
        raise RuntimeError(
            "没有检测到 MX_APIKEY"
        )

    return key


# =========================================================
# 妙想候选股票
# =========================================================

def mx_select():

    query = """
筛选今天A股中近期活跃、近期出现明显上涨或冲高、
近期成交量发生明显变化、存在量价转强可能的股票。

不要使用：
龙虎榜、连板、市值300亿、
竞价换手率29.25%、竞价涨幅5%、
委卖大于委买。

尽可能返回100只股票。

必须返回：
股票代码
股票简称
最新价
涨跌幅
成交量
成交额
换手率
"""

    response = requests.post(
        MX_URL,
        headers={
            "Content-Type": "application/json",
            "apikey": get_key()
        },
        json={
            "keyword": query,
            "pageNo": 1,
            "pageSize": 100
        },
        timeout=60
    )

    response.raise_for_status()

    return response.json()


# =========================================================
# 解析妙想结果
# =========================================================

def find_data_lists(obj):

    result = []

    def walk(x, level=0):

        if level > 10:
            return

        if isinstance(x, dict):

            if isinstance(
                x.get("dataList"),
                list
            ):
                result.append(x)

            for value in x.values():
                walk(value, level + 1)

        elif isinstance(x, list):

            for value in x[:20]:
                walk(value, level + 1)

    walk(obj)

    return result


def parse_candidates(raw):

    nodes = find_data_lists(raw)

    if not nodes:
        return []

    node = max(
        nodes,
        key=lambda x: len(
            x.get("dataList", [])
        )
    )

    rows = node.get(
        "dataList",
        []
    )

    result = []

    for row in rows:

        if not isinstance(row, dict):
            continue

        code = (
            row.get("SECURITY_CODE")
            or row.get("股票代码")
            or row.get("代码")
            or ""
        )

        name = (
            row.get("SECURITY_SHORT_NAME")
            or row.get("股票简称")
            or row.get("名称")
            or ""
        )

        price = (
            row.get("NEWEST_PRICE")
            or row.get("最新价")
            or row.get("现价")
            or ""
        )

        pct = (
            row.get("CHG")
            or row.get("涨跌幅")
            or row.get("涨幅")
            or ""
        )

        turnover = (
            row.get("TURNOVER_RATE")
            or row.get("换手率")
            or ""
        )

        if not code:
            continue

        code = str(code)

        # 只保留A股6位代码
        if not re.match(
            r"^(60|68|00|30|83|87|88)\d{4}$",
            code
        ):
            continue

        result.append({
            "code": code,
            "name": str(name),
            "price": price,
            "pct": pct,
            "turnover": turnover
        })

    # 去重
    unique = {}

    for item in result:
        unique[item["code"]] = item

    return list(unique.values())


# =========================================================
# 市场代码
# =========================================================

def get_secid(code):

    code = str(code)

    if code.startswith(
        ("60", "68", "51", "58")
    ):
        return "1." + code

    return "0." + code


# =========================================================
# 获取历史K线
# =========================================================

def get_kline(code):

    secid = get_secid(code)

    params = {
        "secid": secid,

        "fields1":
            "f1,f2,f3,f4",

        "fields2":
            "f51,f52,f53,f54,f55,f56,f57",

        "klt": 101,

        "fqt": 1,

        "beg": "0",

        "end": "20500101",

        "lmt": 90,

        "ut":
            "fa5fd1943c7b386f172d6893dbbd1d0c"
    }

    headers = {
        "User-Agent":
            "Mozilla/5.0",
        "Referer":
            "https://quote.eastmoney.com/"
    }

    try:

        r = requests.get(
            KLINE_URL,
            params=params,
            headers=headers,
            timeout=15
        )

        r.raise_for_status()

        data = r.json()

        if not data.get("data"):
            return []

        klines = (
            data["data"].get(
                "klines",
                []
            )
        )

        result = []

        for line in klines:

            parts = str(line).split(",")

            if len(parts) < 11:
                continue

            try:

                result.append({
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                    "amount": float(parts[6]),
                    "amplitude": float(parts[7]),
                    "pct": float(parts[8]),
                    "change": float(parts[9]),
                    "turnover": float(parts[10])
                })

            except Exception:
                continue

        return result

    except Exception:

        return []


# =========================================================
# 数学工具
# =========================================================

def avg(values):

    if not values:
        return 0

    return sum(values) / len(values)


def lowest(values):

    if not values:
        return 0

    return min(values)


def highest(values):

    if not values:
        return 0

    return max(values)


# =========================================================
# 五项量价评分
# =========================================================

def calculate_score(k):

    if len(k) < 30:

        return {
            "score": 0,
            "tags": [],
            "details": {
                "缩量回调": False,
                "地量": False,
                "止跌": False,
                "温和放量": False,
                "放量突破": False
            }
        }

    # 最近数据
    last = k[-1]

    close = [
        x["close"]
        for x in k
    ]

    high = [
        x["high"]
        for x in k
    ]

    low = [
        x["low"]
        for x in k
    ]

    volume = [
        x["volume"]
        for x in k
    ]


    # -----------------------------------------------------
    # 1. 缩量回调
    # -----------------------------------------------------

    # 最近20日之前的高点
    previous_high = highest(
        high[-50:-15]
    )

    recent_low = lowest(
        low[-15:]
    )

    # 高点之后出现一定幅度回调
    pullback = False

    if previous_high > 0:

        pullback_rate = (
            previous_high - recent_low
        ) / previous_high

        pullback = (
            pullback_rate >= 0.05
        )

    # 回调阶段成交量下降
    old_volume = avg(
        volume[-30:-15]
    )

    recent_volume = avg(
        volume[-10:]
    )

    shrinking = (
        old_volume > 0
        and recent_volume
        < old_volume * 0.85
    )

    cond1 = (
        pullback
        and shrinking
    )


    # -----------------------------------------------------
    # 2. 地量
    # -----------------------------------------------------

    recent_10_volume = avg(
        volume[-10:]
    )

    volume_60 = sorted(
        volume[-60:]
    )

    position_20 = volume_60[
        max(
            0,
            int(len(volume_60) * 0.20)
        )
    ]

    cond2 = (
        recent_10_volume
        <= position_20 * 1.15
    )


    # -----------------------------------------------------
    # 3. 止跌
    # -----------------------------------------------------

    last_5_low = low[-5:]

    last_5_close = close[-5:]

    low_min = min(
        last_5_low
    )

    # 后面几天不再持续创新低
    stable_low = (
        last_5_low[-1]
        >= low_min * 0.99
    )

    # 最近收盘价高于5日前
    price_stable = (
        last_5_close[-1]
        >= last_5_close[0] * 0.98
    )

    cond3 = (
        stable_low
        and price_stable
    )


    # -----------------------------------------------------
    # 4. 温和放量
    # -----------------------------------------------------

    previous_5_volume = avg(
        volume[-6:-1]
    )

    latest_volume = volume[-1]

    latest_up = (
        close[-1]
        > k[-1]["open"]
    )

    mild_volume = (
        previous_5_volume > 0
        and latest_volume
        >= previous_5_volume * 1.15
        and latest_volume
        <= previous_5_volume * 3.0
    )

    cond4 = (
        latest_up
        and mild_volume
    )


    # -----------------------------------------------------
    # 5. 放量突破
    # -----------------------------------------------------

    resistance = highest(
        high[-21:-1]
    )

    volume_20 = avg(
        volume[-21:-1]
    )

    breakout_price = (
        close[-1]
        > resistance * 1.005
    )

    breakout_volume = (
        volume_20 > 0
        and volume[-1]
        >= volume_20 * 1.30
    )

    cond5 = (
        breakout_price
        and breakout_volume
    )


    conditions = {
        "缩量回调": cond1,
        "地量": cond2,
        "止跌": cond3,
        "温和放量": cond4,
        "放量突破": cond5
    }

    tags = [
        name
        for name, ok
        in conditions.items()
        if ok
    ]

    score = len(tags) * 20

    return {
        "score": score,
        "tags": tags,
        "details": conditions
    }


# =========================================================
# 单只股票分析
# =========================================================

def analyze(stock):

    k = get_kline(
        stock["code"]
    )

    result = calculate_score(k)

    stock["score"] = result["score"]

    stock["tags"] = result["tags"]

    stock["details"] = result["details"]

    stock["kline_count"] = len(k)

    if k:

        last = k[-1]

        stock["price"] = last["close"]

        stock["pct"] = last["pct"]

        stock["turnover"] = last["turnover"]

        stock["volume"] = last["volume"]

        stock["amount"] = last["amount"]

    return stock


# =========================================================
# 主扫描
# =========================================================

def scan():

    try:

        raw = mx_select()

        candidates = parse_candidates(
            raw
        )

        if not candidates:

            return {
                "success": False,
                "message":
                    "妙想没有返回候选股票",
                "data": []
            }

        results = []

        # 并发拉取K线
        with ThreadPoolExecutor(
            max_workers=8
        ) as executor:

            jobs = {
                executor.submit(
                    analyze,
                    stock
                ): stock
                for stock in candidates
            }

            for future in as_completed(
                jobs
            ):

                try:

                    stock = future.result()

                    results.append(stock)

                except Exception:

                    pass


        # -------------------------------------------------
        # 关键：
        # 不再把0分股票伪装成TOP3
        # -------------------------------------------------

        results.sort(
            key=lambda x: (
                x.get("score", 0),
                len(x.get("tags", [])),
                x.get("pct", 0)
                if isinstance(
                    x.get("pct"),
                    (int, float)
                )
                else 0
            ),
            reverse=True
        )

        qualified = [
            x
            for x in results
            if x.get("score", 0) >= 50
        ]

        return {
            "success": True,
            "message": "扫描完成",
            "total_scanned": len(results),
            "qualified": len(qualified),
            "data": qualified[:50],
            "all": results[:50]
        }

    except Exception as e:

        return {
            "success": False,
            "message": str(e),
            "data": []
        }


# =========================================================
# 网页
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

<title>视频量价选股</title>

<style>

*{
box-sizing:border-box
}

body{
margin:0;
background:#090b0e;
color:#eee;
font-family:
-apple-system,
BlinkMacSystemFont,
"PingFang SC",
"Microsoft YaHei",
sans-serif
}

.wrap{
max-width:1100px;
margin:auto;
padding:12px
}

.box{
background:#12161b;
border:1px solid #292f36;
border-radius:15px;
padding:14px;
margin-bottom:12px
}

.title{
font-size:21px;
font-weight:900
}

.sub{
font-size:12px;
color:#929ba5;
margin-top:6px;
line-height:1.5
}

.steps{
display:grid;
grid-template-columns:
repeat(5,1fr);
gap:6px;
margin-top:12px
}

.step{
background:#181d22;
border-radius:9px;
padding:9px 4px;
text-align:center;
font-size:11px
}

.step b{
display:block;
font-size:14px;
margin-bottom:3px
}

button{
width:100%;
border:0;
border-radius:10px;
padding:13px;
margin-top:12px;
background:#e53935;
color:#fff;
font-size:16px;
font-weight:900
}

.status{
margin-top:9px;
font-size:12px;
color:#a2abb5
}

h2{
margin:0 0 12px;
font-size:18px
}

.badge{
font-size:11px;
background:#19361f;
color:#58dc78;
padding:4px 7px;
border-radius:6px
}

.top{
display:grid;
grid-template-columns:
repeat(3,1fr);
gap:9px
}

.card{
background:#181d22;
border:1px solid #303842;
border-radius:12px;
padding:12px
}

.rank{
font-size:12px;
color:#ffc94a
}

.stock{
font-size:17px;
font-weight:900;
margin-top:5px
}

.code{
color:#818b96;
font-size:11px
}

.score{
font-size:28px;
font-weight:900;
margin-top:4px
}

.info{
font-size:12px;
color:#a4adb7
}

.tags{
display:flex;
gap:4px;
flex-wrap:wrap;
margin-top:8px
}

.tag{
background:#222a32;
padding:4px 6px;
border-radius:5px;
font-size:10px
}

.table-wrap{
overflow:auto
}

table{
width:100%;
min-width:850px;
border-collapse:collapse
}

th,td{
padding:8px 6px;
border-bottom:1px solid #292f36;
font-size:11px;
text-align:center
}

th{
background:#171c21;
color:#aeb7c0
}

.name{
text-align:left;
font-weight:800
}

.good{
color:#55dd79;
font-weight:900
}

.bad{
color:#666f79
}

.up{
color:#ff5252
}

.down{
color:#43db79
}

.empty{
padding:30px;
text-align:center;
color:#7e8791
}

@media(max-width:650px){

.steps{
grid-template-columns:
repeat(2,1fr)
}

.top{
grid-template-columns:1fr
}

.title{
font-size:19px
}

}

</style>

</head>


<body>

<div class="wrap">


<div class="box">

<div class="title">
📦 视频量价选股 · 今日结果
</div>

<div class="sub">
缩量回调 → 地量 → 止跌 → 温和放量 → 放量突破
｜每项20分｜≥50分进入候选
</div>


<div class="steps">

<div class="step">
<b>20分</b>
缩量回调
</div>

<div class="step">
<b>20分</b>
地量
</div>

<div class="step">
<b>20分</b>
止跌
</div>

<div class="step">
<b>20分</b>
温和放量
</div>

<div class="step">
<b>20分</b>
放量突破
</div>

</div>


<button onclick="scan()">
🔍 开始选股
</button>


<div id="status"
class="status">

等待扫描

</div>

</div>


<div class="box">

<h2>
🏆 TOP 3 强势候选
<span id="topCount"
class="badge">
0只
</span>
</h2>


<div id="top"
class="top">

<div class="empty">
等待扫描
</div>

</div>

</div>


<div class="box">

<h2>
🎯 重点信号
<span id="count"
class="badge">
≥50分
</span>
</h2>


<div class="table-wrap">

<table>

<thead>

<tr>

<th>代码</th>
<th>名称</th>
<th>评分</th>
<th>满足</th>
<th>缩量</th>
<th>地量</th>
<th>止跌</th>
<th>温和放量</th>
<th>突破</th>
<th>涨跌幅</th>
<th>换手率</th>

</tr>

</thead>


<tbody id="tbody">

<tr>

<td
colspan="11"
class="empty">

等待扫描

</td>

</tr>

</tbody>

</table>

</div>

</div>


</div>


<script>


function renderTop(data){

let top =
data.slice(0,3)

document.getElementById(
"topCount"
).textContent =
top.length + "只"


if(!top.length){

document.getElementById(
"top"
).innerHTML =
'<div class="empty">今天没有≥50分股票</div>'

return

}


document.getElementById(
"top"
).innerHTML =

top.map(
(s,i)=>`

<div class="card">

<div class="rank">
${["🥇 TOP 1","🥈 TOP 2","🥉 TOP 3"][i]}
</div>

<div class="stock">
${s.name}
<span class="code">
${s.code}
</span>
</div>

<div class="score">
${s.score}分
</div>

<div class="info">
${s.price || "--"}
　
${s.pct || "--"}%
</div>

<div class="tags">

${(s.tags || [])
.map(
x=>`
<span class="tag">
✓ ${x}
</span>
`
)
.join("")}

</div>

</div>

`
)
.join("")

}


function renderTable(data){

let tbody =
document.getElementById(
"tbody"
)


if(!data.length){

tbody.innerHTML =
`
<tr>
<td colspan="11"
class="empty">

今天没有达到50分

</td>
</tr>
`

return

}


let names = [
"缩量回调",
"地量",
"止跌",
"温和放量",
"放量突破"
]


tbody.innerHTML =

data.map(
s=>`

<tr>

<td>
${s.code}
</td>

<td class="name">
${s.name}
</td>

<td class="good">
${s.score}
</td>

<td>
${(s.tags||[]).length}/5
</td>


${names.map(
n=>{

let ok =
s.details &&
s.details[n]

return `
<td class="${ok?"good":"bad"}">
${ok?"✓":"—"}
</td>
`

}
).join("")}


<td>
${s.pct ?? "--"}%
</td>

<td>
${s.turnover ?? "--"}
</td>

</tr>

`
)
.join("")

}


async function scan(){

document.getElementById(
"status"
).textContent =
"正在获取候选股票并计算90日历史K线…"


document.getElementById(
"top"
).innerHTML =
'<div class="empty">正在计算…</div>'


try{

let response =
await fetch(
"/api/scan",
{
cache:"no-store"
}
)


let data =
await response.json()


if(!data.success){

document.getElementById(
"status"
).textContent =
"扫描失败：" +
data.message

return

}


let stocks =
data.data || []


document.getElementById(
"status"
).textContent =

"扫描完成：" +
(data.total_scanned || 0) +
"只候选 → ≥50分：" +
(data.qualified || 0) +
"只"


renderTop(stocks)

renderTable(stocks)


}

catch(error){

document.getElementById(
"status"
).textContent =
"服务器错误：" +
error

}

}


</script>

</body>

</html>
"""


# =========================================================
# FastAPI
# =========================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def home():

    return HTML


@app.get(
    "/api/scan"
)
def api_scan():

    return scan()


@app.get(
    "/health"
)
def health():

    return {
        "status": "ok"
    }


if __name__ == "__main__":

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