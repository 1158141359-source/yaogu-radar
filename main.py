from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import os
import html
import time
import requests
from datetime import datetime, timedelta

app = FastAPI(title="妖股雷达")

MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/"
}

CACHE = {
    "ts": 0,
    "data": [],
    "source": "未连接"
}

CACHE_SECONDS = 60


# =========================================================
# 妙想：只负责找候选股票
# =========================================================

QUERY = """
请筛选今天A股市场最强势的股票候选，返回至少30只。

重点关注：
1. 今日涨幅较大；
2. 今日换手率较高；
3. 最近出现涨停；
4. 最近出现龙虎榜；
5. 市值300亿元以内；
6. 排除ST、*ST、退市股票。

请返回：
股票代码、股票简称、最新价、涨跌幅、换手率。

不要因为缺少市值、龙虎榜或涨停次数而不返回股票。
"""


def mx_query():

    key = os.getenv("MX_APIKEY")

    if not key:
        raise RuntimeError("MX_APIKEY 未设置")

    r = requests.post(
        MX_URL,
        headers={
            "Content-Type": "application/json",
            "apikey": key
        },
        json={
            "keyword": QUERY
        },
        timeout=40
    )

    r.raise_for_status()

    return r.json()


def parse_mx(obj):

    outer = obj.get("data") or {}
    inner = outer.get("data") or {}

    result = (
        (inner.get("allResults") or {})
        .get("result") or {}
    )

    rows = result.get("dataList") or []

    if rows:
        return rows

    partial = inner.get("partialResults") or ""

    lines = [
        x.strip()
        for x in str(partial).splitlines()
        if x.strip().startswith("|")
    ]

    if len(lines) < 3:
        return []

    def cells(x):
        return [
            v.strip()
            for v in x.strip("|").split("|")
        ]

    headers = cells(lines[0])

    rows = []

    for line in lines[2:]:

        vals = cells(line)

        if len(vals) != len(headers):
            continue

        rows.append(dict(zip(headers, vals)))

    return rows


def get_value(row, names):

    for name in names:

        if name in row:
            value = row[name]

            if value not in ("", None):
                return value

    for k, v in row.items():

        if v in ("", None):
            continue

        for name in names:

            if name.lower() in str(k).lower():
                return v

    return ""


def number(v):

    try:

        if v is None:
            return 0.0

        s = str(v).strip()

        if not s:
            return 0.0

        s = s.replace(",", "")
        s = s.replace("%", "")

        return float(s)

    except Exception:

        return 0.0


def parse_candidates(rows):

    result = []

    for row in rows:

        code = str(get_value(
            row,
            [
                "SECURITY_CODE",
                "股票代码",
                "证券代码",
                "代码"
            ]
        )).strip()

        name = str(get_value(
            row,
            [
                "SECURITY_SHORT_NAME",
                "SECURITY_NAME_ABBR",
                "股票简称",
                "证券简称",
                "名称"
            ]
        )).strip()

        code = "".join(
            x for x in code if x.isdigit()
        )

        if len(code) != 6:
            continue

        if not name:
            continue

        if "ST" in name.upper():
            continue

        if name.startswith("退"):
            continue

        price = number(get_value(
            row,
            [
                "NEWEST_PRICE",
                "最新价",
                "现价"
            ]
        ))

        pct = number(get_value(
            row,
            [
                "CHG",
                "涨跌幅",
                "涨幅"
            ]
        ))

        turnover = number(get_value(
            row,
            [
                "TURNOVER_RATE",
                "换手率",
                "HSL"
            ]
        ))

        result.append({
            "code": code,
            "name": name,
            "price": price,
            "pct": pct,
            "turnover": turnover
        })

    # 去重
    unique = {}

    for x in result:
        unique[x["code"]] = x

    return list(unique.values())


# =========================================================
# 东方财富实时行情
# =========================================================

def market_of(code):

    if code.startswith(("6", "68")):
        return "1"

    return "0"


def get_quotes(codes):

    if not codes:
        return {}

    secids = ",".join(
        market_of(c) + "." + c
        for c in codes
    )

    url = (
        "https://push2.eastmoney.com/"
        "api/qt/ulist.np/get"
    )

    params = {
        "fltt": "2",
        "invt": "2",
        "secids": secids,
        "fields": (
            "f2,f3,f8,f12,f13,f14,f20,f21"
        )
    }

    try:

        r = requests.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=15
        )

        data = r.json()

        diff = (
            (data.get("data") or {})
            .get("diff") or []
        )

        result = {}

        for x in diff:

            code = str(x.get("f12", ""))

            result[code] = {
                "price": float(x.get("f2") or 0),
                "pct": float(x.get("f3") or 0),
                "turnover": float(x.get("f8") or 0),
                "market_cap": (
                    float(x.get("f20") or 0)
                    / 100000000
                ),
                "name": x.get("f14") or ""
            }

        return result

    except Exception as e:

        print("QUOTE_ERROR:", repr(e))

        return {}


# =========================================================
# 最近交易日
# =========================================================

def get_trading_dates(count=3):

    dates = []

    day = datetime.now()

    for _ in range(15):

        d = day.strftime("%Y%m%d")

        try:

            url = (
                "https://push2ex.eastmoney.com/"
                "getTopicZTPool"
            )

            params = {
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "dpt": "wz.ztzt",
                "Pageindex": 0,
                "pagesize": 1,
                "sort": "fbt:asc",
                "date": d
            }

            r = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=10
            )

            data = r.json()

            pool = (
                (data.get("data") or {})
                .get("pool") or []
            )

            if pool:
                dates.append(d)

                if len(dates) >= count:
                    break

        except Exception as e:

            print(
                "TRADING_DATE_ERROR:",
                repr(e)
            )

        day -= timedelta(days=1)

    return dates


# =========================================================
# 近3日涨停
# =========================================================

def get_limit_up_counts(codes, dates):

    result = {
        code: 0
        for code in codes
    }

    for d in dates:

        try:

            url = (
                "https://push2ex.eastmoney.com/"
                "getTopicZTPool"
            )

            params = {
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "dpt": "wz.ztzt",
                "Pageindex": 0,
                "pagesize": 200,
                "sort": "fbt:asc",
                "date": d
            }

            r = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=15
            )

            data = r.json()

            pool = (
                (data.get("data") or {})
                .get("pool") or []
            )

            for item in pool:

                code = str(
                    item.get("c", "")
                ).zfill(6)

                if code in result:
                    result[code] += 1

        except Exception as e:

            print(
                "ZT_ERROR:",
                d,
                repr(e)
            )

    return result


# =========================================================
# 龙虎榜
# =========================================================

def get_lhb(codes, dates):

    result = {
        code: {
            "net": 0.0,
            "on_board": False
        }
        for code in codes
    }

    for d in dates:

        date_fmt = (
            f"{d[:4]}-{d[4:6]}-{d[6:]}"
        )

        url = (
            "https://datacenter-web.eastmoney.com/"
            "api/data/v1/get"
        )

        params = {
            "reportName":
                "RPT_DAILYBILLBOARD_DETAILSNEW",

            "columns":
                "SECURITY_CODE,"
                "SECURITY_NAME_ABBR,"
                "BILLBOARD_NET_AMT,"
                "BILLBOARD_BUY_AMT,"
                "BILLBOARD_SELL_AMT,"
                "TRADE_DATE",

            "pageNumber": 1,
            "pageSize": 500,

            "sortColumns":
                "BILLBOARD_NET_AMT",

            "sortTypes": -1,

            "source": "WEB",
            "client": "WEB",

            "filter":
                f"(TRADE_DATE='{date_fmt}')"
        }

        try:

            r = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=15
            )

            data = r.json()

            rows = (
                (data.get("result") or {})
                .get("data") or []
            )

            for row in rows:

                code = str(
                    row.get(
                        "SECURITY_CODE",
                        ""
                    )
                ).zfill(6)

                if code not in result:
                    continue

                net = float(
                    row.get(
                        "BILLBOARD_NET_AMT",
                        0
                    ) or 0
                )

                # 龙虎榜金额：元 → 亿元
                result[code]["net"] += (
                    net / 100000000
                )

                result[code]["on_board"] = True

        except Exception as e:

            print(
                "LHB_ERROR:",
                date_fmt,
                repr(e)
            )

    return result


# =========================================================
# 最终评分
# =========================================================

def make_result(candidates):

    codes = [
        x["code"]
        for x in candidates
    ]

    quotes = get_quotes(codes)

    dates = get_trading_dates(3)

    zt = get_limit_up_counts(
        codes,
        dates
    )

    # 龙虎榜至少检查最近几个交易日
    lhb_dates = get_trading_dates(10)

    lhb = get_lhb(
        codes,
        lhb_dates
    )

    result = []

    for x in candidates:

        code = x["code"]

        q = quotes.get(code, {})

        price = q.get(
            "price",
            x["price"]
        )

        pct = q.get(
            "pct",
            x["pct"]
        )

        turnover = q.get(
            "turnover",
            x["turnover"]
        )

        market_cap = q.get(
            "market_cap",
            0
        )

        zt_count = zt.get(
            code,
            0
        )

        lhb_net = (
            lhb.get(code, {})
            .get("net", 0)
        )

        on_lhb = (
            lhb.get(code, {})
            .get("on_board", False)
        )

        # 三项硬条件
        hard_zt = zt_count >= 1

        hard_lhb = on_lhb

        hard_cap = (
            market_cap > 0
            and market_cap <= 300
        )

        hard_count = sum([
            hard_zt,
            hard_lhb,
            hard_cap
        ])

        # 妖股评分
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
        if on_lhb:
            score += 10

        if lhb_net > 1:
            score += 5

        if lhb_net < 0:
            score += 2

        # 涨停
        if zt_count >= 3:
            score += 15
        elif zt_count >= 2:
            score += 10
        elif zt_count >= 1:
            score += 5

        result.append({
            "code": code,
            "name": q.get(
                "name",
                x["name"]
            ),
            "price": price,
            "pct": pct,
            "turnover": turnover,
            "market_cap": market_cap,
            "lhb_net": lhb_net,
            "zt_count": zt_count,
            "score": min(100, score),
            "hard_count": hard_count,
            "hard_zt": hard_zt,
            "hard_lhb": hard_lhb,
            "hard_cap": hard_cap
        })

    result.sort(
        key=lambda x: (
            x["hard_count"],
            x["score"],
            x["zt_count"],
            x["lhb_net"],
            x["pct"]
        ),
        reverse=True
    )

    return result


# =========================================================
# 主数据
# =========================================================

def get_data():

    now = time.time()

    if (
        CACHE["data"]
        and now - CACHE["ts"]
        < CACHE_SECONDS
    ):
        return (
            CACHE["data"],
            CACHE["source"]
        )

    try:

        raw = mx_query()

        rows = parse_mx(raw)

        candidates = parse_candidates(rows)

        print(
            "MX_CANDIDATES:",
            len(candidates)
        )

        data = make_result(
            candidates
        )

        CACHE["ts"] = now
        CACHE["data"] = data

        CACHE["source"] = (
            "妙想选股 + 东方财富公开数据"
        )

        return (
            data,
            CACHE["source"]
        )

    except Exception as e:

        print(
            "MAIN_ERROR:",
            repr(e)
        )

        CACHE["ts"] = now
        CACHE["data"] = []

        CACHE["source"] = (
            "接口异常：" +
            str(e)[:150]
        )

        return (
            [],
            CACHE["source"]
        )


# =========================================================
# API
# =========================================================

@app.get("/api/health")
def health():

    return {
        "status": "ok",
        "mx_apikey_configured":
            bool(os.getenv("MX_APIKEY"))
    }


@app.get("/api/scanner")
def scanner():

    data, source = get_data()

    hard = [
        x for x in data
        if (
            x["hard_zt"]
            and x["hard_lhb"]
            and x["hard_cap"]
        )
    ]

    return {
        "status": "ok",
        "source": source,
        "updated_at":
            time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        "trading_candidates":
            len(data),
        "hard_condition_count":
            len(hard),
        "top3":
            hard[:3] if hard
            else data[:3],
        "data": data
    }


# =========================================================
# 页面
# =========================================================

@app.get("/", response_class=HTMLResponse)
def home():

    data, source = get_data()

    hard = [
        x for x in data
        if (
            x["hard_zt"]
            and x["hard_lhb"]
            and x["hard_cap"]
        )
    ]

    top3 = (
        hard[:3]
        if hard
        else data[:3]
    )

    if top3:

        cards = ""

        for i, x in enumerate(top3):

            cards += f"""
            <div class="card">

                <div class="rank">
                    #{i+1}
                    · 硬条件
                    {x["hard_count"]}/3
                </div>

                <div class="name">
                    {html.escape(x["name"])}
                </div>

                <div class="code">
                    {x["code"]}
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
            暂无候选股票
        </div>
        """

    rows = ""

    for x in data:

        rows += f"""
        <tr>

            <td>{x["code"]}</td>

            <td>
                <b>{html.escape(x["name"])}</b>
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
                {x["hard_count"]}/3
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
                暂无数据
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
margin:7px 0 12px;
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
border-collapse:collapse;
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
妙想选股 + 东方财富公开数据

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
