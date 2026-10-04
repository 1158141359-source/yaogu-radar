# -*- coding: utf-8 -*-

import os
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# ============================================================
# 基础配置
# ============================================================

app = FastAPI(title="视频量价选股器")

# 东方财富妙想智能选股接口
MX_API_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"


# ============================================================
# API KEY
# ============================================================

def get_api_key():
    """
    从环境变量读取东方财富妙想 API Key。

    Render 部署时：
    Key = MX_APIKEY
    Value = 你的东方财富妙想 API Key
    """

    key = os.getenv("MX_APIKEY")

    if not key:
        raise RuntimeError(
            "未检测到 MX_APIKEY，请在服务器环境变量中设置东方财富妙想 API Key。"
        )

    return key


# ============================================================
# 调用东方财富妙想
# ============================================================

def mx_select(query: str):

    api_key = get_api_key()

    headers = {
        "Content-Type": "application/json",
        "apikey": api_key
    }

    payload = {
        "keyword": query
    }

    response = requests.post(
        MX_API_URL,
        headers=headers,
        json=payload,
        timeout=60
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# 解析东方财富返回数据
# ============================================================

def parse_result(result):

    if not isinstance(result, dict):
        return {
            "success": False,
            "message": "东方财富接口返回格式异常",
            "data": []
        }

    if result.get("status") != 0:

        return {
            "success": False,
            "message": result.get("message") or "东方财富接口返回异常",
            "data": []
        }

    data = result.get("data", {})

    if not isinstance(data, dict):
        data = {}

    inner = data.get("data", {})

    if not isinstance(inner, dict):
        inner = {}

    all_results = inner.get("allResults", {})

    if not isinstance(all_results, dict):
        all_results = {}

    result_data = all_results.get("result", {})

    if not isinstance(result_data, dict):
        result_data = {}

    data_list = result_data.get("dataList", [])

    if not isinstance(data_list, list):
        data_list = []

    columns = result_data.get("columns", [])

    if not isinstance(columns, list):
        columns = []

    # --------------------------------------------------------
    # 建立字段中文名称映射
    # --------------------------------------------------------

    column_map = {}

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

        if key:
            column_map[str(key)] = str(title)

    # --------------------------------------------------------
    # 解析股票
    # --------------------------------------------------------

    stocks = []

    for row in data_list:

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
            or 0
        )

        pct = (
            row.get("CHG")
            or row.get("涨跌幅")
            or row.get("涨幅")
            or 0
        )

        extra = {}

        for key, value in row.items():

            cn_name = column_map.get(
                str(key),
                str(key)
            )

            extra[cn_name] = value

        stocks.append({
            "code": str(code),
            "name": str(name),
            "price": price,
            "pct": pct,
            "extra": extra
        })

    # --------------------------------------------------------
    # 如果没有结构化结果，保留部分结果
    # --------------------------------------------------------

    if not stocks:

        partial = inner.get(
            "partialResults",
            ""
        )

        if partial:

            return {
                "success": True,
                "message": "东方财富返回了部分结果",
                "data": [],
                "partial": partial
            }

    return {
        "success": True,
        "message": "ok",
        "data": stocks,
        "total": len(stocks),
        "parserText": inner.get(
            "parserText",
            ""
        ),
        "condition": inner.get(
            "totalCondition",
            {}
        )
    }


# ============================================================
# 视频里的量价选股逻辑
# ============================================================

VIDEO_QUERY = r"""
请按照以下“视频量价交易方法”筛选今天A股股票。

注意：

不要使用以前的“妖股雷达”规则。

不要使用：
龙虎榜硬条件、
连板硬条件、
市值300亿硬条件、
竞价换手率29.25%硬条件、
竞价涨幅5%硬条件、
委卖大于委买硬条件。

本次只按照视频中的“缩量筑底 → 地量 → 止跌 → 温和放量 → 突破”的量价结构选股。

==================================================
第一阶段：前期上涨 / 冲高
==================================================

股票之前应该经历过一段明显上涨，
或者出现过阶段性冲高。

随后出现较明显的回撤或调整。

重点寻找：

前期有上涨空间
+
随后出现明显回调
+
目前处于调整末端或者底部区域。

==================================================
第二阶段：成交量持续缩小
==================================================

调整过程中：

股价回落，
成交量同步持续缩小。

优先寻找：

缩量回调
+
成交量逐渐降低
+
抛压逐渐减弱。

==================================================
第三阶段：出现阶段性地量
==================================================

重点寻找明显的阶段性地量。

要求：

近期成交量明显低于此前上涨阶段；
成交量逐渐接近阶段低位；
出现比较明显的缩量区域。

“地量”不能单独作为买入信号。

==================================================
第四阶段：地量之后止跌
==================================================

地量出现以后：

股价不再持续创新低，
或者低点基本保持稳定。

重点寻找：

地量
+
止跌
+
低点不再明显下移。

说明卖压可能逐渐衰竭。

==================================================
第五阶段：温和放量
==================================================

在地量和止跌之后：

出现阳线；
成交量相比前几日开始温和增加。

重点寻找：

止跌
+
阳线
+
温和放量。

不要寻找已经连续暴涨很多天的股票。

==================================================
第六阶段：放量突破
==================================================

最重要的确认信号：

后续出现明显放量；
同时股价突破：

前期平台，
或者近期重要高点，
或者阶段压力位。

重点寻找：

放量
+
突破平台
+
突破近期高点。

==================================================
综合排序
==================================================

请根据上述量价结构进行综合评分。

优先级：

1. 已经完成缩量筑底、地量、止跌，并出现温和放量；
2. 已经开始放量突破前期平台；
3. 正在接近突破确认；
4. 量价结构完整；
5. 前期上涨明显；
6. 回调充分；
7. 成交量缩减明显；
8. 地量明显；
9. 止跌明显；
10. 突破时成交量明显增加。

特别注意：

“地量”本身不是买入信号。

必须结合：

缩量筑底
+
地量
+
止跌
+
温和放量
+
突破

进行综合判断。

==================================================
输出要求
==================================================

请返回尽可能完整的候选股票列表。

每只股票尽量提供：

股票代码
股票简称
最新价
涨跌幅
成交量
成交额
换手率
近期高点
近期低点
量价变化
以及能够证明其符合：

缩量
地量
止跌
温和放量
突破

的相关指标。

最后按照：

“视频量价结构匹配程度”

从高到低排序。

重点标记最强的TOP 3股票。

不要把单纯下跌后的股票直接判定为强势股。

必须优先考虑已经出现量价转强迹象，
或者正在接近突破确认的股票。
"""


# ============================================================
# 扫描股票
# ============================================================

def scan():

    result = mx_select(VIDEO_QUERY)

    return parse_result(result)


# ============================================================
# HTML 页面
# ============================================================

HTML = r"""
<!DOCTYPE html>

<html lang="zh-CN">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"
>

<title>视频量价选股器</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background:
        radial-gradient(
            circle at top,
            #18202b 0%,
            #080b10 45%,
            #050608 100%
        );

    color: #f2f4f7;

    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "PingFang SC",
        "Microsoft YaHei",
        sans-serif;

    min-height: 100vh;
}

.header {

    position: sticky;

    top: 0;

    z-index: 10;

    padding: 16px;

    background: rgba(5,8,12,0.94);

    backdrop-filter: blur(12px);

    border-bottom: 1px solid #202733;
}

.title {

    font-size: 22px;

    font-weight: 800;

    letter-spacing: 1px;
}

.subtitle {

    margin-top: 6px;

    color: #8d98a8;

    font-size: 12px;
}

.container {

    width: 100%;

    max-width: 1000px;

    margin: auto;

    padding: 14px;
}

.method {

    display: flex;

    gap: 6px;

    overflow-x: auto;

    padding-bottom: 12px;
}

.step {

    flex: 0 0 auto;

    padding: 8px 11px;

    border-radius: 8px;

    background: #111722;

    border: 1px solid #222b37;

    color: #aeb8c6;

    font-size: 12px;
}

.step.active {

    color: #fff;

    border-color: #8b1e2d;

    background: #321019;
}

.toolbar {

    display: flex;

    gap: 10px;

    margin-bottom: 15px;
}

button {

    border: none;

    border-radius: 9px;

    padding: 11px 17px;

    font-size: 14px;

    font-weight: 700;

    color: white;

    background: #b82031;

    cursor: pointer;
}

button:active {

    transform: scale(0.97);
}

.status {

    flex: 1;

    display: flex;

    align-items: center;

    justify-content: center;

    color: #8f9aaa;

    font-size: 12px;

    background: #10151d;

    border-radius: 9px;

    border: 1px solid #202733;
}

.section-title {

    margin: 18px 0 10px;

    font-size: 17px;

    font-weight: 800;
}

.top3 {

    display: grid;

    grid-template-columns:
        repeat(3, 1fr);

    gap: 10px;
}

.card {

    padding: 14px;

    border-radius: 13px;

    background:
        linear-gradient(
            145deg,
            #171c25,
            #0c0f14
        );

    border: 1px solid #2a313c;

    box-shadow:
        0 8px 30px rgba(0,0,0,0.25);
}

.rank {

    font-size: 12px;

    color: #d35a68;

    font-weight: 800;
}

.name {

    margin-top: 6px;

    font-size: 19px;

    font-weight: 900;
}

.code {

    margin-top: 3px;

    color: #737f8e;

    font-size: 11px;
}

.price {

    margin-top: 12px;

    font-size: 23px;

    font-weight: 900;
}

.pct {

    margin-top: 3px;

    font-size: 13px;

    font-weight: 800;
}

.up {

    color: #ff4d5e;
}

.down {

    color: #25c47a;
}

.tags {

    display: flex;

    flex-wrap: wrap;

    gap: 5px;

    margin-top: 12px;
}

.tag {

    padding: 4px 7px;

    border-radius: 5px;

    background: #202733;

    color: #b7c1cd;

    font-size: 10px;
}

.empty {

    padding: 30px 15px;

    text-align: center;

    color: #727e8e;

    background: #0d1117;

    border-radius: 12px;

    border: 1px dashed #29313d;
}

.table-wrap {

    overflow-x: auto;

    border-radius: 12px;

    border: 1px solid #202733;

    background: #0b0e13;
}

table {

    width: 100%;

    min-width: 650px;

    border-collapse: collapse;

    font-size: 12px;
}

th {

    text-align: left;

    color: #8d98a8;

    background: #121720;

    padding: 10px;

    white-space: nowrap;
}

td {

    padding: 10px;

    border-top: 1px solid #1b222c;

    white-space: nowrap;
}

.footer {

    margin: 20px 0 30px;

    color: #606b79;

    text-align: center;

    font-size: 11px;
}

.loading {

    animation:
        pulse 1s infinite;
}

@keyframes pulse {

    50% {
        opacity: 0.45;
    }

}

@media(max-width:650px) {

    .top3 {

        grid-template-columns: 1fr;
    }

    .card {

        padding: 13px;
    }

    .title {

        font-size: 20px;
    }

}

</style>

</head>


<body>


<div class="header">

    <div class="title">
        📈 视频量价选股器
    </div>

    <div class="subtitle">
        缩量筑底 → 地量 → 止跌 → 温和放量 → 突破
    </div>

</div>


<div class="container">


    <div class="method">

        <div class="step active">
            ① 缩量筑底
        </div>

        <div class="step active">
            ② 地量
        </div>

        <div class="step active">
            ③ 止跌
        </div>

        <div class="step active">
            ④ 温和放量
        </div>

        <div class="step active">
            ⑤ 放量突破
        </div>

    </div>


    <div class="toolbar">

        <button onclick="scan()">
            🔍 开始选股
        </button>

        <div
            id="status"
            class="status"
        >
            等待扫描
        </div>

    </div>


    <div class="section-title">
        🔥 视频量价匹配 TOP 3
    </div>


    <div
        id="top3"
        class="top3"
    >

        <div class="empty">
            点击“开始选股”
        </div>

    </div>


    <div class="section-title">
        📊 全部候选
    </div>


    <div class="table-wrap">

        <table>

            <thead>

                <tr>

                    <th>排名</th>

                    <th>代码</th>

                    <th>名称</th>

                    <th>最新价</th>

                    <th>涨跌幅</th>

                    <th>量价指标</th>

                </tr>

            </thead>

            <tbody id="table">

                <tr>

                    <td colspan="6">

                        <div class="empty">
                            暂无数据
                        </div>

                    </td>

                </tr>

            </tbody>

        </table>

    </div>


    <div class="footer">

        数据由东方财富妙想接口提供<br>
        本工具仅用于量价结构分析，不构成投资建议

    </div>


</div>


<script>


function safeText(value) {

    if (
        value === null ||
        value === undefined
    ) {

        return "";

    }

    return String(value);

}


function formatPct(value) {

    if (
        value === null ||
        value === undefined ||
        value === ""
    ) {

        return "--";

    }

    return safeText(value);

}


function renderTop3(list) {

    const box =
        document.getElementById("top3");

    if (
        !list ||
        list.length === 0
    ) {

        box.innerHTML = `
            <div class="empty">
                当前没有返回候选股票
            </div>
        `;

        return;

    }


    const top =
        list.slice(0,3);


    box.innerHTML =
        top.map((item,index) => {

            const pct =
                safeText(item.pct);

            const numericPct =
                parseFloat(
                    String(pct)
                        .replace("%","")
                );

            const cls =
                numericPct >= 0
                ? "up"
                : "down";


            return `

                <div class="card">

                    <div class="rank">
                        TOP ${index + 1}
                    </div>

                    <div class="name">
                        ${safeText(item.name) || "--"}
                    </div>

                    <div class="code">
                        ${safeText(item.code) || "--"}
                    </div>

                    <div class="price">
                        ${safeText(item.price) || "--"}
                    </div>

                    <div class="pct ${cls}">
                        ${pct || "--"}
                    </div>

                    <div class="tags">

                        <span class="tag">
                            缩量筑底
                        </span>

                        <span class="tag">
                            地量
                        </span>

                        <span class="tag">
                            止跌
                        </span>

                        <span class="tag">
                            放量确认
                        </span>

                    </div>

                </div>

            `;

        }).join("");

}


function renderTable(list) {

    const table =
        document.getElementById("table");


    if (
        !list ||
        list.length === 0
    ) {

        table.innerHTML = `

            <tr>

                <td colspan="6">

                    <div class="empty">
                        暂无符合结果
                    </div>

                </td>

            </tr>

        `;

        return;

    }


    table.innerHTML =

        list.map((item,index) => {

            const pct =
                safeText(item.pct);


            return `

                <tr>

                    <td>
                        ${index + 1}
                    </td>

                    <td>
                        ${safeText(item.code)}
                    </td>

                    <td>
                        <b>
                            ${safeText(item.name)}
                        </b>
                    </td>

                    <td>
                        ${safeText(item.price)}
                    </td>

                    <td>
                        ${pct}
                    </td>

                    <td>

                        ${Object.entries(
                            item.extra || {}
                        )
                        .slice(0,5)
                        .map(
                            ([key,value]) =>
                            `${key}: ${value}`
                        )
                        .join("<br>")}

                    </td>

                </tr>

            `;

        }).join("");

}


async function scan() {

    const status =
        document.getElementById("status");


    status.innerHTML =
        '<span class="loading">正在扫描东方财富数据...</span>';


    try {

        const response =
            await fetch(
                "/api/scan",
                {
                    cache: "no-store"
                }
            );


        const result =
            await response.json();


        if (!result.success) {

            throw new Error(
                result.message ||
                "扫描失败"
            );

        }


        const list =
            result.data || [];


        renderTop3(list);

        renderTable(list);


        status.innerHTML =
            `完成：${list.length} 只`;


    } catch(error) {

        console.error(error);


        document.getElementById("top3")
            .innerHTML = `

                <div class="empty">

                    ❌ 扫描失败<br><br>

                    ${safeText(error.message)}

                </div>

            `;


        status.innerHTML =
            "扫描失败";

    }

}


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
def home():

    return HTML


# ============================================================
# API
# ============================================================

@app.get("/api/scan")
def api_scan():

    try:

        return scan()

    except Exception as e:

        return {
            "success": False,
            "message": str(e),
            "data": []
        }


# ============================================================
# 健康检查
# ============================================================

@app.get("/health")
def health():

    return {
        "status": "ok"
    }


# ============================================================
# 启动
# ============================================================

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
