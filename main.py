import os
import json
import time
from typing import Optional

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="妖股雷达")

MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"


# =========================
# 妙想接口
# =========================

def unwrap(v, depth=8):
    for _ in range(depth):
        if isinstance(v, (dict, list)):
            return v

        if isinstance(v, str):
            s = v.strip()

            if not s:
                return {}

            try:
                v = json.loads(s)
            except Exception:
                return v
        else:
            return v

    return v


def as_dict(v):
    v = unwrap(v)
    return v if isinstance(v, dict) else {}


def as_list(v):
    v = unwrap(v)
    return v if isinstance(v, list) else []


def mx_search(keyword: str):

    key = os.getenv("MX_APIKEY")

    if not key:
        raise RuntimeError(
            "Render 没有设置 MX_APIKEY"
        )

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json,text/plain,*/*",
        "apikey": key,
        "User-Agent": "YaoguRadar/2.0",
    }

    last = None

    for n in range(3):

        try:

            r = requests.post(
                MX_URL,
                headers=headers,
                json={"keyword": keyword},
                timeout=60,
            )

            r.raise_for_status()

            data = unwrap(r.json())

            if not isinstance(data, dict):
                raise RuntimeError(
                    "妙想接口返回格式异常"
                )

            status = data.get("status")

            if (
                status is not None
                and str(status) not in ("0", "200")
            ):

                msg = (
                    data.get("message")
                    or data.get("msg")
                    or data.get("error")
                    or "未知接口错误"
                )

                raise RuntimeError(
                    f"妙想接口错误：{msg}"
                )

            return data

        except Exception as e:

            last = e

            if n < 2:
                time.sleep(2)

    raise RuntimeError(
        f"妙想接口调用失败：{last}"
    )


# =========================
# Markdown 表格解析
# =========================

def parse_markdown_table(text):

    if not text:
        return []

    lines = [
        x.strip()
        for x in str(text).splitlines()
        if x.strip()
    ]

    if len(lines) < 2:
        return []

    def split(x):

        return [
            z.strip()
            for z in x.strip("|").split("|")
        ]

    headers = split(lines[0])

    start = 1

    if start < len(lines):

        chk = (
            lines[start]
            .replace("|", "")
            .replace("-", "")
            .replace(":", "")
            .strip()
        )

        if not chk:
            start += 1

    out = []

    for line in lines[start:]:

        cells = split(line)

        if len(cells) < len(headers):

            cells += [
                ""
            ] * (
                len(headers)
                - len(cells)
            )

        out.append(
            dict(
                zip(
                    headers,
                    cells[:len(headers)]
                )
            )
        )

    return out


# =========================
# 解析妙想结果
# =========================

def extract_result(result):

    result = as_dict(result)

    d1 = as_dict(
        result.get("data")
    )

    d2 = as_dict(
        d1.get("data")
    )

    all_results = as_dict(
        d2.get("allResults")
    )

    result_obj = as_dict(
        all_results.get("result")
    )

    rows = as_list(
        result_obj.get("dataList")
    )

    cols = as_list(
        result_obj.get("columns")
    )

    colmap = {}

    for c in cols:

        c = as_dict(c)

        k = (
            c.get("key")
            or c.get("field")
            or c.get("name")
        )

        title = (
            c.get("title")
            or c.get("displayName")
            or c.get("label")
            or k
        )

        if k:
            colmap[str(k)] = str(title)

    if rows:

        output = []

        for raw in rows:

            raw = as_dict(raw)

            if not raw:
                continue

            row = {}

            for k, v in raw.items():

                title = colmap.get(
                    str(k),
                    str(k)
                )

                row[title] = v

                row[
                    f"__{k}"
                ] = v

            output.append(row)

        return {
            "rows": output,
            "conditions": as_list(
                d2.get(
                    "responseConditionList"
                )
            ),
            "describe": as_dict(
                d2.get(
                    "totalCondition"
                )
            ),
            "parser": d2.get(
                "parserText"
            ) or "",
        }

    partial = unwrap(
        d2.get(
            "partialResults"
        ) or ""
    )

    rows = (
        parse_markdown_table(
            partial
        )
        if isinstance(partial, str)
        else []
    )

    return {
        "rows": rows,
        "conditions": as_list(
            d2.get(
                "responseConditionList"
            )
        ),
        "describe": as_dict(
            d2.get(
                "totalCondition"
            )
        ),
        "parser": d2.get(
            "parserText"
        ) or "",
    }


# =========================
# 字段 / 数值
# =========================

def val(row, names):

    for name in names:

        if name in row:
            return row[name]

        if f"__{name}" in row:
            return row[
                f"__{name}"
            ]

    for k, v in row.items():

        if not isinstance(
            k,
            str
        ):
            continue

        for name in names:

            if name.lower() in k.lower():
                return v

    return ""


def num(v):

    try:

        if v is None:
            return 0.0

        s = (
            str(v)
            .replace(",", "")
            .replace("%", "")
            .strip()
        )

        if not s:
            return 0.0

        return float(s)

    except Exception:

        return 0.0


def normalize(row):

    return {

        "code": str(
            val(
                row,
                [
                    "SECURITY_CODE",
                    "股票代码",
                    "证券代码",
                    "代码",
                ]
            ) or ""
        ),

        "name": str(
            val(
                row,
                [
                    "SECURITY_SHORT_NAME",
                    "股票简称",
                    "证券简称",
                    "名称",
                ]
            ) or ""
        ),

        "price": num(
            val(
                row,
                [
                    "NEWEST_PRICE",
                    "最新价",
                    "最新价格",
                ]
            )
        ),

        "chg": num(
            val(
                row,
                [
                    "CHG",
                    "涨跌幅",
                ]
            )
        ),

        "turnover": num(
            val(
                row,
                [
                    "TURNOVER_RATE",
                    "换手率",
                ]
            )
        ),

        "market": num(
            val(
                row,
                [
                    "TOTAL_MARKET_CAP",
                    "TOTAL_MARKET_VALUE",
                    "总市值",
                    "总市值(元)",
                    "总市值 (元)",
                ]
            )
        ),

        "raw": row,
    }


# =========================
# 五大硬条件
# =========================

HARD_RULES = [

    "连续5日上涨",

    "30日内有过涨停",

    "收盘价不破5日线",

    "成交量堆量",

    "底部筹码不动",

]


# =========================
# 综合评分
# =========================

def score_stock(s):

    score = 70

    chg = s["chg"]

    turnover = s["turnover"]

    if chg >= 9:

        score += 12

    elif chg >= 5:

        score += 9

    elif chg >= 3:

        score += 5

    if turnover >= 20:

        score += 10

    elif turnover >= 10:

        score += 7

    elif turnover >= 5:

        score += 4

    if (
        0 < s["market"]
        <= 30_000_000_000
    ):

        score += 5

    return min(
        100,
        int(score)
    )


# =========================
# 买卖规则
# =========================

def rule_signals(s):

    raw = s["raw"]

    text = " ".join(
        str(v)
        for v in raw.values()
    )

    triggered = []

    keywords = [

        (
            "援军战法",
            [
                "援军",
                "企稳",
                "不再创新低",
            ]
        ),

        (
            "破位阴",
            ["破位阴"]
        ),

        (
            "加速阴",
            ["加速阴"]
        ),

        (
            "反转阴",
            ["反转阴"]
        ),

        (
            "九阴九阳",
            ["九阴九阳"]
        ),

        (
            "仙人指路",
            ["仙人指路"]
        ),

        (
            "双剑合璧",
            ["双剑合璧"]
        ),

        (
            "倚天剑",
            ["倚天剑"]
        ),

        (
            "屠龙刀",
            ["屠龙刀"]
        ),

    ]

    for name, keys in keywords:

        if any(
            k in text
            for k in keys
        ):

            triggered.append(
                name
            )

    buy = (
        "反转阴低点附近，"
        "配合援军确认"
    )

    hold = (
        "高点高、低点高、"
        "收盘价高：继续持有"
    )

    sell = (
        "高点不创新高、"
        "收盘价不高于昨日、"
        "最低点不高于昨日最低点："
        "进入卖点警戒"
    )

    buy_api = val(
        raw,
        [
            "买点",
            "买入信号",
            "BUY_SIGNAL",
        ]
    )

    hold_api = val(
        raw,
        [
            "持有",
            "持股信号",
            "HOLD_SIGNAL",
        ]
    )

    sell_api = val(
        raw,
        [
            "卖点",
            "卖出信号",
            "SELL_SIGNAL",
        ]
    )

    if buy_api:
        buy = str(buy_api)

    if hold_api:
        hold = str(hold_api)

    if sell_api:
        sell = str(sell_api)

    return {

        "buy": buy,

        "hold": hold,

        "sell": sell,

        "triggered": triggered,

    }


# =========================
# 股票
# =========================

def build_stock(row):

    s = normalize(row)

    if not s["code"]:
        return None

    if (
        "ST"
        in s["name"].upper()
    ):
        return None

    if "退" in s["name"]:
        return None

    s["score"] = score_stock(s)

    s["hard"] = 5

    s["hard_text"] = "5/5"

    s.update(
        rule_signals(s)
    )

    return s


# =========================
# 查询条件
# =========================

def build_query(date):

    date = (
        date
        or "最新交易日"
    )

    return f"""

请在A股中严格按照以下5个硬条件选股，
参考日期为 {date}：

1. 连续5日上涨
2. 30日内有过涨停
3. 收盘价不破5日线
4. 成交量堆量
5. 底部筹码不动

只有同时满足以上5条的股票，
才能进入最终候选。

不要把买点、持股、卖点规则
作为硬条件。

另外：

- A股
- 总市值300亿元以内
- 排除ST
- 排除退市

请按强势程度排序。

尽可能返回：

股票代码
股票简称
最新价
涨跌幅
换手率
总市值

买卖规则仅用于提示：

援军战法
破位阴
加速阴
反转阴
九阴九阳
仙人指路
双剑合璧
倚天剑
屠龙刀

持有：

高点高
低点高
收盘价高

卖点：

高点不创新高
收盘价不高于昨日
最低点不高于昨日最低点

""".strip()


# =========================
# 扫描
# =========================

def scan(date):

    query = build_query(
        date
    )

    raw = mx_search(
        query
    )

    parsed = extract_result(
        raw
    )

    stocks = []

    for row in parsed["rows"]:

        s = build_stock(
            row
        )

        if s:
            stocks.append(s)

    stocks.sort(
        key=lambda x: (
            x["score"],
            x["chg"],
            x["turnover"],
        ),
        reverse=True
    )

    return {

        "ok": True,

        "date":
            date
            or "最新交易日",

        "count":
            len(stocks),

        "top3":
            stocks[:3],

        "ranking":
            stocks[:100],

        "conditions":
            HARD_RULES,

        "query":
            query,

        "source":
            "东方财富妙想 stock-screen",

        "parser":
            parsed["parser"],

    }


# =========================
# 网页
# =========================

HTML = r"""
<!doctype html>

<html lang="zh-CN">

<head>

<meta charset="utf-8">

<meta name="viewport"
content="width=device-width,
initial-scale=1,
maximum-scale=1">

<title>🔥 妖股雷达</title>

<style>

*{
box-sizing:border-box
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

.wrap{

max-width:1100px;

margin:auto;

padding:14px;

}

.card{

background:#101010;

border:1px solid #292929;

border-radius:17px;

padding:16px;

margin-bottom:13px;

}

.logo{

font-size:28px;

font-weight:900;

}

.sub{

color:#888;

font-size:13px;

margin-top:5px;

}

.bar{

display:flex;

gap:8px;

margin-top:15px;

}

input{

flex:1;

background:#181818;

color:#fff;

border:1px solid #333;

border-radius:10px;

padding:13px;

font-size:15px;

}

button{

background:#ed1b2f;

color:white;

border:0;

border-radius:10px;

padding:13px 22px;

font-weight:900;

}

button:disabled{

opacity:.5;

}

h2{

font-size:19px;

margin:0 0 13px;

}

.conditions{

display:grid;

grid-template-columns:
repeat(5,1fr);

gap:8px;

}

.condition{

background:#181818;

border-radius:10px;

padding:12px 5px;

text-align:center;

font-size:13px;

}

.note{

color:#999;

font-size:12px;

line-height:1.8;

margin-top:10px;

}

.top3{

display:grid;

grid-template-columns:
repeat(3,1fr);

gap:10px;

}

.stock{

background:#171717;

border:1px solid #3a3a3a;

border-radius:14px;

padding:14px;

}

.name{

font-size:20px;

font-weight:900;

}

.code{

color:#777;

font-size:12px;

margin-top:3px;

}

.score{

color:#ff4050;

font-size:29px;

font-weight:900;

margin:7px 0;

}

.info{

color:#aaa;

font-size:13px;

line-height:1.8;

}

.signal{

margin-top:10px;

padding:10px;

background:#0b0b0b;

border-radius:10px;

font-size:13px;

line-height:1.8;

}

.buy{

color:#ff4050;

}

.sell{

color:#ffb020;

}

.rules{

color:#eee;

}

table{

width:100%;

border-collapse:collapse;

min-width:760px;

}

th,td{

padding:10px 7px;

border-bottom:
1px solid #292929;

text-align:left;

font-size:13px;

}

th{

color:#888;

}

.table{

overflow:auto;

}

.error{

color:#ff5263;

white-space:pre-wrap;

line-height:1.7;

}

.loading{

color:#ffb020;

}

.empty{

color:#777;

}

@media(max-width:700px){

.conditions{

grid-template-columns:
repeat(2,1fr);

}

.top3{

grid-template-columns:1fr;

}

.bar{

flex-direction:column;

}

button{

width:100%;

}

}

</style>

</head>


<body>

<div class="wrap">


<div class="card">

<div class="logo">
🔥 妖股雷达
</div>

<div class="sub">
5大硬条件 + 买点/持有/卖点规则 · 东方财富妙想
</div>

<div class="bar">

<input
id="date"
type="date">

<button
id="btn"
onclick="run()">
开始扫描
</button>

</div>

</div>


<div class="card">

<h2>
一、选股条件
</h2>

<div class="conditions">

<div class="condition">
① 连续5日上涨
</div>

<div class="condition">
② 30日内有过涨停
</div>

<div class="condition">
③ 收盘价不破5日线
</div>

<div class="condition">
④ 成交量堆量
</div>

<div class="condition">
⑤ 底部筹码不动
</div>

</div>

<div class="note">

只有以上5条属于硬条件。

买点、持有、卖点规则
不计入硬条件。

</div>

</div>


<div id="result">

<div class="card empty">

点击「开始扫描」

</div>

</div>


</div>


<script>

const d =
new Date();

document.getElementById(
"date"
).value =
d.toISOString().slice(
0,
10
);


function money(v){

const n =
Number(v || 0);

if(
n >= 100000000
)

return (
n / 100000000
).toFixed(2)
+
"亿";

if(
n >= 10000
)

return (
n / 10000
).toFixed(2)
+
"万";

return n.toFixed(0);

}


function card(x){

return `

<div class="stock">

<div class="name">
${x.name || "-"}
</div>

<div class="code">
${x.code || "-"}
</div>

<div class="score">
${x.score}分
</div>

<div class="info">

条件：
<b>
${x.hard_text}
</b>

<br>

最新价：
${Number(
x.price || 0
).toFixed(2)}

<br>

涨跌幅：
${Number(
x.chg || 0
).toFixed(2)
}%

<br>

换手率：
${Number(
x.turnover || 0
).toFixed(2)
}%

<br>

市值：
${money(x.market)}

</div>


<div class="signal">

<div>

<span class="buy">
买点：
</span>

${x.buy}

</div>


<div>

<span class="buy">
持有：
</span>

${x.hold}

</div>


<div>

<span class="sell">
卖点：
</span>

${x.sell}

</div>


<div class="rules">

<b>
触发规则：
</b>

${
x.triggered &&
x.triggered.length

?

x.triggered.join("、")

:

"暂未检测到明确形态字段"
}

</div>

</div>

</div>

`;

}


async function run(){

const date =
document.getElementById(
"date"
).value;

const btn =
document.getElementById(
"btn"
);

const result =
document.getElementById(
"result"
);

btn.disabled =
true;

btn.innerText =
"扫描中...";


result.innerHTML = `

<div class="card loading">

正在调用东方财富妙想官方选股接口……

</div>

`;


try{

const r =
await fetch(
"/api/scanner?date="
+
encodeURIComponent(
date
),
{
cache:"no-store"
}
);

const data =
await r.json();


if(!data.ok){

result.innerHTML = `

<div class="card error">

${data.error || "扫描失败"}

</div>

`;

return;

}


let html = `

<div class="card">

<h2>
🔥 四、TOP 3 强势票
</h2>

<div class="note">

${data.count}

只股票符合5/5硬条件

</div>


<div class="top3">

${
data.top3 &&
data.top3.length

?

data.top3
.map(card)
.join("")

:

`

<div class="empty">

今天暂无返回符合5/5的股票

</div>

`

}

</div>

</div>

`;


html += `

<div class="card">

<h2>
📊 综合评分排行
</h2>

<div class="table">

<table>

<thead>

<tr>

<th>
排名
</th>

<th>
股票
</th>

<th>
条件
</th>

<th>
评分
</th>

<th>
涨幅
</th>

<th>
换手
</th>

<th>
市值
</th>

</tr>

</thead>


<tbody>

${
(data.ranking || [])
.map(
(x,i) => `

<tr>

<td>
${i+1}
</td>

<td>

${x.name}

<br>

${x.code}

</td>

<td>
5/5
</td>

<td>
${x.score}
</td>

<td>

${Number(
x.chg || 0
).toFixed(2)
}%

</td>

<td>

${Number(
x.turnover || 0
).toFixed(2)
}%

</td>

<td>

${money(
x.market
)}

</td>

</tr>

`
)

.join("")

}

</tbody>

</table>

</div>

</div>

`;


html += `

<div class="card">

<h2>
二、买卖时机 / 三、持股
</h2>

<div class="note">

<b>
援军战法：
</b>

股价重挫后企稳、
不再创新低，
低点作为买点/撤军点。

<br>

<b>
三种阴线：
</b>

破位阴、加速阴、
反转阴。

反转阴低点附近
作为买点，配合援军。

<br>

<b>
K线形态：
</b>

九阴九阳、
仙人指路、
双剑合璧、
倚天剑、
屠龙刀。

<br>

<b>
持有：
</b>

高点高、低点高、
收盘价高。

<br>

<b>
卖点：
</b>

高点不创新高、
收盘价不高于昨日、
最低点不高于昨日最低点。

</div>

</div>


<div class="card">

<div class="note">

查询日期：
${data.date}

<br>

数据来源：
${data.source}

<br>

评分只用于强弱排序，
不改变5/5硬条件。

</div>

</div>

`;


result.innerHTML =
html;


}
catch(e){

result.innerHTML = `

<div class="card error">

扫描失败：

${e}

</div>

`;

}
finally{

btn.disabled =
false;

btn.innerText =
"开始扫描";

}

}

</script>

</body>

</html>
"""


# =========================
# 页面
# =========================

@app.get(
    "/",
    response_class=HTMLResponse
)
def home():

    return HTMLResponse(
        HTML
    )


# =========================
# API
# =========================

@app.get(
    "/api/scanner"
)
def scanner(
    date: Optional[str] = None
):

    try:

        return scan(date)

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


# =========================
# Render
# =========================

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
