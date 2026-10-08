# -*- coding: utf-8 -*-
import os
import re
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

app = FastAPI(title="视频量价选股器")

API_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"


def api_key():
    key = os.getenv("MX_APIKEY", "").strip()
    if not key:
        raise RuntimeError("未检测到 MX_APIKEY")
    return key


def mx(query):
    r = requests.post(
        API_URL,
        headers={
            "Content-Type": "application/json",
            "apikey": api_key()
        },
        json={
            "keyword": query,
            "pageNo": 1,
            "pageSize": 100
        },
        timeout=90
    )
    r.raise_for_status()
    return r.json()


def find_lists(obj):
    result = []

    def walk(x, deep=0):
        if deep > 8:
            return

        if isinstance(x, dict):
            if isinstance(x.get("dataList"), list):
                result.append(x)

            if isinstance(x.get("data"), list):
                result.append({
                    "dataList": x["data"],
                    "columns": x.get("columns", [])
                })

            for v in x.values():
                walk(v, deep + 1)

        elif isinstance(x, list):
            for v in x[:10]:
                walk(v, deep + 1)

    walk(obj)
    return result


def parse(raw):
    if not isinstance(raw, dict):
        return {
            "success": False,
            "message": "接口返回异常",
            "data": []
        }

    nodes = find_lists(raw)

    if not nodes:
        return {
            "success": True,
            "message": "没有找到股票数据",
            "data": []
        }

    node = max(
        nodes,
        key=lambda x: len(x.get("dataList", []))
    )

    rows = node.get("dataList", [])
    columns = node.get("columns", [])

    cmap = {}

    for c in columns:
        if not isinstance(c, dict):
            continue

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
            cmap[str(k)] = str(title)

    stocks = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        def pick(*names):
            for name in names:
                if name in row and row[name] not in ("", None):
                    return row[name]
            return ""

        code = pick(
            "SECURITY_CODE",
            "股票代码",
            "代码",
            "证券代码",
            "SECUCODE"
        )

        name = pick(
            "SECURITY_SHORT_NAME",
            "股票简称",
            "名称",
            "证券简称"
        )

        price = pick(
            "NEWEST_PRICE",
            "最新价",
            "现价"
        )

        pct = pick(
            "CHG",
            "涨跌幅",
            "涨幅",
            "涨跌"
        )

        turnover = pick(
            "TURNOVER_RATE",
            "换手率",
            "换手"
        )

        volume = pick(
            "VOLUME",
            "成交量"
        )

        amount = pick(
            "AMOUNT",
            "成交额"
        )

        extra = {}

        for k, v in row.items():
            extra[cmap.get(str(k), str(k))] = v

        stocks.append({
            "code": str(code),
            "name": str(name),
            "price": price,
            "pct": pct,
            "turnover": turnover,
            "volume": volume,
            "amount": amount,
            "extra": extra
        })

    return {
        "success": True,
        "message": "ok",
        "data": stocks
    }


# ============================================================
# 视频量价选股规则
# ============================================================

QUERY = r"""
筛选今天A股。

只使用视频量价结构评分法。

不要使用：
龙虎榜、连板、市值300亿、
竞价换手率29.25%、竞价涨幅5%、
委卖大于委买。

以下5项各20分：

1、缩量回调
前期上涨或冲高以后出现明显缩量回调。
满足得20分。

2、地量
近期成交量明显进入阶段低位。
满足得20分。

3、止跌
地量以后股价不再持续创新低，
出现止跌、企稳或小平台。
满足得20分。

4、温和放量
止跌以后出现阳线，
成交量比前几日温和增加。
满足得20分。

5、放量突破
明显放量突破近期平台、
前高或者重要压力位。
满足得20分。

不要求全部满足。

至少满足3项，也就是50分，
即可进入候选。

请返回20-50只候选。

必须提供：

股票代码
股票简称
最新价
涨跌幅
成交量
成交额
换手率
缩量回调
地量
止跌
温和放量
放量突破
视频量价结构评分

每项明确写：
是/否
或者
✓/✗

评分范围：
0-100分。

按照评分从高到低排序。
"""


FALLBACK = r"""
筛选今天A股近期量价转强股票。

不要使用龙虎榜、连板、市值、
竞价换手率、竞价涨幅、委卖委买。

重点寻找：

前期上涨
缩量回调
阶段地量
止跌企稳
温和放量
阳线
平台突破
放量突破

5项量价条件：

缩量回调20分
地量20分
止跌20分
温和放量20分
放量突破20分

不要求全部满足。

至少满足3项进入候选。

返回尽可能多的股票。

提供：
代码、简称、最新价、涨跌幅、
成交量、成交额、换手率、
缩量回调、地量、止跌、
温和放量、放量突破、评分。
"""


def get_score(stock):
    text = " ".join(
        str(k) + " " + str(v)
        for k, v in stock.get("extra", {}).items()
    )

    patterns = [
        r"(?:视频量价结构评分|量价结构评分|评分|分数|得分|匹配度)\D{0,10}(\d{1,3})",
        r"(\d{1,3})\s*分"
    ]

    for p in patterns:
        m = re.search(p, text)

        if m:
            n = int(m.group(1))

            if 0 <= n <= 100:
                return n

    score = 0

    for key in [
        "缩量回调",
        "地量",
        "止跌",
        "温和放量",
        "放量突破"
    ]:
        for k, v in stock.get("extra", {}).items():
            if key in str(k):
                if re.search(
                    r"是|✓|√|满足|有",
                    str(v)
                ):
                    score += 20

    return score


def get_tags(stock):
    tags = []

    for key in [
        "缩量回调",
        "地量",
        "止跌",
        "温和放量",
        "放量突破"
    ]:
        for k, v in stock.get("extra", {}).items():
            if key in str(k):
                if re.search(
                    r"是|✓|√|满足|有",
                    str(v)
                ):
                    tags.append(key)

    return tags


def scan():

    errors = []

    for query in [QUERY, FALLBACK]:

        try:
            raw = mx(query)
            result = parse(raw)

            if result["data"]:

                stocks = result["data"]

                for s in stocks:
                    s["score"] = get_score(s)
                    s["tags"] = get_tags(s)

                stocks.sort(
                    key=lambda x: (
                        x["score"],
                        len(x["tags"])
                    ),
                    reverse=True
                )

                strong = [
                    s for s in stocks
                    if s["score"] >= 50
                ]

                if strong:
                    stocks = strong

                return {
                    "success": True,
                    "message": "ok",
                    "data": stocks[:50],
                    "total": len(stocks)
                }

            errors.append(
                result.get("message", "无数据")
            )

        except Exception as e:
            errors.append(str(e))

    return {
        "success": False,
        "message": "；".join(errors),
        "data": []
    }


# ============================================================
# 手机网页
# ============================================================

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
background:#080a0d;
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
background:#11151a;
border:1px solid #252b32;
border-radius:14px;
padding:14px;
margin-bottom:12px
}

.title{
font-size:21px;
font-weight:900
}

.sub{
color:#89939f;
font-size:12px;
margin-top:6px
}

button{
width:100%;
border:0;
border-radius:10px;
padding:13px;
margin-top:12px;
background:#e53935;
color:white;
font-size:16px;
font-weight:900
}

.status{
margin-top:9px;
font-size:12px;
color:#929ca7
}

.steps{
display:grid;
grid-template-columns:
repeat(5,1fr);
gap:6px;
margin-top:12px
}

.step{
background:#181d23;
border-radius:8px;
padding:8px 4px;
text-align:center;
font-size:11px
}

.step b{
display:block;
font-size:13px;
color:white
}

h2{
margin:0 0 12px;
font-size:18px
}

.badge{
font-size:11px;
padding:4px 7px;
background:#18351f;
color:#58db79;
border-radius:6px
}

.top{
display:grid;
grid-template-columns:
repeat(3,1fr);
gap:8px
}

.card{
background:#181d23;
border:1px solid #2b333c;
border-radius:11px;
padding:12px
}

.rank{
color:#ffca45;
font-size:12px
}

.stock{
font-size:17px;
font-weight:900;
margin-top:5px
}

.code{
color:#7f8994;
font-size:11px
}

.score{
font-size:27px;
font-weight:900;
margin-top:5px
}

.info{
font-size:12px;
color:#9ca6b0
}

.tags{
display:flex;
flex-wrap:wrap;
gap:4px;
margin-top:8px
}

.tag{
background:#202831;
padding:4px 6px;
border-radius:5px;
font-size:10px
}

.tablebox{
overflow:auto
}

table{
width:100%;
min-width:850px;
border-collapse:
collapse
}

th,td{
padding:8px 6px;
border-bottom:
1px solid #252b31;
text-align:center;
font-size:11px
}

th{
color:#9da7b2;
background:#151a20
}

.name{
text-align:left;
font-weight:800
}

.good{
color:#51dc76;
font-weight:900
}

.up{
color:#ff4d4d
}

.down{
color:#45db7a
}

.muted{
color:#68727d
}

.empty{
text-align:center;
padding:28px;
color:#808a95
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
缩量回调 → 地量 → 止跌 → 温和放量 → 突破
｜评分制，不要求全部满足
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
点击开始获取今日候选
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
<span class="badge">
≥50分
</span>
</h2>

<div class="tablebox">

<table>

<thead>

<tr>

<th>代码</th>
<th>名称</th>
<th>评分</th>
<th>满足</th>
<th>缩量回调</th>
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
<td colspan="11"
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

function yes(v){

return /是|✓|√|满足|有/
.test(String(v||""))

}


function val(s,name){

for(
const k of Object.keys(
s.extra||{}
)){

if(k.includes(name))
return s.extra[k]

}

return ""

}


function score(s){

return Number(s.score||0)

}


function renderTop(data){

let top=data.slice(0,3)

document.getElementById(
"topCount"
).textContent=
top.length+"只"

if(!top.length){

document.getElementById(
"top"
).innerHTML=
'<div class="empty">暂无≥50分候选</div>'

return

}

document.getElementById(
"top"
).innerHTML=
top.map((s,i)=>`

<div class="card">

<div class="rank">
${["🥇 TOP 1","🥈 TOP 2","🥉 TOP 3"][i]}
</div>

<div class="stock">
${s.name||"--"}
<span class="code">
${s.code||""}
</span>
</div>

<div class="score">
${score(s)}分
</div>

<div class="info">
${s.price||"--"}
　${s.pct||"--"}
</div>

<div class="tags">

${(s.tags||[]).map(
x=>`
<span class="tag">
✓ ${x}
</span>
`
).join("")}

</div>

</div>

`).join("")

}


function renderTable(data){

let tb=
document.getElementById("tbody")

if(!data.length){

tb.innerHTML=
`
<tr>
<td colspan="11"
class="empty">
没有满足50分的候选
</td>
</tr>
`

return

}

let fields=[
"缩量回调",
"地量",
"止跌",
"温和放量",
"放量突破"
]

tb.innerHTML=
data.map(s=>`

<tr>

<td>${s.code||"--"}</td>

<td class="name">
${s.name||"--"}
</td>

<td class="good">
${score(s)}
</td>

<td>
${(s.tags||[]).length}/5
</td>

${fields.map(
f=>{

let v=yes(
val(s,f)
)

return `
<td class="${v?"good":"muted"}">
${v?"✓":"—"}
</td>
`

}
).join("")}

<td>
${s.pct||"--"}
</td>

<td>
${s.turnover||
val(s,"换手率")||
"--"}
</td>

</tr>

`).join("")

}


async function scan(){

let status=
document.getElementById(
"status"
)

status.textContent=
"正在读取东方财富妙想数据…"

document.getElementById(
"top"
).innerHTML=
'<div class="empty">扫描中…</div>'

try{

let r=
await fetch(
"/api/scan",
{cache:"no-store"}
)

let j=await r.json()

if(!j.success){

status.textContent=
"扫描失败："+j.message

return

}

let data=j.data||[]

let strong=
data.filter(
x=>score(x)>=50
)

status.textContent=
"扫描完成："+data.length+
"只，≥50分："+strong.length+"只"

renderTop(
strong.length?strong:data
)

renderTable(
strong.length?strong:data
)

}catch(e){

status.textContent=
"服务器错误："+e

}

}

</script>

</body>

</html>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTML


@app.get("/api/scan")
def api_scan():
    return scan()


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":

    port = int(
        os.getenv("PORT", "8000")
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )