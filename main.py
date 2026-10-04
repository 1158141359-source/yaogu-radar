from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import json
import time
import html

app = FastAPI(title="妖股雷达")

# =========================
# 基础配置
# =========================

EASTMONEY_URL = "https://push2.eastmoney.com/api/qt/clist/get"

CACHE = {
    "time": 0,
    "data": [],
    "source": "等待数据"
}

CACHE_SECONDS = 15

# 接口异常时使用的兜底数据
FALLBACK_DATA = [
    {
        "code": "000001",
        "name": "平安银行",
        "price": 12.58,
        "pct": 9.96,
        "turnover": 5.2,
        "volratio": 2.1,
        "amount": 8.6,
        "score": 82,
        "state": "强势"
    },
    {
        "code": "000725",
        "name": "京东方A",
        "price": 4.31,
        "pct": 8.29,
        "turnover": 4.8,
        "volratio": 1.8,
        "amount": 6.2,
        "score": 76,
        "state": "强势"
    }
]


# =========================
# 工具函数
# =========================

def to_float(value, default=0.0):
    try:
        if value is None or value == "-":
            return default
        return float(value)
    except:
        return default


def limit_pct(code):
    """
    不同板块涨跌幅限制不同。
    创业板/科创板按20%附近判断，其余主板按10%附近判断。
    """
    if code.startswith(("300", "301", "688", "689")):
        return 19.5
    return 9.5


def calculate_score(code, pct, turnover, volratio, amount):
    """
    妖股雷达评分：
    涨幅 + 换手 + 量比 + 成交额 + 涨停强度

    这是量化筛选指标，不是买卖建议。
    """

    limit = limit_pct(code)

    pct_score = min(max(pct, 0) / limit, 1) * 40

    turnover_score = min(max(turnover, 0), 15) / 15 * 20

    volume_score = min(max(volratio, 0), 5) / 5 * 15

    amount_score = min(max(amount, 0), 20) / 20 * 10

    limit_bonus = 15 if pct >= limit else 0

    score = round(
        min(
            100,
            pct_score
            + turnover_score
            + volume_score
            + amount_score
            + limit_bonus
        )
    )

    return score


def get_state(code, pct):
    limit = limit_pct(code)

    if pct >= limit:
        return "涨停附近"

    if pct >= 7:
        return "强势"

    if pct >= 3:
        return "异动"

    return "观察"


# =========================
# 获取东方财富行情
# =========================

def fetch_market():

    params = {
        "pn": 1,
        "pz": 1000,
        "po": 1,
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": "f3",

        # 沪A、深A、创业板、科创板
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",

        # 代码、名称、价格、涨幅、成交量、成交额、换手率、量比
        "fields": "f12,f14,f2,f3,f5,f6,f8,f10"
    }

    url = EASTMONEY_URL + "?" + urlencode(params)

    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/"
        }
    )

    with urlopen(req, timeout=8) as response:
        raw = response.read().decode("utf-8", "ignore")

    result = json.loads(raw)

    data = result.get("data") or {}
    diff = data.get("diff") or []

    if isinstance(diff, dict):
        diff = list(diff.values())

    stocks = []

    for item in diff:

        code = str(item.get("f12") or "")
        name = str(item.get("f14") or "")

        if not code or not name:
            continue

        # 排除 ST、退市等风险股
        upper_name = name.upper()

        if "ST" in upper_name:
            continue

        if name.startswith("退"):
            continue

        price = to_float(item.get("f2"))
        pct = to_float(item.get("f3"))
        amount = to_float(item.get("f6")) / 100000000
        turnover = to_float(item.get("f8"))
        volratio = to_float(item.get("f10"))

        if price <= 0:
            continue

        score = calculate_score(
            code,
            pct,
            turnover,
            volratio,
            amount
        )

        state = get_state(code, pct)

        stocks.append({
            "code": code,
            "name": name,
            "price": price,
            "pct": pct,
            "turnover": turnover,
            "volratio": volratio,
            "amount": amount,
            "score": score,
            "state": state
        })

    # 按雷达评分排序
    stocks.sort(
        key=lambda x: (
            x["score"],
            x["pct"]
        ),
        reverse=True
    )

    return stocks[:50]


# =========================
# 获取数据（带缓存）
# =========================

def get_market_data():

    now = time.time()

    # 15秒缓存，避免反复请求
    if (
        CACHE["data"]
        and now - CACHE["time"] < CACHE_SECONDS
    ):
        return CACHE["data"], CACHE["source"]

    try:

        data = fetch_market()

        if data:

            CACHE["time"] = now
            CACHE["data"] = data
            CACHE["source"] = "东方财富行情"

            return data, "东方财富行情"

    except Exception as e:

        print("行情接口异常：", e)

    # 接口异常
    CACHE["time"] = now
    CACHE["data"] = FALLBACK_DATA
    CACHE["source"] = "演示数据"

    return FALLBACK_DATA, "演示数据"


# =========================
# API
# =========================

@app.get("/api/health")
def health():

    return {
        "status": "ok",
        "time": datetime.now().isoformat()
    }


@app.get("/api/scanner")
def scanner():

    data, source = get_market_data()

    return {
        "status": "ok",
        "source": source,
        "updated_at": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "data": data
    }


# =========================
# 网页
# =========================

def render_rows(data):

    rows = ""

    for x in data:

        name = html.escape(str(x["name"]))
        code = html.escape(str(x["code"]))

        pct = x["pct"]
        score = x["score"]

        if pct >= 0:
            pct_text = f"+{pct:.2f}%"
        else:
            pct_text = f"{pct:.2f}%"

        rows += f"""
        <tr>
            <td class="code">{code}</td>

            <td class="name">
                {name}
            </td>

            <td>
                {x["price"]:.2f}
            </td>

            <td class="up">
                {pct_text}
            </td>

            <td>
                {x["turnover"]:.2f}%
            </td>

            <td>
                {x["volratio"]:.2f}
            </td>

            <td>
                {x["state"]}
            </td>

            <td>
                <span class="score">
                    {score}
                </span>
            </td>
        </tr>
        """

    return rows


@app.get("/", response_class=HTMLResponse)
def home():

    data, source = get_market_data()

    rows = render_rows(data)

    strong_count = sum(
        1 for x in data
        if x["pct"] >= 7
    )

    limit_count = sum(
        1 for x in data
        if x["state"] == "涨停附近"
    )

    if data:
        avg_score = round(
            sum(x["score"] for x in data) / len(data)
        )
    else:
        avg_score = 0

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    source_text = html.escape(source)

    page = """
<!doctype html>

<html lang="zh-CN">

<head>

<meta charset="utf-8">

<meta
name="viewport"
content="width=device-width,initial-scale=1"
>

<title>妖股雷达</title>

<style>

*{
    box-sizing:border-box;
}

body{
    margin:0;
    background:#080c13;
    color:#e8edf5;
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        "PingFang SC",
        sans-serif;
}

.wrap{
    max-width:1200px;
    margin:auto;
    padding:22px 14px;
}

h1{
    margin:0;
    font-size:28px;
}

.sub{
    color:#7f8da3;
    margin-top:7px;
    margin-bottom:20px;
}

.status{
    display:inline-block;
    padding:5px 9px;
    border-radius:8px;
    background:#12251c;
    color:#42d98b;
    font-size:12px;
    margin-bottom:16px;
}

.cards{
    display:grid;
    grid-template-columns:
        repeat(4,1fr);
    gap:12px;
    margin-bottom:18px;
}

.card{
    background:#111824;
    border:1px solid #1d2938;
    border-radius:14px;
    padding:15px;
}

.label{
    color:#8794a8;
    font-size:13px;
}

.num{
    font-size:25px;
    font-weight:700;
    margin-top:8px;
}

.up{
    color:#ff4d67;
}

.tablebox{
    background:#111824;
    border:1px solid #1d2938;
    border-radius:14px;
    overflow:auto;
}

table{
    width:100%;
    min-width:760px;
    border-collapse:collapse;
}

th,td{
    padding:13px 11px;
    text-align:left;
    border-bottom:1px solid #1d2938;
}

th{
    color:#8c99ad;
    font-size:13px;
    font-weight:500;
}

.name{
    font-weight:600;
}

.code{
    color:#8794a8;
}

.score{
    display:inline-block;
    min-width:38px;
    text-align:center;
    padding:4px 7px;
    border-radius:7px;
    background:#33220c;
    color:#ffb84d;
    font-weight:700;
}

.footer{
    color:#68758a;
    font-size:12px;
    margin-top:14px;
    line-height:1.7;
}

@media(max-width:700px){

    .cards{
        grid-template-columns:
            repeat(2,1fr);
    }

    h1{
        font-size:24px;
    }
}

</style>

</head>

<body>

<div class="wrap">

<h1>🔥 妖股雷达</h1>

<div class="sub">
短线强势股监测 · 自动刷新
</div>

<div class="status" id="status">
● __SOURCE__
</div>

<div class="cards">

<div class="card">
<div class="label">监测标的</div>
<div class="num" id="total">
__TOTAL__
</div>
</div>

<div class="card">
<div class="label">强势股 ≥ 7%</div>
<div class="num up" id="strong">
__STRONG__
</div>
</div>

<div class="card">
<div class="label">涨停附近</div>
<div class="num up" id="limit">
__LIMIT__
</div>
</div>

<div class="card">
<div class="label">平均雷达评分</div>
<div class="num" id="avg">
__AVG__
</div>
</div>

</div>

<div class="tablebox">

<table>

<thead>

<tr>
<th>代码</th>
<th>名称</th>
<th>现价</th>
<th>涨幅</th>
<th>换手</th>
<th>量比</th>
<th>状态</th>
<th>雷达评分</th>
</tr>

</thead>

<tbody id="rows">

__ROWS__

</tbody>

</table>

</div>

<div class="footer">

最后更新：
<span id="updated">
__TIME__
</span>

<br>

数据来源：行情公开接口 · 自动刷新约60秒

<br>

⚠️ 雷达评分仅用于量化筛选和研究，不构成任何投资建议。

</div>

</div>


<script>

function render(data){

    var rows = "";

    data.forEach(function(x){

        var pct =
            x.pct >= 0
            ? "+" + x.pct.toFixed(2) + "%"
            : x.pct.toFixed(2) + "%";

        rows +=
            "<tr>" +

            "<td class='code'>" +
            x.code +
            "</td>" +

            "<td class='name'>" +
            x.name +
            "</td>" +

            "<td>" +
            Number(x.price).toFixed(2) +
            "</td>" +

            "<td class='up'>" +
            pct +
            "</td>" +

            "<td>" +
            Number(x.turnover).toFixed(2) +
            "%" +
            "</td>" +

            "<td>" +
            Number(x.volratio).toFixed(2) +
            "</td>" +

            "<td>" +
            x.state +
            "</td>" +

            "<td>" +
            "<span class='score'>" +
            x.score +
            "</span>" +
            "</td>" +

            "</tr>";

    });

    document.getElementById("rows").innerHTML = rows;

    var strong =
        data.filter(function(x){
            return x.pct >= 7;
        }).length;

    var limit =
        data.filter(function(x){
            return x.state === "涨停附近";
        }).length;

    var avg = 0;

    if(data.length){

        avg = Math.round(
            data.reduce(
                function(a,b){
                    return a + b.score;
                },
                0
            ) / data.length
        );

    }

    document.getElementById("total")
        .innerText = data.length;

    document.getElementById("strong")
        .innerText = strong;

    document.getElementById("limit")
        .innerText = limit;

    document.getElementById("avg")
        .innerText = avg;
}


async function refresh(){

    try{

        var response =
            await fetch(
                "/api/scanner?t=" +
                Date.now()
            );

        var result =
            await response.json();

        render(result.data);

        document.getElementById("status")
            .innerText =
            "● " + result.source;

        document.getElementById("updated")
            .innerText =
            result.updated_at;

    }catch(error){

        document.getElementById("status")
            .innerText =
            "● 数据刷新失败";

    }

}


// 页面打开时刷新一次
refresh();

// 每60秒自动刷新
setInterval(
    refresh,
    60000
);

</script>

</body>

</html>
"""

    page = page.replace(
        "__SOURCE__",
        source_text
    )

    page = page.replace(
        "__TOTAL__",
        str(len(data))
    )

    page = page.replace(
        "__STRONG__",
        str(strong_count)
    )

    page = page.replace(
        "__LIMIT__",
        str(limit_count)
    )

    page = page.replace(
        "__AVG__",
        str(avg_score)
    )

    page = page.replace(
        "__ROWS__",
        rows
    )

    page = page.replace(
        "__TIME__",
        now
    )

    return page
