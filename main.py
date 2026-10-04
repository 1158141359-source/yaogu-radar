import os
import json
import time
from typing import Optional, Any, Dict, List

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse


# =========================================================
# 妖股雷达
# 东方财富妙想智能选股
# =========================================================

app = FastAPI(title="妖股雷达")

MX_URL = (
    "https://mkapi2.dfcfs.com/"
    "finskillshub/api/claw/stock-screen"
)


# =========================================================
# 通用 JSON 解包
# =========================================================

def unwrap_json(value, max_depth=8):
    """
    妙想接口某些情况下会出现：
        dict
        ↓
        data = '{"data": {...}}'
        ↓
        再一层 JSON 字符串

    所以这里统一自动解包。
    """

    current = value

    for _ in range(max_depth):

        if isinstance(current, (dict, list)):
            return current

        if isinstance(current, str):

            text = current.strip()

            if not text:
                return {}

            try:
                current = json.loads(text)
                continue
            except Exception:
                return current

        return current

    return current


def as_dict(value):
    value = unwrap_json(value)

    if isinstance(value, dict):
        return value

    return {}


def as_list(value):
    value = unwrap_json(value)

    if isinstance(value, list):
        return value

    return []


# =========================================================
# 妙想 API
# =========================================================

def mx_search(keyword: str):

    api_key = os.getenv("MX_APIKEY")

    if not api_key:
        raise RuntimeError(
            "没有找到 MX_APIKEY。\n"
            "请到 Render → Environment 确认已经设置 MX_APIKEY。"
        )

    headers = {
        "Content-Type": "application/json",
        "apikey": api_key,
        "User-Agent": "YaoguRadar/1.0",
        "Accept": "application/json,text/plain,*/*",
    }

    payload = {
        "keyword": keyword
    }

    last_error = None

    for attempt in range(3):

        try:

            response = requests.post(
                MX_URL,
                headers=headers,
                json=payload,
                timeout=60,
            )

            response.raise_for_status()

            # 第一次解析
            raw = response.json()

            # 防止接口返回字符串 JSON
            result = unwrap_json(raw)

            if not isinstance(result, dict):

                raise RuntimeError(
                    "妙想接口返回格式异常："
                    + str(type(result).__name__)
                )

            # -------------------------------------------------
            # 官方顶层 status
            # 0 = 成功
            # -------------------------------------------------

            status = result.get("status")

            if status is not None:

                try:
                    status_num = int(status)
                except Exception:
                    status_num = status

                if status_num != 0:

                    message = (
                        result.get("message")
                        or result.get("msg")
                        or result.get("error")
                        or "未知接口错误"
                    )

                    raise RuntimeError(
                        f"妙想接口错误：{message}"
                    )

            return result

        except Exception as e:

            last_error = e

            if attempt < 2:
                time.sleep(2)

    raise RuntimeError(
        f"妙想接口调用失败：{last_error}"
    )


# =========================================================
# 官方结果解析
# =========================================================

def extract_data(result):

    result = as_dict(result)

    # -------------------------------------------------------
    # 官方结构：
    #
    # data
    #   └── data
    #        ├── allResults
    #        │    └── result
    #        │         ├── columns
    #        │         └── dataList
    #        │
    #        ├── partialResults
    #        ├── responseConditionList
    #        ├── totalCondition
    #        └── parserText
    # -------------------------------------------------------

    data = as_dict(
        result.get("data")
    )

    inner = as_dict(
        data.get("data")
    )

    # =======================================================
    # 读取条件说明
    # =======================================================

    condition_list = as_list(
        inner.get(
            "responseConditionList"
        )
    )

    total_condition = as_dict(
        inner.get(
            "totalCondition"
        )
    )

    parser_text = inner.get(
        "parserText",
        ""
    )

    # =======================================================
    # ① 优先 dataList
    # =======================================================

    all_results = as_dict(
        inner.get("allResults")
    )

    result_obj = as_dict(
        all_results.get("result")
    )

    data_list = as_list(
        result_obj.get("dataList")
    )

    columns = as_list(
        result_obj.get("columns")
    )

    if data_list:

        # ---------------------------------------------------
        # columns 官方字段：
        # key / title / displayName / dateMsg
        # ---------------------------------------------------

        column_map = {}
        column_order = []

        for col in columns:

            if not isinstance(col, dict):
                continue

            key = (
                col.get("key")
                or col.get("field")
                or col.get("name")
            )

            title = (
                col.get("title")
                or col.get("displayName")
                or col.get("label")
                or key
            )

            date_msg = col.get(
                "dateMsg",
                ""
            )

            if date_msg:
                title = (
                    str(title)
                    + " "
                    + str(date_msg)
                )

            if key:

                key = str(key)
                title = str(title)

                column_map[key] = title
                column_order.append(key)

        rows = []

        for raw_row in data_list:

            raw_row = as_dict(
                raw_row
            )

            if not raw_row:
                continue

            row = {}

            # 按 columns 顺序
            for key in column_order:

                if key not in raw_row:
                    continue

                value = raw_row.get(key)

                title = column_map.get(
                    key,
                    key
                )

                row[title] = (
                    format_value(value)
                )

                # 保留英文原字段
                row[f"__{key}"] = value

            # columns 没有定义的字段也保留
            for key, value in raw_row.items():

                if key in column_order:
                    continue

                row[key] = format_value(
                    value
                )

                row[f"__{key}"] = value

            rows.append(row)

        return {
            "rows": rows,
            "source": "dataList",
            "condition_list": condition_list,
            "total_condition": total_condition,
            "parser_text": parser_text,
            "total": len(rows),
        }

    # =======================================================
    # ② fallback：partialResults
    # =======================================================

    partial = inner.get(
        "partialResults",
        ""
    )

    partial = unwrap_json(
        partial
    )

    if isinstance(partial, str):

        rows = parse_markdown_table(
            partial
        )

        return {
            "rows": rows,
            "source": "partialResults",
            "condition_list": condition_list,
            "total_condition": total_condition,
            "parser_text": parser_text,
            "total": len(rows),
        }

    # =======================================================
    # ③ 没有结果
    # =======================================================

    return {
        "rows": [],
        "source": "",
        "condition_list": condition_list,
        "total_condition": total_condition,
        "parser_text": parser_text,
        "total": 0,
    }


# =========================================================
# 数据格式化
# =========================================================

def format_value(value):

    if value is None:
        return ""

    if isinstance(
        value,
        (dict, list)
    ):
        return json.dumps(
            value,
            ensure_ascii=False
        )

    return str(value)


# =========================================================
# Markdown 表格解析
# =========================================================

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

    def split_line(line):

        return [
            x.strip()
            for x in line.strip("|").split("|")
        ]

    headers = split_line(
        lines[0]
    )

    if not headers:
        return []

    start = 1

    # 跳过 |---|---|
    if start < len(lines):

        check = (
            lines[start]
            .replace("|", "")
            .replace("-", "")
            .replace(":", "")
            .strip()
        )

        if not check:
            start += 1

    rows = []

    for line in lines[start:]:

        cells = split_line(
            line
        )

        if len(cells) < len(headers):

            cells += [
                ""
            ] * (
                len(headers)
                - len(cells)
            )

        if len(cells) > len(headers):

            cells = cells[
                :len(headers)
            ]

        rows.append(
            dict(
                zip(
                    headers,
                    cells
                )
            )
        )

    return rows


# =========================================================
# 字段读取
# =========================================================

def get_value(row, keys):

    if not isinstance(row, dict):
        return ""

    # 直接字段
    for key in keys:

        if key in row:
            return row[key]

    # 原始英文字段
    for key in keys:

        raw_key = f"__{key}"

        if raw_key in row:
            return row[raw_key]

    # 模糊匹配
    for actual_key, value in row.items():

        if not isinstance(
            actual_key,
            str
        ):
            continue

        for key in keys:

            if (
                key.lower()
                in actual_key.lower()
            ):
                return value

    return ""


def number(value):

    try:

        if value is None:
            return 0.0

        text = str(value).strip()

        if not text:
            return 0.0

        text = (
            text
            .replace(",", "")
            .replace("%", "")
            .replace("元", "")
            .replace("亿", "")
            .replace("万", "")
        )

        return float(text)

    except Exception:
        return 0.0


# =========================================================
# 股票标准化
# =========================================================

def normalize_stock(row):

    code = get_value(
        row,
        [
            "SECURITY_CODE",
            "股票代码",
            "证券代码",
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
            "最新价 (元)",
            "最新价格",
        ]
    )

    chg = get_value(
        row,
        [
            "CHG",
            "涨跌幅",
            "涨跌幅 (%)",
        ]
    )

    turnover = get_value(
        row,
        [
            "TURNOVER_RATE",
            "换手率",
            "换手率 (%)",
        ]
    )

    market = get_value(
        row,
        [
            "TOTAL_MARKET_CAP",
            "TOTAL_MARKET_VALUE",
            "TOTAL_MARKET_VALUE",
            "总市值",
            "总市值(元)",
            "总市值 (元)",
        ]
    )

    return {
        "code": str(code or ""),
        "name": str(name or ""),
        "price": number(price),
        "chg": number(chg),
        "turnover": number(turnover),
        "market": number(market),
        "raw": row,
    }


# =========================================================
# 综合评分
# =========================================================

def calculate_score(stock):

    score = 80

    chg = stock["chg"]
    turnover = stock["turnover"]

    # 涨幅
    if chg >= 9:
        score += 10
    elif chg >= 5:
        score += 7
    elif chg >= 3:
        score += 4

    # 换手
    if turnover >= 20:
        score += 10
    elif turnover >= 10:
        score += 7
    elif turnover >= 5:
        score += 4

    return min(
        100,
        int(score)
    )


# =========================================================
# 操作提示
# =========================================================

def add_signals(stock):

    stock["buy"] = (
        "反转阴低点附近，"
        "等待援军确认后考虑"
    )

    stock["hold"] = (
        "高点高、低点高、"
        "收盘高：继续持有"
    )

    stock["sell"] = (
        "高点不创新高、"
        "收盘不高于前日、"
        "低点跌破前日低点时警戒"
    )

    return stock


# =========================================================
# 分析结果
# =========================================================

def analyze_rows(rows):

    stocks = []

    for row in rows:

        stock = normalize_stock(
            row
        )

        if not stock["code"]:
            continue

        name_upper = (
            stock["name"]
            .upper()
        )

        # 排除 ST
        if "ST" in name_upper:
            continue

        # 排除退市
        if "退" in stock["name"]:
            continue

        stock["score"] = (
            calculate_score(
                stock
            )
        )

        stock = add_signals(
            stock
        )

        stocks.append(
            stock
        )

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
# 选股语句
# =========================================================

def build_query(date):

    date_text = (
        date
        if date
        else "最新交易日"
    )

    return f"""
请在A股中严格执行以下5个核心硬条件进行选股，
参考日期为 {date_text}：

① 连续5个交易日上涨；
② 最近30个交易日内至少出现过一次涨停；
③ 最新收盘价不破5日均线；
④ 成交量出现明显堆量；
⑤ 底部筹码保持稳定，没有明显底部破坏。

同时要求：

- A股；
- 总市值300亿元以内；
- 排除ST；
- 排除退市股票；
- 按综合强势程度从高到低排序。

这5条是唯一硬条件。

不要把买点、持有、卖出规则当成硬条件。

请尽可能返回：
股票代码、股票简称、最新价、涨跌幅、
换手率、总市值。

请只返回符合以上5条条件的股票。
""".strip()


# =========================================================
# 扫描
# =========================================================

def run_scan(date=None):

    query = build_query(
        date
    )

    result = mx_search(
        query
    )

    parsed = extract_data(
        result
    )

    rows = parsed["rows"]

    stocks = analyze_rows(
        rows
    )

    top3 = stocks[:3]

    return {
        "ok": True,

        "date": (
            date
            or "最新交易日"
        ),

        "count": len(stocks),

        "top3": top3,

        "ranking": stocks[:100],

        "source": parsed[
            "source"
        ],

        "condition_list": (
            parsed[
                "condition_list"
            ]
        ),

        "total_condition": (
            parsed[
                "total_condition"
            ]
        ),

        "parser_text": (
            parsed[
                "parser_text"
            ]
        ),

        "query": query,
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

.logo{
    font-size:28px;
    font-weight:900;
}

.sub{
    color:#888;
    margin-top:5px;
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
    background:#191919;
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
    font-weight:800;
    font-size:15px;
}

button:disabled{
    opacity:.5;
}

.card{
    background:#101010;
    border:1px solid #292929;
    border-radius:17px;
    padding:16px;
    margin-bottom:13px;
}

.title{
    font-size:19px;
    font-weight:900;
    margin-bottom:13px;
}

.conditions{
    display:grid;
    grid-template-columns:
    repeat(5,1fr);
    gap:8px;
}

.condition{
    background:#171717;
    border-radius:10px;
    padding:12px 6px;
    text-align:center;
    font-size:13px;
}

.note{
    color:#999;
    font-size:12px;
    line-height:1.7;
    margin-top:13px;
}

.top3{
    display:grid;
    grid-template-columns:
    repeat(3,1fr);
    gap:10px;
}

.stock{
    background:#171717;
    border:1px solid #393939;
    border-radius:14px;
    padding:14px;
}

.stock-name{
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
    line-height:1.85;
    font-size:13px;
}

.signal{
    margin-top:10px;
    padding:10px;
    border-radius:9px;
    background:#0c0c0c;
    font-size:13px;
    line-height:1.8;
}

.buy{
    color:#ff4758;
}

.sell{
    color:#ffb020;
}

.table-wrap{
    overflow:auto;
}

table{
    width:100%;
    min-width:680px;
    border-collapse:collapse;
}

th,td{
    padding:10px 7px;
    border-bottom:1px solid #292929;
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
    line-height:1.7;
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

    button{
        width:100%;
    }

}

</style>

</head>


<body>

<div class="container">


<div class="header">

<div class="logo">
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
onclick="scan()">
开始扫描
</button>

</div>

</div>


<div class="card">

<div class="title">
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

<div class="note">
以上5条才属于硬条件。
⑤“底部筹码不动”由公开数据进行代理判断，
不能等同于券商真实筹码分布。
</div>

</div>


<div id="result">

<div class="card empty">
点击「开始扫描」
</div>

</div>


</div>


<script>

const now =
new Date();

document.getElementById(
"date"
).value =
now.toISOString().slice(
0,10
);


function money(v){

    const n =
    Number(v || 0);

    if(n >= 100000000){

        return (
            n / 100000000
        ).toFixed(2)
        + "亿";
    }

    if(n >= 10000){

        return (
            n / 10000
        ).toFixed(2)
        + "万";
    }

    return n.toFixed(0);
}


function stockCard(x){

    return `

    <div class="stock">

        <div class="stock-name">
            ${x.name || "-"}
        </div>

        <div class="code">
            ${x.code || "-"}
        </div>

        <div class="score">
            ${x.score}分
        </div>

        <div class="info">

            最新价：
            ${Number(
                x.price || 0
            ).toFixed(2)}

            <br>

            涨跌幅：
            ${Number(
                x.chg || 0
            ).toFixed(2)}%

            <br>

            换手率：
            ${Number(
                x.turnover || 0
            ).toFixed(2)}%

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
                卖出：
                </span>
                ${x.sell}
            </div>

        </div>

    </div>

    `;
}


function conditionsHtml(list){

    if(!list ||
       !list.length){

        return "";
    }

    return `

    <div class="card">

        <div class="title">
            妙想条件解析
        </div>

        ${
            list.map(
                x => {

                    if(typeof x ===
                       "string"){

                        return `
                        <div class="note">
                        ${x}
                        </div>
                        `;
                    }

                    return `
                    <div class="note">
                    ${
                        x.describe
                        || x.title
                        || ""
                    }

                    ${
                        x.stockCount !==
                        undefined
                        ?
                        " · 匹配 "
                        + x.stockCount
                        + " 只"
                        :
                        ""
                    }

                    </div>
                    `;

                }
            ).join("")
        }

    </div>

    `;
}


async function scan(){

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

    button.disabled =
    true;

    button.innerText =
    "扫描中...";

    result.innerHTML = `

    <div class="card loading">

        正在调用东方财富妙想官方选股接口……

    </div>

    `;

    try{

        const response =
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
        await response.json();

        if(!data.ok){

            result.innerHTML = `

            <div class="card error">

                ${data.error || "扫描失败"}

            </div>

            `;

            return;
        }


        let html = "";


        html += `

        <div class="card">

            <div class="title">
                🔥 TOP 3 强势票
            </div>

            <div class="note">
                ${data.count}
                只股票符合妙想官方5大硬条件
            </div>

            <div class="top3">

                ${
                    data.top3 &&
                    data.top3.length

                    ?

                    data.top3
                    .map(stockCard)
                    .join("")

                    :

                    `
                    <div class="empty">
                    今天暂无符合5大硬条件的股票
                    </div>
                    `
                }

            </div>

        </div>

        `;


        html += `

        <div class="card">

            <div class="title">
                📊 综合评分排行
            </div>

            <div class="table-wrap">

            <table>

            <thead>

            <tr>

                <th>排名</th>
                <th>股票</th>
                <th>条件</th>
                <th>评分</th>
                <th>最新价</th>
                <th>涨幅</th>
                <th>换手</th>
                <th>市值</th>

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


        html += conditionsHtml(
            data.condition_list
        );


        html += `

        <div class="card">

            <div class="note">

                查询日期：
                ${data.date}

                <br>

                数据来源：
                东方财富妙想官方智能选股

                <br>

                接口返回：
                ${data.source || "-"}

                <br>

                ${
                    data.total_condition &&
                    data.total_condition.describe
                    ?
                    "组合条件："
                    +
                    data.total_condition.describe
                    :
                    ""
                }

            </div>

        </div>

        `;


        result.innerHTML =
        html;

    }
    catch(error){

        result.innerHTML = `

        <div class="card error">

            扫描失败：

            ${error}

            <br><br>

            请重新点击「开始扫描」。

        </div>

        `;

    }
    finally{

        button.disabled =
        false;

        button.innerText =
        "开始扫描";

    }

}

</script>

</body>

</html>
"""


# =========================================================
# 页面
# =========================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def home():

    return HTMLResponse(
        HTML
    )


# =========================================================
# API
# =========================================================

@app.get(
    "/api/scanner"
)
def scanner(
    date: Optional[str] = None
):

    try:

        return run_scan(
            date
        )

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


# =========================================================
# Render 启动
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
