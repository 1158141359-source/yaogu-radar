import os
import json
import time
from typing import Optional, Any, Dict, List

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse


app = FastAPI(title="妖股雷达")

MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"


# =========================================================
# 妙想智能选股
# =========================================================

def mx_search(keyword: str) -> Dict[str, Any]:
    api_key = os.getenv("MX_APIKEY")

    if not api_key:
        raise RuntimeError(
            "Render 环境变量 MX_APIKEY 未设置"
        )

    headers = {
        "Content-Type": "application/json",
        "apikey": api_key,
        "User-Agent": "YaoguRadar/1.0",
    }

    payload = {
        "keyword": keyword
    }

    last_error = None

    for i in range(3):

        try:

            r = requests.post(
                MX_URL,
                headers=headers,
                json=payload,
                timeout=40,
            )

            r.raise_for_status()

            data = r.json()

            if data.get("status") != 0:
                raise RuntimeError(
                    f"妙想接口错误："
                    f"{data.get('message', '')}"
                )

            return data

        except Exception as e:

            last_error = e

            if i < 2:
                time.sleep(2)

    raise RuntimeError(
        f"妙想接口连接失败：{last_error}"
    )


# =========================================================
# 提取妙想结果
# =========================================================

def extract_rows(result: Dict[str, Any]):

    root = result.get("data") or {}
    inner = root.get("data") or {}

    # 官方文档推荐全量 dataList
    all_results = inner.get("allResults") or {}
    result_obj = all_results.get("result") or {}

    rows = result_obj.get("dataList") or []
    columns = result_obj.get("columns") or []

    if rows:

        column_map = {}

        for col in columns:

            if not isinstance(col, dict):
                continue

            key = (
                col.get("field")
                or col.get("name")
                or col.get("key")
            )

            title = (
                col.get("displayName")
                or col.get("title")
                or col.get("label")
                or key
            )

            if key:
                column_map[str(key)] = str(title)

        output = []

        for row in rows:

            if not isinstance(row, dict):
                continue

            x = {}

            for key, value in row.items():

                title = column_map.get(
                    str(key),
                    str(key)
                )

                x[title] = value

                # 同时保留原始字段
                x[f"__{key}"] = value

            output.append(x)

        return output

    # =====================================================
    # fallback：partialResults
    # =====================================================

    partial = inner.get("partialResults") or ""

    if isinstance(partial, str) and partial.strip():

        lines = [
            x.strip()
            for x in partial.splitlines()
            if x.strip()
        ]

        if len(lines) >= 2:

            def split_line(line):
                return [
                    x.strip()
                    for x in line.strip("|").split("|")
                ]

            headers = split_line(lines[0])

            start = 1

            if start < len(lines):
                if set(
                    lines[start].replace("|", "")
                ) <= {"-", " ", ":"}:
                    start += 1

            output = []

            for line in lines[start:]:

                cells = split_line(line)

                if len(cells) < len(headers):
                    cells += [""] * (
                        len(headers) - len(cells)
                    )

                row = dict(
                    zip(headers, cells)
                )

                output.append(row)

            return output

    return []


# =========================================================
# 字段识别
# =========================================================

def get_value(row, keys):

    for key in keys:

        if key in row:
            return row[key]

        key2 = f"__{key}"

        if key2 in row:
            return row[key2]

    # 模糊匹配
    for k, v in row.items():

        if not isinstance(k, str):
            continue

        for key in keys:

            if key.lower() in k.lower():
                return v

    return ""


def to_float(v, default=0):

    try:

        if v is None or v == "":
            return default

        s = str(v)
        s = (
            s.replace("%", "")
             .replace(",", "")
             .replace("亿", "")
             .strip()
        )

        return float(s)

    except Exception:
        return default


# =========================================================
# 股票标准化
# =========================================================

def normalize_stock(row):

    code = get_value(
        row,
        [
            "SECURITY_CODE",
            "股票代码",
            "代码",
        ]
    )

    name = get_value(
        row,
        [
            "SECURITY_SHORT_NAME",
            "股票简称",
            "证券简称",
            "名称",
        ]
    )

    price = get_value(
        row,
        [
            "NEWEST_PRICE",
            "最新价",
            "最新价格",
        ]
    )

    chg = get_value(
        row,
        [
            "CHG",
            "涨跌幅",
        ]
    )

    market = get_value(
        row,
        [
            "TOTAL_MARKET_CAP",
            "TOTAL_MARKET_VALUE",
            "总市值",
            "总市值(元)",
        ]
    )

    turnover = get_value(
        row,
        [
            "TURNOVER_RATE",
            "换手率",
        ]
    )

    return {
        "code": str(code or ""),
        "name": str(name or ""),
        "price": to_float(price),
        "chg": to_float(chg),
        "market": to_float(market),
        "turnover": to_float(turnover),
        "raw": row,
    }


# =========================================================
# 分析
# =========================================================

def analyze_rows(rows):

    stocks = []

    for row in rows:

        x = normalize_stock(row)

        if not x["code"]:
            continue

        # 排除明显风险股
        if "ST" in x["name"].upper():
            continue

        if "退" in x["name"]:
            continue

        # 计算综合评分
        score = 0

        # 五大条件由妙想筛选器负责
        score += 50

        if x["chg"] >= 5:
            score += 15

        elif x["chg"] >= 3:
            score += 10

        elif x["chg"] > 0:
            score += 5

        if x["turnover"] >= 20:
            score += 15

        elif x["turnover"] >= 10:
            score += 10

        elif x["turnover"] >= 5:
            score += 5

        if x["market"] > 0:

            # 300亿以内
            if x["market"] <= 300 * 100000000:
                score += 10

        score = min(score, 100)

        x["score"] = score

        # 操作提示
        x["buy"] = (
            "优先等回踩确认，"
            "反转阴低点附近配合援军"
        )

        x["hold"] = (
            "高点高、低点高、收盘高：继续持有"
        )

        x["sell"] = (
            "高点不创新高、收盘不高于前日、"
            "低点跌破前日低点时警戒卖出"
        )

        stocks.append(x)

    stocks.sort(
        key=lambda x: (
            x["score"],
            x["chg"],
            x["turnover"]
        ),
        reverse=True
    )

    return stocks


# =========================================================
# 五条件查询
# =========================================================

def build_keyword(date: Optional[str] = None):

    date_text = date or "最新交易日"

    return f"""
A股，{date_text}附近进行选股。

严格按照以下5个硬条件筛选：

1、连续5个交易日上涨；
2、最近30个交易日内有过涨停；
3、最新收盘价不破5日均线；
4、成交量出现明显堆量；
5、底部筹码保持稳定，不出现明显底部破坏。

另外要求：
市值300亿元以内；
排除ST和退市股票。

请返回符合条件的股票，并按照强势程度排序。

返回股票代码、股票简称、最新价、涨跌幅、
换手率、总市值等可获得行情字段。

不要把买点、持股、卖出规则作为筛选硬条件。
""".strip()


# =========================================================
# 扫描
# =========================================================

def scan(date: Optional[str] = None):

    keyword = build_keyword(date)

    result = mx_search(keyword)

    rows = extract_rows(result)

    stocks = analyze_rows(rows)

    top3 = stocks[:3]

    inner = (
        result.get("data", {})
        .get("data", {})
    )

    return {
        "date": date or "最新交易日",
        "keyword": keyword,
        "count": len(stocks),
        "top3": top3,
        "ranking": stocks[:100],
        "parser_text": inner.get(
            "parserText",
            ""
        ),
        "condition_list": inner.get(
            "responseConditionList",
            []
        ),
        "message": inner.get(
            "totalCondition",
            {}
        ).get(
            "describe",
            ""
        ),
    }


# =========================================================
# HTML
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
    background:#070707;
    color:#eee;
    font-family:
      -apple-system,
      BlinkMacSystemFont,
      "PingFang SC",
      "Microsoft YaHei",
      Arial;
}

.container{
    max-width:1100px;
    margin:auto;
    padding:14px;
}

.header{
    background:#111;
    border:1px solid #292929;
    border-radius:18px;
    padding:18px;
    margin-bottom:14px;
}

.title{
    font-size:27px;
    font-weight:900;
}

.sub{
    margin-top:6px;
    color:#888;
    font-size:13px;
}

.toolbar{
    display:flex;
    gap:8px;
    margin-top:15px;
}

input{
    flex:1;
    min-width:0;
    background:#1b1b1b;
    color:#fff;
    border:1px solid #333;
    border-radius:10px;
    padding:12px;
}

button{
    background:#e51b2b;
    color:#fff;
    border:0;
    border-radius:10px;
    padding:12px 18px;
    font-weight:800;
}

.card{
    background:#111;
    border:1px solid #292929;
    border-radius:16px;
    padding:15px;
    margin-bottom:12px;
}

.title2{
    font-size:18px;
    font-weight:900;
    margin-bottom:12px;
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
    padding:11px 7px;
    text-align:center;
    font-size:13px;
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

.stock strong{
    font-size:20px;
}

.code{
    color:#777;
    font-size:12px;
    margin-top:3px;
}

.score{
    color:#ff4050;
    font-size:28px;
    font-weight:900;
    margin:8px 0;
}

.info{
    color:#aaa;
    line-height:1.8;
    font-size:13px;
}

.signal{
    background:#0c0c0c;
    padding:10px;
    margin-top:10px;
    border-radius:10px;
    line-height:1.8;
    font-size:13px;
}

.good{
    color:#ff4050;
}

.warn{
    color:#ffb020;
}

.table-wrap{
    overflow:auto;
}

table{
    width:100%;
    border-collapse:collapse;
    min-width:650px;
}

th,td{
    padding:10px;
    border-bottom:
      1px solid #292929;
    text-align:left;
    font-size:13px;
}

th{
    color:#888;
}

.loading{
    color:#ffb020;
}

.error{
    color:#ff5263;
    white-space:pre-wrap;
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

    .toolbar{
        flex-direction:column;
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

<div class="sub">
东方财富妙想官方智能选股 · 5大硬条件
</div>

<div class="toolbar">

<input
 id="date"
 type="date">

<button
 id="scan"
 onclick="doScan()">
开始扫描
</button>

</div>

</div>


<div class="card">

<div class="title2">
五大硬条件
</div>

<div class="conditions">

<div class="condition">
① 连续5日上涨
</div>

<div class="condition">
② 30日内有过涨停
</div>

<div class="condition">
③ 收盘不破5日线
</div>

<div class="condition">
④ 堆量成交量
</div>

<div class="condition">
⑤ 底部筹码不动
</div>

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

document.getElementById("date").value =
 d.toISOString().slice(0,10);


function money(v){

    const n = Number(v || 0);

    if(n >= 100000000){
        return (
          n / 100000000
        ).toFixed(2) + "亿";
    }

    if(n >= 10000){
        return (
          n / 10000
        ).toFixed(2) + "万";
    }

    return n.toFixed(0);
}


function card(x){

    return `

    <div class="stock">

        <strong>
            ${x.name || "-"}
        </strong>

        <div class="code">
            ${x.code || "-"}
        </div>

        <div class="score">
            ${x.score}分
        </div>

        <div class="info">

            最新价：
            ${Number(x.price || 0).toFixed(2)}

            <br>

            涨跌幅：
            ${Number(x.chg || 0).toFixed(2)}%

            <br>

            换手率：
            ${Number(x.turnover || 0).toFixed(2)}%

            <br>

            市值：
            ${money(x.market)}

        </div>

        <div class="signal">

            <div>
                <span class="good">
                买点：
                </span>
                ${x.buy}
            </div>

            <div>
                <span class="good">
                持有：
                </span>
                ${x.hold}
            </div>

            <div>
                <span class="warn">
                卖出：
                </span>
                ${x.sell}
            </div>

        </div>

    </div>

    `;
}


async function doScan(){

    const date =
      document.getElementById(
        "date"
      ).value;

    const button =
      document.getElementById(
        "scan"
      );

    const result =
      document.getElementById(
        "result"
      );

    button.disabled = true;
    button.innerText = "扫描中...";

    result.innerHTML = `
      <div class="card loading">
        正在调用东方财富妙想官方选股...
      </div>
    `;

    try{

        const r =
          await fetch(
            "/api/scanner?date="
            +
            encodeURIComponent(date)
          );

        const data =
          await r.json();

        if(data.error){

            result.innerHTML = `
              <div class="card error">
                ${data.error}
              </div>
            `;

            return;
        }

        let html = "";

        html += `
        <div class="card">

            <div class="title2">
                TOP3 强势标的
            </div>

            <div class="top3">

              ${
                data.top3.length
                ?
                data.top3
                  .map(card)
                  .join("")
                :
                `
                <div class="empty">
                今天没有返回符合5大硬条件的股票
                </div>
                `
              }

            </div>

        </div>
        `;


        html += `
        <div class="card">

          <div class="title2">
            综合评分排行
          </div>

          <div class="table-wrap">

          <table>

          <thead>

          <tr>
            <th>排名</th>
            <th>股票</th>
            <th>评分</th>
            <th>最新价</th>
            <th>涨幅</th>
            <th>换手率</th>
            <th>市值</th>
          </tr>

          </thead>

          <tbody>

          ${
            data.ranking
              .map((x,i)=>`

              <tr>

                <td>${i+1}</td>

                <td>
                  ${x.name}
                  <br>
                  ${x.code}
                </td>

                <td>
                  ${x.score}
                </td>

                <td>
                  ${Number(
                    x.price || 0
                  ).toFixed(2)}
                </td>

                <td>
                  ${Number(
                    x.chg || 0
                  ).toFixed(2)}%
                </td>

                <td>
                  ${Number(
                    x.turnover || 0
                  ).toFixed(2)}%
                </td>

                <td>
                  ${money(x.market)}
                </td>

              </tr>

              `)
              .join("")
          }

          </tbody>

          </table>

          </div>

        </div>
        `;


        html += `
        <div class="card">

          <div class="info">

            返回股票：
            ${data.count}

            <br><br>

            妙想解析条件：

            ${
              data.message
              || "已按5大硬条件提交"
            }

            <br><br>

            ${data.parser_text || ""}

          </div>

        </div>
        `;

        result.innerHTML = html;

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

        button.disabled = false;
        button.innerText = "开始扫描";

    }

}

</script>

</body>

</html>
"""


# =========================================================
# 路由
# =========================================================

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)


@app.get("/api/scanner")
def scanner(
    date: Optional[str] = None
):

    try:

        data = scan(date)

        return data

    except Exception as e:

        return {
            "error":
                "妙想官方选股接口调用失败："
                + str(e)
        }


# =========================================================
# Render
# =========================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.environ.get(
            "PORT",
            "8000"
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
