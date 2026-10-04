from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import os
import html
import time
import requests
import re

app = FastAPI(title="妖股雷达")

MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"

CACHE = {"ts": 0, "data": [], "source": "未连接"}
CACHE_SECONDS = 60

# 不在这里写死硬条件，让妙想先返回尽可能完整的候选数据
QUERY = """
你是A股量化选股数据助手。

请查询今天A股市场，返回尽可能多的活跃股票候选，至少返回50只。

优先选择：
1. 最近3个交易日出现过涨停的股票；
2. 最近交易日出现过龙虎榜的股票；
3. 总市值300亿元以内的股票；
4. 排除ST、*ST、退市整理股票。

请务必返回真实数据，不要编造。

每只股票必须尽可能提供以下字段：
股票代码
股票简称
最新价
涨跌幅
换手率
总市值
龙虎榜净买额
近3个交易日涨停次数
集合竞价委买金额
集合竞价委卖金额

特别注意：
- 总市值请使用“亿元”
- 龙虎榜净买额请使用“亿元”
- 涨停次数使用整数
- 换手率使用百分比
- 涨跌幅使用百分比
- 龙虎榜没有数据时可以填0
- 涨停次数没有数据时可以填0

请优先返回数据完整、近期强势的股票。
"""

def num(v, default=0.0):
    if v is None:
        return default

    s = str(v).strip()

    if not s or s in ("-", "--", "—", "None", "null", "N/A"):
        return default

    s = s.replace(",", "").replace("％", "%")

    try:
        # 处理中文单位
        if "亿" in s:
            s = s.replace("亿元", "").replace("亿", "").strip()
            return float(s)

        if "万" in s:
            s = s.replace("万元", "").replace("万", "").strip()
            return float(s) / 10000

        s = s.replace("%", "").strip()

        return float(s)

    except Exception:
        m = re.search(r"-?\d+(?:\.\d+)?", s)

        if m:
            try:
                return float(m.group())
            except Exception:
                pass

        return default


def pick(row, *names):
    # 精确匹配
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]

    # 模糊匹配
    for k, v in row.items():
        if v in (None, ""):
            continue

        ks = str(k).lower()

        for name in names:
            if str(name).lower() in ks:
                return v

    return ""


def extract_rows(obj):
    if not isinstance(obj, dict):
        return []

    outer = obj.get("data") or {}
    inner = outer.get("data") or {}

    # 妙想结构
    all_results = inner.get("allResults") or {}
    result = all_results.get("result") or {}

    rows = result.get("dataList") or []

    if isinstance(rows, list) and rows:
        return rows

    # partialResults Markdown
    partial = inner.get("partialResults") or ""

    if not partial:
        return []

    lines = [
        x.strip()
        for x in str(partial).splitlines()
        if x.strip().startswith("|")
    ]

    if len(lines) < 3:
        return []

    def cells(line):
        return [
            x.strip()
            for x in line.strip("|").split("|")
        ]

    headers = cells(lines[0])

    rows = []

    for line in lines[2:]:
        values = cells(line)

        if len(values) != len(headers):
            continue

        row = dict(zip(headers, values))

        # 跳过分隔线
        if any("---" in str(v) for v in values):
            continue

        rows.append(row)

    return rows


def normalize(rows):
    result = []

    for row in rows:

        if not isinstance(row, dict):
            continue

        code = str(pick(
            row,
            "SECURITY_CODE",
            "股票代码",
            "证券代码",
            "代码"
        )).strip()

        name = str(pick(
            row,
            "SECURITY_SHORT_NAME",
            "SECURITY_NAME_ABBR",
            "股票简称",
            "证券简称",
            "名称"
        )).strip()

        if not code or not name:
            continue

        # 只保留6位A股代码
        code_clean = re.sub(r"\D", "", code)

        if len(code_clean) != 6:
            continue

        upper_name = name.upper()

        if "ST" in upper_name:
            continue

        if name.startswith("退"):
            continue

        price = num(pick(
            row,
            "NEWEST_PRICE",
            "最新价",
            "现价",
            "最新"
        ))

        pct = num(pick(
            row,
            "CHG",
            "涨跌幅",
            "今日涨跌幅",
            "涨幅"
        ))

        turnover = num(pick(
            row,
            "TURNOVER_RATE",
            "换手率",
            "今日换手率",
            "HSL"
        ))

        # 市值
        cap_raw = pick(
            row,
            "TOTAL_MARKET_CAP",
            "TOTAL_MV",
            "MARKET_CAP",
            "总市值",
            "总市值(元)",
            "市值"
        )

        cap = num(cap_raw)

        # 如果接口返回的是元
        if cap > 100000:
            cap = cap / 100000000

        # 如果接口返回的是万元
        elif cap > 10000:
            cap = cap / 10000

        # 龙虎榜
        lhb_raw = pick(
            row,
            "BILLBOARD_NET_AMT",
            "LHB_NET_BUY",
            "龙虎榜净买额",
            "龙虎榜净额",
            "龙虎榜净买",
            "龙虎榜"
        )

        lhb = num(lhb_raw)

        # 如果是元
        if abs(lhb) > 10000:
            lhb = lhb / 100000000

        # 涨停次数
        zt_raw = pick(
            row,
            "LIMIT_UP_COUNT_3D",
            "LIMIT_UP_COUNT",
            "近3日涨停次数",
            "近3个交易日涨停次数",
            "3日涨停",
            "涨停次数"
        )

        zt_count = int(max(0, num(zt_raw)))

        # 委买
        buy = num(pick(
            row,
            "BID_AMOUNT",
            "委买金额",
            "竞价委买",
            "集合竞价委买"
        ))

        # 委卖
        sell = num(pick(
            row,
            "ASK_AMOUNT",
            "委卖金额",
            "竞价委卖",
            "集合竞价委卖"
        ))

        # -------------------------
        # 妖股评分
        # -------------------------

        score = 30

        # 涨幅
        if pct >= 5:
            score += 10

        if pct >= 7:
            score += 5

        if pct >= 9.5:
            score += 5

        # 换手
        if turnover > 29.25:
            score += 15
        elif turnover > 20:
            score += 10
        elif turnover > 15:
            score += 7
        elif turnover > 8:
            score += 4

        # 龙虎榜
        if lhb > 0:
            score += 10

        if lhb >= 1:
            score += 3

        if lhb < 0:
            score += 2

        # 近3日涨停
        if zt_count >= 3:
            score += 15
        elif zt_count >= 2:
            score += 10
        elif zt_count >= 1:
            score += 5

        # 委卖 > 委买
        if sell > buy and (sell + buy) > 0:
            score += 5

        # 硬条件判断
        hard_zt = zt_count >= 1
        hard_lhb = abs(lhb) > 0
        hard_cap = cap > 0 and cap <= 300

        hard_count = sum([
            hard_zt,
            hard_lhb,
            hard_cap
        ])

        result.append({
            "code": code_clean,
            "name": name,
            "price": price,
            "pct": pct,
            "turnover": turnover,
            "market_cap": cap,
            "lhb_net": lhb,
            "zt_count": zt_count,
            "buy": buy,
            "sell": sell,
            "score": min(100, int(score)),
            "hard_count": hard_count,
            "hard_zt": hard_zt,
            "hard_lhb": hard_lhb,
            "hard_cap": hard_cap
        })

    # 优先：
    # 1. 三项硬条件全部满足
    # 2. 硬条件满足数量
    # 3. 妖股评分
    # 4. 涨幅
    result.sort(
        key=lambda x: (
            x["hard_count"],
            x["score"],
            x["pct"],
            x["lhb_net"]
        ),
        reverse=True
    )

    return result[:50]


def query_miaoxiang():

    api_key = os.getenv("MX_APIKEY")

    if not api_key:
        raise RuntimeError("MX_APIKEY 未设置")

    response = requests.post(
        MX_URL,
        headers={
            "Content-Type": "application/json",
            "apikey": api_key
        },
        json={
            "keyword": QUERY
        },
        timeout=40
    )

    response.raise_for_status()

    return response.json()


def get_data():

    now = time.time()

    if (
        CACHE["data"]
        and now - CACHE["ts"] < CACHE_SECONDS
    ):
        return CACHE["data"], CACHE["source"]

    try:

        raw = query_miaoxiang()

        rows = extract_rows(raw)

        data = normalize(rows)

        CACHE["ts"] = now
        CACHE["data"] = data
        CACHE["source"] = "东方财富妙想智能选股 API"

        print("ROWS:", len(rows))
        print("NORMALIZED:", len(data))

        return data, CACHE["source"]

    except Exception as e:

        CACHE["ts"] = now
        CACHE["data"] = []
        CACHE["source"] = "接口异常：" + str(e)[:180]

        print(
            "MIAOXIANG_ERROR:",
            repr(e)
        )

        return [], CACHE["source"]


@app.get("/api/health")
def health():

    return {
        "status": "ok",
        "mx_apikey_configured": bool(
            os.getenv("MX_APIKEY")
        )
    }


@app.get("/api/scanner")
def scanner():

    data, source = get_data()

    # 真正的硬条件结果
    hard = [
        x for x in data
        if x["hard_zt"]
        and x["hard_lhb"]
        and x["hard_cap"]
    ]

    return {
        "status": "ok",
        "source": source,
        "updated_at": time.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "top3": hard[:3] if hard else data[:3],
        "hard_condition_count": len(hard),
        "data": data
    }


@app.get("/", response_class=HTMLResponse)
def home():

    data, source = get_data()

    hard = [
        x for x in data
        if x["hard_zt"]
        and x["hard_lhb"]
        and x["hard_cap"]
    ]

    # 有完整硬条件就显示完整硬条件
    # 没有则显示最强候选，避免整页空白
    top3 = hard[:3] if hard else data[:3]

    if top3:

        cards = ""

        for i, x in enumerate(top3):

            cards += f"""
            <div class="card">

                <div class="rank">
                    #{i + 1}
                    · 硬条件 {x["hard_count"]}/3
                </div>

                <div class="name">
                    {html.escape(x["name"])}
                </div>

                <div class="code">
                    {html.escape(x["code"])}
                </div>

                <div class="bigscore">
                    {x["score"]}
                    <small>分</small>

                    <span>
                        {x["pct"]:+.2f}%
                    </span>
                </div>

                <div class="detail">

                    市值：
                    {x["market_cap"]:.2f}亿

                    <br>

                    龙虎榜：
                    {x["lhb_net"]:+.2f}亿

                    <br>

                    近3日涨停：
                    {x["zt_count"]}次

                    <br>

                    换手：
                    {x["turnover"]:.2f}%

                </div>

            </div>
            """

    else:

        cards = """
        <div class="empty">
            暂无有效候选数据，请稍后刷新
        </div>
        """

    rows = ""

    for x in data:

        hard_text = (
            "✓ 满足"
            if x["hard_count"] == 3
            else f'{x["hard_count"]}/3'
        )

        rows += f"""
        <tr>

            <td>
                {html.escape(x["code"])}
            </td>

            <td>
                <b>
                    {html.escape(x["name"])}
                </b>
            </td>

            <td>
                {x["price"]:.2f}
            </td>

            <td class="up">
                {x["pct"]:+.2f}%
            </td>

            <td>
                {x["turnover"]:.2f}%
            </td>

            <td>
                {x["market_cap"]:.2f}亿
            </td>

            <td>
                {x["lhb_net"]:+.2f}亿
            </td>

            <td>
                {x["zt_count"]}次
            </td>

            <td>
                {hard_text}
            </td>

            <td class="score">
                {x["score"]}
            </td>

        </tr>
        """

    if not rows:

        rows = """
        <tr>
            <td colspan="10"
                class="empty">
                暂无候选数据
            </td>
        </tr>
        """

    updated = time.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    return f"""
<!doctype html>

<html lang="zh-CN">

<head>

<meta charset="utf-8">

<meta name="viewport"
content="width=device-width,
initial-scale=1,
maximum-scale=1">

<meta http-equiv="refresh"
content="60">

<title>🔥 妖股雷达</title>

<style>

* {{
    box-sizing:border-box;
}}

body {{

    margin:0;

    background:#070b11;

    color:#edf2f8;

    font-family:
    -apple-system,
    BlinkMacSystemFont,
    "PingFang SC",
    "Microsoft YaHei",
    sans-serif;

}}

.wrap {{

    max-width:1200px;

    margin:auto;

    padding:16px 10px;

}}

h1 {{

    margin:0;

    font-size:25px;

}}

.sub {{

    color:#8390a3;

    line-height:1.7;

    margin:
    7px 0 12px;

}}

.status {{

    display:inline-block;

    padding:7px 10px;

    border-radius:9px;

    background:#111b25;

    border:1px solid #263443;

    color:#62e59a;

    font-size:12px;

    margin-bottom:12px;

}}

.tops {{

    display:grid;

    grid-template-columns:
    repeat(3,1fr);

    gap:10px;

    margin-bottom:12px;

}}

.card,
.box {{

    background:#101722;

    border:1px solid #202d3c;

    border-radius:14px;

}}

.card {{

    padding:14px;

}}

.rank,
.code,
.detail {{

    color:#7f8da1;

    font-size:12px;

}}

.name {{

    font-size:19px;

    font-weight:700;

    margin-top:4px;

}}

.bigscore {{

    color:#ffb54a;

    font-size:28px;

    font-weight:800;

    margin-top:10px;

}}

.bigscore small {{

    font-size:12px;

    font-weight:400;

}}

.bigscore span {{

    color:#ff4d67;

    font-size:17px;

    margin-left:8px;

}}

.detail {{

    margin-top:7px;

    line-height:1.7;

}}

.box {{

    overflow:auto;

}}

table {{

    width:100%;

    min-width:1000px;

    border-collapse:
    collapse;

}}

th,
td {{

    padding:11px 9px;

    border-bottom:
    1px solid #202d3c;

    text-align:left;

}}

th {{

    color:#8795a9;

    font-size:12px;

}}

td {{

    font-size:13px;

}}

.up {{

    color:#ff4d67;

    font-weight:700;

}}

.score {{

    color:#ffb54a;

    font-size:17px;

    font-weight:800;

}}

.empty {{

    padding:28px;

    text-align:center;

    color:#7f8da1;

}}

.note {{

    margin-top:10px;

    color:#69778b;

    font-size:12px;

    line-height:1.8;

}}

@media(max-width:700px) {{

    .tops {{

        grid-template-columns:1fr;

    }}

    h1 {{

        font-size:23px;

    }}

}}

</style>

</head>

<body>

<div class="wrap">

<h1>
🔥 妖股雷达
</h1>

<div class="sub">

硬条件：
近3交易日涨停 + 龙虎榜 + 总市值 ≤ 300亿

<br>

评分：
涨幅 + 换手率 + 龙虎榜资金
+ 近3日涨停次数 + 竞价强弱

</div>

<div class="status">

● {html.escape(source)}

</div>

<div class="tops">

{cards}

</div>

<div class="box">

<table>

<thead>

<tr>

<th>代码</th>

<th>名称</th>

<th>现价</th>

<th>涨幅</th>

<th>换手</th>

<th>市值</th>

<th>龙虎榜净额</th>

<th>近3日涨停</th>

<th>硬条件</th>

<th>妖股评分</th>

</tr>

</thead>

<tbody>

{rows}

</tbody>

</table>

</div>

<div class="note">

更新时间：
{updated}

<br>

数据源：
东方财富妙想智能选股 API

<br>

每60秒自动刷新

<br>

⚠️ 妖股评分仅用于量化筛选和研究，不构成投资建议。

</div>

</div>

</body>

</html>
"""


if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000"
            )
        )
    )
