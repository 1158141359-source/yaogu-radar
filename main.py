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

MX_API_URL = (
    "https://mkapi2.dfcfs.com/"
    "finskillshub/api/claw/stock-screen"
)


# ============================================================
# 东方财富妙想 API KEY
# ============================================================

def get_api_key():

    key = os.getenv("MX_APIKEY")

    if not key:
        raise RuntimeError(
            "未检测到 MX_APIKEY，请在服务器环境变量中设置。"
        )

    return key


# ============================================================
# 调用东方财富妙想
# ============================================================

def mx_select(query):

    headers = {
        "Content-Type": "application/json",
        "apikey": get_api_key(),
    }

    payload = {
        "keyword": query,
        "pageNo": 1,
        "pageSize": 100,
    }

    response = requests.post(
        MX_API_URL,
        headers=headers,
        json=payload,
        timeout=60,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# 解析妙想返回数据
# ============================================================

def parse_result(result):

    if not isinstance(result, dict):

        return {
            "success": False,
            "message": "东方财富接口返回格式异常",
            "data": [],
        }

    if result.get("status") != 0:

        return {
            "success": False,
            "message": (
                result.get("message")
                or "东方财富接口返回异常"
            ),
            "data": [],
        }

    data = result.get("data") or {}

    if not isinstance(data, dict):
        data = {}

    inner = data.get("data") or {}

    if not isinstance(inner, dict):
        inner = {}

    # ========================================================
    # 妙想当前返回结构
    #
    # data
    #   └── data
    #        └── result
    #             ├── columns
    #             └── dataList
    # ========================================================

    result_data = inner.get("result") or {}

    if not isinstance(result_data, dict):
        result_data = {}

    data_list = (
        result_data.get("dataList")
        or []
    )

    columns = (
        result_data.get("columns")
        or []
    )

    if not isinstance(data_list, list):
        data_list = []

    if not isinstance(columns, list):
        columns = []

    # ========================================================
    # 建立字段中文名称
    # ========================================================

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

    # ========================================================
    # 股票数据
    # ========================================================

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
            or ""
        )

        pct = (
            row.get("CHG")
            or row.get("涨跌幅")
            or row.get("涨幅")
            or ""
        )

        extra = {}

        for key, value in row.items():

            display_name = column_map.get(
                str(key),
                str(key)
            )

            extra[display_name] = value

        stocks.append(
            {
                "code": str(code),
                "name": str(name),
                "price": price,
                "pct": pct,
                "extra": extra,
            }
        )

    return {
        "success": True,
        "message": "ok",
        "data": stocks,
        "total": len(stocks),
    }


# ============================================================
# 视频量价评分策略
# ============================================================

VIDEO_QUERY = r"""

请筛选今天A股。

严格按照下面的“视频量价结构评分法”。

特别注意：

【不要求全部条件同时满足】

5个条件是独立评分。

每项20分。

总分100分。

只要满足至少3项，也就是50分以上，
就可以进入候选。

============================================================
第一项：缩量回调
20分
============================================================

股票之前应该经历过：

明显上涨
或者
阶段性冲高。

之后出现回调。

回调过程中：

成交量总体下降，
成交量明显小于前期上涨阶段。

满足：
前期上涨 + 缩量回调

得20分。

============================================================
第二项：阶段性地量
20分
============================================================

寻找近期明显的阶段性低成交量。

重点观察：

近期成交量明显低于此前活跃阶段；
成交量进入阶段低位；
成交量出现明显收缩。

出现明显地量：

得20分。

============================================================
第三项：地量之后止跌
20分
============================================================

地量出现以后：

股价不再持续创新低。

重点寻找：

低点趋稳；
连续下跌结束；
出现止跌；
或者开始形成小平台。

满足：

地量 + 止跌

得20分。

============================================================
第四项：温和放量
20分
============================================================

止跌之后：

出现阳线；
价格开始转强；
成交量相比前几日温和增加。

重点寻找：

止跌
+
阳线
+
温和放量。

满足：

得20分。

============================================================
第五项：放量突破
20分
============================================================

如果股票已经出现：

明显放量；

并且突破：

近期平台；
前期高点；
阶段压力位；
近期重要高点；

则得20分。

============================================================
评分规则
============================================================

缩量回调       20分
地量           20分
止跌           20分
温和放量       20分
放量突破       20分

总分：

0-100分。

注意：

【不要求全部满足】

50分：
进入候选。

60分：
较强候选。

70分：
重点关注。

80分以上：
强势候选。

100分：
量价结构完整。

============================================================
非常重要
============================================================

不要因为缺少某一个条件而淘汰股票。

例如：

股票A：

缩量回调 ✓
地量 ✓
止跌 ✓
温和放量 ✗
突破 ✗

仍然应该：

60分
进入候选。

股票B：

缩量回调 ✓
地量 ✓
止跌 ✓
温和放量 ✓
突破 ✗

应该：

80分
重点关注。

股票C：

缩量回调 ✓
地量 ✓
止跌 ✓
温和放量 ✓
突破 ✓

应该：

100分
最高级候选。

============================================================
排序
============================================================

按照：

第一：
视频量价结构总分

第二：
是否出现温和放量

第三：
是否已经突破

第四：
近期量价转强程度

从高到低排序。

============================================================
输出
============================================================

请尽可能返回：

20-50只候选股票。

每只尽量提供：

股票代码
股票简称
最新价
涨跌幅
成交量
成交额
换手率
近期高点
近期低点

以及：

缩量回调
地量
止跌
温和放量
放量突破

分别是否满足。

并给出：

视频量价结构评分：

0-100分。

============================================================
禁止使用旧策略
============================================================

不要使用：

龙虎榜硬条件；
连板硬条件；
市值300亿硬条件；
竞价换手率29.25%硬条件；
竞价涨幅5%硬条件；
委卖大于委买硬条件。

这些全部取消。

本次只使用：

【视频量价结构评分法】。

"""


# ============================================================
# 扫描股票
# ============================================================

def scan():

    try:

        result = parse_result(
            mx_select(VIDEO_QUERY)
        )

        if result.get("data"):

            return result

        # ====================================================
        # 第一轮没有结果时，自动放宽
        # ====================================================

        fallback_query = r"""

筛选今天A股。

按照视频量价结构寻找候选。

不要要求所有条件同时满足。

以下5项：

1. 缩量回调
2. 地量
3. 止跌
4. 温和放量
5. 放量突破

只要至少满足3项，
即可进入候选。

按照量价结构完整程度排序。

重点寻找：

前期上涨；
缩量回调；
成交量萎缩；
阶段性地量；
低点稳定；
止跌；
温和放量；
平台突破。

不要使用：

龙虎榜；
连板；
市值300亿；
竞价换手率29.25%；
竞价涨幅5%；
委卖大于委买。

返回尽可能多的候选股票。

提供：

代码；
简称；
最新价；
涨跌幅；
成交量；
成交额；
换手率；
近期高低点；
量价指标。

"""

        return parse_result(
            mx_select(fallback_query)
        )

    except Exception as e:

        return {
            "success": False,
            "message": str(e),
            "data": [],
        }


# ============================================================
# 手机端网页
# ============================================================

HTML = r"""

<!DOCTYPE html>

<html lang="zh-CN">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,
    initial-scale=1,
    maximum-scale=1,
    user-scalable=no"
>

<title>
视频量价选股器
</title>


<style>


* {
    box-sizing: border-box;
}


body {

    margin: 0;

    background:
        radial-gradient(
            circle at top,
            #17202b 0%,
            #080b10 45%,
            #050608 100%
        );

    color: #f1f4f7;

    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "PingFang SC",
        "Microsoft YaHei",
        sans-serif;

}


.header {

    position: sticky;

    top: 0;

    z-index: 10;

    padding: 17px 16px;

    background:
        rgba(
            5,
            8,
            12,
            0.96
        );

    backdrop-filter:
        blur(12px);

    border-bottom:
        1px solid #202733;

}


.title {

    font-size: 21px;

    font-weight: 900;

}


.subtitle {

    margin-top: 5px;

    color: #8d98a8;

    font-size: 12px;

}


.container {

    max-width: 1000px;

    margin: auto;

    padding: 14px;

}


.steps {

    display: flex;

    gap: 6px;

    overflow-x: auto;

    margin-bottom: 12px;

}


.step {

    flex:
        0 0 auto;

    white-space:
        nowrap;

    padding:
        8px 10px;

    border-radius:
        8px;

    border:
        1px solid #5e1825;

    background:
        #281019;

    color:
        #e3a7b0;

    font-size:
        12px;

}


.toolbar {

    display: flex;

    gap: 9px;

    margin-bottom: 16px;

}


button {

    border: none;

    border-radius: 10px;

    padding:
        12px 18px;

    background:
        #c32036;

    color: white;

    font-weight: 800;

    font-size: 15px;

}


.status {

    flex: 1;

    border:
        1px solid #202733;

    background:
        #10151d;

    border-radius:
        10px;

    display:
        flex;

    align-items:
        center;

    justify-content:
        center;

    color:
        #929dac;

    font-size:
        12px;

}


h2 {

    font-size:
        18px;

    margin:
        18px 0 10px;

}


.top3 {

    display:
        grid;

    grid-template-columns:
        repeat(3, 1fr);

    gap:
        10px;

}


.card {

    background:
        linear-gradient(
            145deg,
            #171d27,
            #0d1117
        );

    border:
        1px solid #2b3440;

    border-radius:
        13px;

    padding:
        14px;

}


.rank {

    font-size:
        12px;

    color:
        #ff5265;

    font-weight:
        900;

}


.name {

    font-size:
        19px;

    font-weight:
        900;

    margin-top:
        5px;

}


.code {

    font-size:
        11px;

    color:
        #768293;

    margin-top:
        3px;

}


.score {

    font-size:
        28px;

    font-weight:
        900;

    margin-top:
        10px;

    color:
        #ff6372;

}


.score small {

    font-size:
        12px;

    color:
        #8994a3;

}


.price {

    font-size:
        18px;

    font-weight:
        800;

    margin-top:
        8px;

}


.pct {

    font-size:
        13px;

    margin-top:
        2px;

}


.up {

    color:
        #ff5265;

}


.down {

    color:
        #28c982;

}


.tags {

    display:
        flex;

    flex-wrap:
        wrap;

    gap:
        5px;

    margin-top:
        10px;

}


.tag {

    font-size:
        10px;

    padding:
        4px 6px;

    border-radius:
        5px;

    background:
        #202832;

    color:
        #c1cad4;

}


.empty {

    padding:
        28px;

    text-align:
        center;

    color:
        #727e8d;

    border:
        1px dashed #29313d;

    border-radius:
        12px;

}


.table-wrap {

    overflow-x:
        auto;

    border:
        1px solid #202733;

    border-radius:
        12px;

    background:
        #0b0e13;

}


table {

    width:
        100%;

    min-width:
        820px;

    border-collapse:
        collapse;

    font-size:
        12px;

}


th {

    background:
        #121720;

    color:
        #8e99a8;

    text-align:
        left;

    padding:
        10px;

    white-space:
        nowrap;

}


td {

    padding:
        10px;

    border-top:
        1px solid #1b222c;

    white-space:
        nowrap;

}


.scorebar {

    font-weight:
        900;

    color:
        #ff6372;

}


.footer {

    text-align:
        center;

    color:
        #606b79;

    font-size:
        11px;

    padding:
        20px 0 35px;

}


@media(max-width:650px) {

    .top3 {

        grid-template-columns:
            1fr;

    }

    .title {

        font-size:
            20px;

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

        缩量回调 → 地量 → 止跌 → 温和放量 → 突破

        ｜评分制，不要求全部满足

    </div>

</div>


<div class="container">


<div class="steps">

    <div class="step">
        ① 缩量回调 20
    </div>

    <div class="step">
        ② 地量 20
    </div>

    <div class="step">
        ③ 止跌 20
    </div>

    <div class="step">
        ④ 温和放量 20
    </div>

    <div class="step">
        ⑤ 放量突破 20
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


<h2>

    🔥 视频量价匹配 TOP 3

</h2>


<div
    id="top3"
    class="top3"
>

    <div class="empty">

        点击“开始选股”

    </div>

</div>


<h2>

    📊 全部候选

</h2>


<div class="table-wrap">

<table>

<thead>

<tr>

    <th>
        排名
    </th>

    <th>
        代码
    </th>

    <th>
        名称
    </th>

    <th>
        评分
    </th>

    <th>
        最新价
    </th>

    <th>
        涨跌幅
    </th>

    <th>
        量价指标
    </th>

</tr>

</thead>


<tbody id="table">

<tr>

<td colspan="7">

    <div class="empty">

        暂无数据

    </div>

</td>

</tr>

</tbody>

</table>

</div>


<div class="footer">

    数据由东方财富妙想接口提供

    <br>

    本工具仅用于量价结构分析，不构成投资建议

</div>


</div>


<script>


function safe(v) {

    if (
        v === null ||
        v === undefined
    ) {

        return "";

    }

    return String(v);

}


/* =========================================================
   提取评分
========================================================= */

function getScore(stock) {

    const extra =
        stock.extra || {};

    for (
        const key of Object.keys(extra)
    ) {

        if (
            /评分|分数|量价结构|匹配度/
            .test(key)
        ) {

            const number =
                parseFloat(
                    String(extra[key])
                    .replace(
                        /[^\d.-]/g,
                        ""
                    )
                );

            if (
                !isNaN(number)
            ) {

                return Math.max(
                    0,
                    Math.min(
                        100,
                        number
                    )
                );

            }

        }

    }

    return null;

}


/* =========================================================
   判断量价标签
========================================================= */

function getTags(stock) {

    const extra =
        stock.extra || {};

    const text =
        Object.entries(extra)
        .map(
            ([key,value]) =>
                key + ":" + value
        )
        .join(" ");

    const tags = [];


    if (
        /缩量/
        .test(text)
    ) {

        tags.push("缩量");

    }


    if (
        /地量/
        .test(text)
    ) {

        tags.push("地量");

    }


    if (
        /止跌/
        .test(text)
    ) {

        tags.push("止跌");

    }


    if (
        /温和放量|放量/
        .test(text)
    ) {

        tags.push("放量");

    }


    if (
        /突破/
        .test(text)
    ) {

        tags.push("突破");

    }


    return tags.slice(
        0,
        5
    );

}


/* =========================================================
   TOP 3
========================================================= */

function renderTop3(list) {

    const box =
        document.getElementById(
            "top3"
        );


    if (
        !list.length
    ) {

        box.innerHTML = `

            <div class="empty">

                当前没有候选股票

            </div>

        `;

        return;

    }


    box.innerHTML =

        list
        .slice(0,3)
        .map(
            (stock,index) => {

                const pct =
                    parseFloat(
                        String(
                            stock.pct
                        )
                        .replace(
                            "%",
                            ""
                        )
                    );


                const score =
                    getScore(
                        stock
                    );


                const tags =
                    getTags(
                        stock
                    );


                return `

<div class="card">

    <div class="rank">

        TOP ${index + 1}

    </div>


    <div class="name">

        ${safe(
            stock.name
        ) || "--"}

    </div>


    <div class="code">

        ${safe(
            stock.code
        ) || "--"}

    </div>


    <div class="score">

        ${
            score === null
            ? "—"
            : score
        }

        <small>

            ${
                score === null
                ? ""
                : " / 100"
            }

        </small>

    </div>


    <div class="price">

        ${
            safe(
                stock.price
            ) || "--"
        }

    </div>


    <div
        class="pct ${
            isNaN(pct) ||
            pct >= 0
            ? "up"
            : "down"
        }"
    >

        ${
            safe(
                stock.pct
            ) || "--"
        }

    </div>


    <div class="tags">

        ${
            tags
            .map(
                tag => `
                <span class="tag">
                    ${tag} ✓
                </span>
                `
            )
            .join("")
        }

    </div>


</div>

`;

            }
        )
        .join("");

}


/* =========================================================
   全部股票
========================================================= */

function renderTable(list) {

    const table =
        document.getElementById(
            "table"
        );


    if (
        !list.length
    ) {

        table.innerHTML = `

<tr>

<td colspan="7">

    <div class="empty">

        暂无符合结果

    </div>

</td>

</tr>

`;

        return;

    }


    table.innerHTML =

        list
        .map(
            (stock,index) => {

                const score =
                    getScore(
                        stock
                    );


                const extra =
                    Object.entries(
                        stock.extra || {}
                    )
                    .slice(
                        0,
                        8
                    )
                    .map(
                        ([key,value]) =>
                            `${key}: ${value}`
                    )
                    .join(
                        "<br>"
                    );


                return `

<tr>

<td>
    ${index + 1}
</td>

<td>
    ${safe(stock.code)}
</td>

<td>
    <b>
        ${safe(stock.name)}
    </b>
</td>

<td class="scorebar">

    ${
        score === null
        ? "—"
        : score
    }

</td>

<td>
    ${safe(stock.price)}
</td>

<td>
    ${safe(stock.pct)}
</td>

<td>
    ${extra}
</td>

</tr>

`;

            }
        )
        .join("");

}


/* =========================================================
   扫描
========================================================= */

async function scan() {

    const status =
        document.getElementById(
            "status"
        );


    status.textContent =
        "正在扫描东方财富妙想……";


    try {

        const response =
            await fetch(
                "/api/scan",
                {
                    cache:
                        "no-store"
                }
            );


        const result =
            await response.json();


        if (
            !result.success
        ) {

            throw new Error(
                result.message ||
                "扫描失败"
            );

        }


        let list =
            result.data || [];


        /*
         * 前端再次按评分排序
         */

        list.sort(
            (a,b) =>
                (
                    getScore(b) || 0
                )
                -
                (
                    getScore(a) || 0
                )
        );


        renderTop3(
            list
        );


        renderTable(
            list
        );


        status.textContent =
            `完成：${list.length} 只`;


    }
    catch(error) {

        document
            .getElementById(
                "top3"
            )
            .innerHTML = `

                <div class="empty">

                    ❌ ${safe(
                        error.message
                    )}

                </div>

            `;


        status.textContent =
            "扫描失败";

    }

}


</script>


</body>

</html>

"""


# ============================================================
# 页面
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

@app.get(
    "/api/scan"
)
def api_scan():

    return scan()


# ============================================================
# 健康检查
# ============================================================

@app.get(
    "/health"
)
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