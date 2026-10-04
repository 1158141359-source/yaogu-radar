from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import json
import time
import html

app = FastAPI(title="妖股雷达")

# ============================================================
# 东方财富接口
# ============================================================

EM_HOSTS = [
    "https://push2ex.eastmoney.com",
    "https://push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
]

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

UT = "bd1d9ddb04089700cf9c27f6f7426281"

CACHE = {
    "ts": 0,
    "data": [],
    "source": "未连接",
}

CACHE_SECONDS = 20


# ============================================================
# 基础函数
# ============================================================

def num(v, default=0.0):
    try:
        if v in (None, "", "-"):
            return default
        return float(v)
    except Exception:
        return default


def get_json(url, params=None, timeout=10):
    full_url = url

    if params:
        full_url += "?" + urlencode(params)

    last_error = None

    for _ in range(2):
        try:
            req = Request(
                full_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 "
                        "Mobile/15E148"
                    ),
                    "Referer": "https://quote.eastmoney.com/",
                    "Accept": "application/json,text/plain,*/*",
                },
            )

            with urlopen(req, timeout=timeout) as response:
                raw = response.read().decode(
                    "utf-8",
                    "ignore"
                )

            return json.loads(raw)

        except Exception as e:
            last_error = e
            time.sleep(0.3)

    raise last_error


# ============================================================
# 东方财富：涨停池
# ============================================================

def get_limit_up_pool(date_string):
    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": 0,
        "pagesize": 200,
        "sort": "fbt:asc",
        "date": date_string,
    }

    try:
        result = get_json(
            EM_HOSTS[0] + "/getTopicZTPool",
            params,
            10,
        )

        data = result.get("data") or {}

        return data.get("pool") or []

    except Exception as e:
        print("涨停池异常:", repr(e))
        return []


# ============================================================
# 东方财富：龙虎榜
# ============================================================

def get_lhb(date_string):
    params = {
        "reportName":
            "RPT_DAILYBILLBOARD_DETAILSNEW",

        "columns":
            "SECURITY_CODE,"
            "SECURITY_NAME_ABBR,"
            "CHANGE_RATE,"
            "BILLBOARD_NET_AMT,"
            "BILLBOARD_BUY_AMT,"
            "BILLBOARD_SELL_AMT,"
            "DEAL_AMOUNT_RATIO,"
            "EXPLANATION,"
            "TRADE_DATE",

        "pageNumber": 1,
        "pageSize": 500,

        "sortColumns":
            "BILLBOARD_NET_AMT",

        "sortTypes": "-1",

        "source": "WEB",
        "client": "WEB",

        "filter":
            f"(TRADE_DATE='{date_string}')",
    }

    result = get_json(
        DC_URL,
        params,
        12,
    )

    return (
        (result.get("result") or {})
        .get("data")
        or []
    )


# ============================================================
# 东方财富：实时行情
# ============================================================

def get_market():
    params = {
        "pn": 1,
        "pz": 5000,

        "ut": UT,

        "po": 1,
        "np": 1,

        "fltt": 2,
        "invt": 2,

        "fid": "f3",

        # 沪A + 深A + 创业板 + 科创板
        "fs":
            "m:0+t:6,"
            "m:0+t:80,"
            "m:1+t:2,"
            "m:1+t:23",

        # 代码、名称、价格、涨幅、
        # 成交额、换手率、量比、市值
        "fields":
            "f12,f14,f2,f3,f6,f8,f10,f20,f21",
    }

    last_error = None

    for host in EM_HOSTS[1:]:

        try:

            result = get_json(
                host + "/api/qt/clist/get",
                params,
                12,
            )

            diff = (
                (result.get("data") or {})
                .get("diff")
                or []
            )

            if isinstance(diff, dict):
                diff = list(diff.values())

            if diff:
                return diff

        except Exception as e:

            last_error = e

    raise last_error or Exception(
        "东方财富行情接口不可用"
    )


# ============================================================
# 单只股票盘口
# ============================================================

def get_stock_quote(code):

    market_code = (
        "1"
        if code.startswith(("6", "68"))
        else "0"
    )

    params = {
        "ut": UT,
        "fltt": 2,
        "invt": 2,

        "secid":
            f"{market_code}.{code}",

        "fields":
            "f43,f47,f48,f50,f57,f58,"
            "f116,f117,f168,f170,"
            "f31,f32,f33,f34,f35,f36,"
            "f37,f38,f39,f40,"
            "f19,f20,f17,f18,"
            "f15,f16,f13,f14,f11,f12",
    }

    for host in EM_HOSTS[1:]:

        try:

            result = get_json(
                host + "/api/qt/stock/get",
                params,
                8,
            )

            return result.get("data") or {}

        except Exception:
            pass

    return {}


# ============================================================
# 找近3个交易日涨停股票
# ============================================================

def get_recent_limit_up():

    today = datetime.now().date()

    found = {}

    checked_days = 0

    # 向前查最多9个自然日，
    # 找到最近3个有涨停池数据的交易日
    for i in range(9):

        day = today - timedelta(days=i)

        date_string = day.strftime("%Y%m%d")

        pool = get_limit_up_pool(
            date_string
        )

        if not pool:
            continue

        checked_days += 1

        for item in pool:

            code = str(
                item.get("c") or ""
            )

            if not code:
                continue

            if code not in found:
                found[code] = []

            found[code].append(
                date_string
            )

        if checked_days >= 3:
            break

    return found


# ============================================================
# 妖股评分
# ============================================================

def calculate_score(stock):

    score = 40

    pct = stock["pct"]
    turnover = stock["turnover"]
    lhb_net = stock["lhb_net"]

    # --------------------------------------------------------
    # 涨幅
    # --------------------------------------------------------

    if pct >= 5:
        score += 10

    if pct >= 7:
        score += 5

    if pct >= 9.5:
        score += 5

    # --------------------------------------------------------
    # 竞价/早盘换手
    # --------------------------------------------------------

    if turnover > 29.25:
        score += 15

    elif turnover > 15:
        score += 8

    elif turnover > 8:
        score += 4

    # --------------------------------------------------------
    # 龙虎榜净买
    # --------------------------------------------------------

    if lhb_net > 0:
        score += 10

    elif lhb_net < 0:
        score += 3

    # --------------------------------------------------------
    # 近3日涨停次数
    # --------------------------------------------------------

    zt_count = len(
        stock["zt_dates"]
    )

    if zt_count >= 2:
        score += 10

    elif zt_count == 1:
        score += 5

    # --------------------------------------------------------
    # 委卖 > 委买
    # --------------------------------------------------------

    if stock["sell"] > stock["buy"]:
        score += 10

    return min(
        100,
        round(score)
    )


# ============================================================
# 核心选股
# ============================================================

def scan():

    market = get_market()

    recent_zt = get_recent_limit_up()

    if not recent_zt:

        raise Exception(
            "近3个交易日涨停池暂无数据"
        )

    # --------------------------------------------------------
    # 获取最近龙虎榜
    # --------------------------------------------------------

    latest_lhb = {}

    for i in range(8):

        date_string = (
            datetime.now().date()
            - timedelta(days=i)
        ).strftime("%Y-%m-%d")

        try:

            rows = get_lhb(
                date_string
            )

            for row in rows:

                code = str(
                    row.get(
                        "SECURITY_CODE"
                    )
                    or ""
                )

                if (
                    code
                    and code not in latest_lhb
                ):
                    latest_lhb[code] = row

        except Exception as e:

            print(
                "龙虎榜异常:",
                repr(e)
            )

        if latest_lhb:
            break

    results = []

    # --------------------------------------------------------
    # 硬条件：
    # 1. 近3交易日涨停
    # 2. 龙虎榜
    # 3. 市值 <= 300亿
    # --------------------------------------------------------

    for item in market:

        code = str(
            item.get("f12") or ""
        )

        name = str(
            item.get("f14") or ""
        )

        if not code or not name:
            continue

        upper_name = name.upper()

        if "ST" in upper_name:
            continue

        if name.startswith("退"):
            continue

        # 硬条件1：近3日涨停
        if code not in recent_zt:
            continue

        # 市值
        market_cap = num(
            item.get("f20")
        )

        # 硬条件3：<=300亿
        if (
            market_cap <= 0
            or market_cap > 30_000_000_000
        ):
            continue

        # 硬条件2：龙虎榜
        lhb_row = latest_lhb.get(code)

        if not lhb_row:
            continue

        quote = get_stock_quote(code)

        price = num(
            quote.get("f43"),
            num(item.get("f2"))
        )

        pct = num(
            quote.get("f170"),
            num(item.get("f3"))
        )

        turnover = num(
            quote.get("f168"),
            num(item.get("f8"))
        )

        # ----------------------------------------------------
        # 委买/委卖估算
        # ----------------------------------------------------

        buy = sum(
            num(quote.get(k))
            for k in (
                "f20",
                "f18",
                "f16",
                "f14",
                "f12",
            )
        )

        sell = sum(
            num(quote.get(k))
            for k in (
                "f40",
                "f38",
                "f36",
                "f34",
                "f32",
            )
        )

        # ----------------------------------------------------
        # 龙虎榜净买额
        # ----------------------------------------------------

        lhb_net = (
            num(
                lhb_row.get(
                    "BILLBOARD_NET_AMT"
                )
            )
            / 100000000
        )

        stock = {

            "code": code,

            "name": name,

            "price": price,

            "pct": pct,

            "turnover": turnover,

            "market_cap":
                market_cap / 100000000,

            "lhb_net":
                lhb_net,

            "buy": buy,

            "sell": sell,

            "zt_dates":
                recent_zt[code],
        }

        stock["score"] = (
            calculate_score(stock)
        )

        if stock["score"] >= 80:

            stock["state"] = "高概率"

        elif stock["score"] >= 70:

            stock["state"] = "强势"

        else:

            stock["state"] = "观察"

        results.append(stock)

    # --------------------------------------------------------
    # 排序
    # --------------------------------------------------------

    results.sort(
        key=lambda x: (
            x["score"],
            x["lhb_net"],
            x["pct"],
        ),
        reverse=True,
    )

    return results[:50]


# ============================================================
# 缓存
# ============================================================

def get_data():

    now = time.time()

    if (
        CACHE["data"]
        and now - CACHE["ts"]
        < CACHE_SECONDS
    ):

        return (
            CACHE["data"],
            CACHE["source"],
        )

    try:

        data = scan()

        CACHE["ts"] = now

        CACHE["data"] = data

        CACHE["source"] = (
            "东方财富：行情 + 涨停池 + 龙虎榜"
        )

        return (
            data,
            CACHE["source"],
        )

    except Exception as e:

        print(
            "SCANNER_ERROR:",
            repr(e)
        )

        # 重要：
        # 不再使用假股票作为演示数据
        CACHE["ts"] = now
        CACHE["data"] = []

        CACHE["source"] = (
            "接口异常：" + str(e)[:120]
        )

        return (
            [],
            CACHE["source"],
        )


# ============================================================
# API
# ============================================================

@app.get("/api/health")
def health():

    return {
        "status": "ok",
        "time":
            datetime.now().isoformat(),
    }


@app.get("/api/scanner")
def scanner():

    data, source = get_data()

    return {

        "status": "ok",

        "source": source,

        "updated_at":
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "top3":
            data[:3],

        "data":
            data,
    }


# ============================================================
# 网页
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def home():

    data, source = get_data()

    top3 = data[:3]

    # --------------------------------------------------------
    # TOP 3
    # --------------------------------------------------------

    if top3:

        top_html = ""

        for i, x in enumerate(top3):

            top_html += f"""
            <div class="top-card">

                <div class="rank">
                    #{i + 1}
                </div>

                <div class="top-name">
                    {html.escape(x["name"])}
                </div>

                <div class="top-code">
                    {x["code"]}
                </div>

                <div class="prob">
                    {x["score"]}
                    <span>分</span>
                </div>

                <div class="pct">
                    {x["pct"]:+.2f}%
                </div>

                <div class="detail">
                    市值 {x["market_cap"]:.1f}亿
                    · 龙虎榜净额
                    {x["lhb_net"]:+.2f}亿
                    · 近3日涨停
                    {len(x["zt_dates"])}次
                </div>

            </div>
            """

    else:

        top_html = """
        <div class="empty">
            当前没有同时满足全部硬条件的股票
        </div>
        """

    # --------------------------------------------------------
    # 表格
    # --------------------------------------------------------

    rows_html = ""

    for x in data:

        rows_html += f"""
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
                {x["market_cap"]:.1f}亿
            </td>

            <td>
                {x["lhb_net"]:+.2f}亿
            </td>

            <td>
                {len(x["zt_dates"])}次
            </td>

            <td>
                <span class="score">
                    {x["score"]}
                </span>
            </td>

        </tr>
        """

    if not rows_html:

        rows_html = """
        <tr>
            <td colspan="9" class="empty">
                暂无符合全部硬条件的股票
            </td>
        </tr>
        """

    updated = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    return f"""
<!doctype html>

<html lang="zh-CN">

<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,
             initial-scale=1,
             maximum-scale=1"
>

<meta
    http-equiv="refresh"
    content="60"
>

<title>妖股雷达</title>

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

    padding:18px 12px;
}}

h1 {{

    margin:0;

    font-size:27px;
}}

.sub {{

    margin-top:7px;

    margin-bottom:14px;

    color:#8390a3;

    line-height:1.6;
}}

.status {{

    display:inline-block;

    padding:7px 10px;

    border-radius:9px;

    background:#111b25;

    border:1px solid #263443;

    color:#62e59a;

    font-size:12px;

    margin-bottom:14px;
}}

.tops {{

    display:grid;

    grid-template-columns:
        repeat(3,1fr);

    gap:10px;

    margin-bottom:14px;
}}

.top-card,
.box {{

    background:#101722;

    border:1px solid #202d3c;

    border-radius:14px;
}}

.top-card {{

    padding:15px;

    position:relative;
}}

.rank {{

    color:#7f8da1;

    font-size:12px;
}}

.top-name {{

    font-size:20px;

    font-weight:700;

    margin-top:5px;
}}

.top-code {{

    color:#7f8da1;

    font-size:12px;

    margin-top:3px;
}}

.prob {{

    display:inline-block;

    margin-top:12px;

    font-size:31px;

    font-weight:800;

    color:#ffb54a;
}}

.prob span {{

    font-size:13px;

    font-weight:400;
}}

.pct {{

    display:inline-block;

    margin-left:10px;

    color:#ff4d67;

    font-size:18px;

    font-weight:700;
}}

.detail {{

    margin-top:8px;

    color:#8190a4;

    font-size:12px;

    line-height:1.6;
}}

.box {{

    overflow:auto;
}}

table {{

    width:100%;

    min-width:900px;

    border-collapse:collapse;
}}

th,
td {{

    padding:12px 10px;

    border-bottom:
        1px solid #202d3c;

    text-align:left;
}}

th {{

    color:#8795a9;

    font-size:12px;

    font-weight:500;
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

    padding:30px;

    text-align:center;

    color:#7f8da1;
}}

.note {{

    margin-top:12px;

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

    .wrap {{

        padding:15px 10px;
    }}

    .top-name {{

        font-size:19px;
    }}

}}

</style>

</head>

<body>

<div class="wrap">

<h1>🔥 妖股雷达</h1>

<div class="sub">

硬条件：
近3交易日涨停 +
龙虎榜 +
总市值 ≤ 300亿

<br>

评分：
涨幅 + 换手率 + 龙虎榜净额 +
近3日涨停次数 + 委买委卖强弱

</div>

<div class="status">

● {html.escape(source)}

</div>

<div class="tops">

{top_html}

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

<th>妖股概率</th>

</tr>

</thead>

<tbody>

{rows_html}

</tbody>

</table>

</div>

<div class="note">

更新时间：
{updated}

<br>

数据：
东方财富公开行情、涨停池、龙虎榜接口

<br>

竞价历史快照如果接口没有提供，
系统不会伪造数据；可取得的早盘盘口数据参与评分。

<br>

⚠️ 妖股概率为量化筛选评分，仅用于研究，
不构成投资建议。

</div>

</div>

</body>

</html>
"""


# ============================================================
# 启动
# ============================================================

# Render 使用 Dockerfile 中的 uvicorn 启动：
# uvicorn main:app --host 0.0.0.0 --port $PORT
