from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests
import json
import re
import time
from urllib.parse import quote
from threading import Lock

app = FastAPI(title="妖股雷达")

# =========================
# 基础配置
# =========================

MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://www.eastmoney.com/",
}

CACHE_SECONDS = 60

_cache = {
    "time": 0,
    "data": []
}

_cache_lock = Lock()


# =========================
# 通用工具
# =========================

def safe_float(v, default=0.0):
    try:
        if v is None:
            return default

        if isinstance(v, (int, float)):
            return float(v)

        s = str(v).strip()

        if not s:
            return default

        s = (
            s.replace(",", "")
             .replace("%", "")
             .replace("亿", "")
             .replace("万", "")
             .replace("元", "")
        )

        return float(s)

    except Exception:
        return default


def safe_int(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default


def clean_code(code):
    if not code:
        return ""

    s = str(code).strip()

    m = re.search(r"\d{6}", s)

    if m:
        return m.group(0)

    return s


def market_of(code):
    """
    东方财富市场代码：
    上海 1
    深圳 0
    北交所 0
    """
    code = clean_code(code)

    if code.startswith(("6", "68")):
        return "1"

    return "0"


# =========================
# 妙想接口
# =========================

def mx_query():
    """
    妙想负责寻找当天相对活跃的候选股票。

    注意：
    涨停、龙虎榜、市值三个指标只是评分依据，
    不要求三个条件必须全部满足。
    """

    query = """
请筛选A股当前交易日中相对活跃、强势、具有短线博弈价值的股票，
用于短线强势股排名。

重点参考以下指标：
1. 近3个交易日是否出现过涨停；
2. 是否上过龙虎榜；
3. 总市值是否在300亿元以内；
4. 当日涨幅；
5. 当日换手率。

其中：
- 近3日涨停
- 龙虎榜
- 市值300亿元以内

这三个指标不要作为必须同时满足的硬性条件，
即使股票缺少其中一个条件，也可以返回，
后续由程序综合评分排名。

排除：
ST、*ST、退市股票。

尽可能返回至少30只候选股票。

返回JSON数组，每只股票包含：
code
name
price
pct
turnover

不要返回解释文字，只返回JSON。
"""

    payload = {
        "query": query
    }

    try:
        r = requests.post(
            MX_URL,
            headers=HEADERS,
            json=payload,
            timeout=18
        )

        r.raise_for_status()

        return r.text

    except Exception:
        return ""


def parse_mx(text):
    if not text:
        return []

    # 先尝试直接JSON
    try:
        obj = json.loads(text)

        if isinstance(obj, list):
            return obj

        if isinstance(obj, dict):
            for key in ["data", "result", "stocks", "items", "rows"]:
                value = obj.get(key)

                if isinstance(value, list):
                    return value

    except Exception:
        pass

    # 再从文本里寻找JSON数组
    m = re.search(r"\[[\s\S]*\]", text)

    if m:
        try:
            obj = json.loads(m.group(0))

            if isinstance(obj, list):
                return obj

        except Exception:
            pass

    # 最后尝试提取股票代码
    result = []

    codes = re.findall(r"\b[03689]\d{5}\b", text)

    seen = set()

    for code in codes:
        if code in seen:
            continue

        seen.add(code)

        result.append({
            "code": code,
            "name": "",
            "price": 0,
            "pct": 0,
            "turnover": 0
        })

    return result


def parse_candidates(raw):
    data = parse_mx(raw)

    result = []
    seen = set()

    for item in data:

        if not isinstance(item, dict):
            continue

        code = (
            item.get("code")
            or item.get("代码")
            or item.get("证券代码")
            or item.get("stock_code")
        )

        code = clean_code(code)

        if not re.fullmatch(r"\d{6}", code):
            continue

        if code in seen:
            continue

        name = (
            item.get("name")
            or item.get("名称")
            or item.get("股票名称")
            or ""
        )

        if "ST" in str(name).upper():
            continue

        price = (
            item.get("price")
            or item.get("现价")
            or item.get("最新价")
            or 0
        )

        pct = (
            item.get("pct")
            or item.get("涨幅")
            or item.get("涨跌幅")
            or 0
        )

        turnover = (
            item.get("turnover")
            or item.get("换手率")
            or 0
        )

        result.append({
            "code": code,
            "name": str(name),
            "price": safe_float(price),
            "pct": safe_float(pct),
            "turnover": safe_float(turnover)
        })

        seen.add(code)

    return result


# =========================
# 东方财富行情
# =========================

def get_quotes(codes):
    if not codes:
        return {}

    result = {}

    # 东方财富单次不要塞太多
    for start in range(0, len(codes), 300):

        part = codes[start:start + 300]

        secids = ",".join(
            f"{market_of(code)}.{code}"
            for code in part
        )

        params = {
            "pn": 1,
            "pz": len(part),
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": f"b:{market_of(part[0])}+f:!50",
            "fields": "f2,f3,f8,f12,f13,f14,f20,f21",
            "secids": secids
        }

        try:
            r = requests.get(
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                params=params,
                headers=HEADERS,
                timeout=10
            )

            obj = r.json()

            diff = (
                obj.get("data", {})
                   .get("diff", [])
            )

            if isinstance(diff, dict):
                diff = list(diff.values())

            for item in diff:

                code = clean_code(item.get("f12"))

                if not code:
                    continue

                result[code] = {
                    "price": safe_float(item.get("f2")),
                    "pct": safe_float(item.get("f3")),
                    "turnover": safe_float(item.get("f8")),
                    "market_cap": (
                        safe_float(item.get("f20")) / 1e8
                        if safe_float(item.get("f20")) > 0
                        else 0
                    ),
                    "name": item.get("f14") or ""
                }

        except Exception:
            continue

    return result


# =========================
# 获取最近交易日
# =========================

def get_trading_dates(count=3):
    """
    从东方财富涨停池反推最近交易日。
    """

    dates = []

    now = time.localtime()

    # 最多向前寻找20个自然日
    for offset in range(0, 20):

        t = time.time() - offset * 86400

        date_str = time.strftime(
            "%Y%m%d",
            time.localtime(t)
        )

        params = {
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt",
            "Pageindex": 0,
            "pagesize": 1,
            "sort": "m:260",
            "date": date_str
        }

        try:
            r = requests.get(
                "https://push2ex.eastmoney.com/getTopicZTPool",
                params=params,
                headers=HEADERS,
                timeout=8
            )

            obj = r.json()

            pool = (
                obj.get("data", {})
                   .get("pool", [])
            )

            if pool:
                real_date = (
                    time.strftime(
                        "%Y-%m-%d",
                        time.strptime(date_str, "%Y%m%d")
                    )
                )

                if real_date not in dates:
                    dates.append(real_date)

            if len(dates) >= count:
                break

        except Exception:
            continue

    return dates


# =========================
# 获取涨停次数
# =========================

def get_limit_up_counts(codes, dates):
    counts = {
        code: 0
        for code in codes
    }

    if not dates:
        return counts

    for date in dates:

        date_compact = date.replace("-", "")

        params = {
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt",
            "Pageindex": 0,
            "pagesize": 2000,
            "sort": "m:260",
            "date": date_compact
        }

        try:
            r = requests.get(
                "https://push2ex.eastmoney.com/getTopicZTPool",
                params=params,
                headers=HEADERS,
                timeout=10
            )

            obj = r.json()

            pool = (
                obj.get("data", {})
                   .get("pool", [])
            )

            for item in pool:

                code = clean_code(
                    item.get("c")
                    or item.get("code")
                )

                if code in counts:
                    counts[code] += 1

        except Exception:
            continue

    return counts


# =========================
# 龙虎榜
# =========================

def get_lhb(codes, dates):
    """
    一次请求最近一段时间的龙虎榜，
    避免逐日请求造成 Render 超时。
    """

    result = {
        code: {
            "on_board": False,
            "lhb_net": 0.0
        }
        for code in codes
    }

    if not codes or not dates:
        return result

    start_date = min(dates)
    end_date = max(dates)

    # 东方财富 DataCenter 日期格式
    start_fmt = start_date.replace("-", "")
    end_fmt = end_date.replace("-", "")

    # 不直接把复杂条件交给 requests 二次编码
    filter_text = (
        f"(TRADE_DATE>='{start_fmt}')"
        f"(TRADE_DATE<='{end_fmt}')"
    )

    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
    )

    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": (
            "SECURITY_CODE,SECURITY_NAME_ABBR,"
            "TRADE_DATE,BILLBOARD_NET_AMT"
        ),
        "pageNumber": 1,
        "pageSize": 5000,
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
        "filter": filter_text
    }

    try:
        r = requests.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=12
        )

        obj = r.json()

        data = (
            obj.get("result", {})
               .get("data", [])
        )

        if not isinstance(data, list):
            return result

        for item in data:

            code = clean_code(
                item.get("SECURITY_CODE")
            )

            if code not in result:
                continue

            result[code]["on_board"] = True

            net = safe_float(
                item.get("BILLBOARD_NET_AMT")
            )

            # 龙虎榜金额一般为元
            if abs(net) >= 100000000:
                net = net / 100000000

            result[code]["lhb_net"] += net

    except Exception:
        pass

    return result


# =========================
# 核心评分
# =========================

def make_result(candidates):
    if not candidates:
        return []

    codes = [
        x["code"]
        for x in candidates
        if x.get("code")
    ]

    quotes = get_quotes(codes)

    # 最近3个交易日
    dates3 = get_trading_dates(3)

    # 最近10个交易日用于龙虎榜
    dates10 = get_trading_dates(10)

    zt_counts = get_limit_up_counts(
        codes,
        dates3
    )

    lhb_data = get_lhb(
        codes,
        dates10
    )

    result = []

    for item in candidates:

        code = item["code"]

        quote = quotes.get(code, {})

        price = (
            quote.get("price")
            or item.get("price")
            or 0
        )

        pct = (
            quote.get("pct")
            if quote.get("pct") is not None
            else item.get("pct", 0)
        )

        turnover = (
            quote.get("turnover")
            if quote.get("turnover") is not None
            else item.get("turnover", 0)
        )

        market_cap = safe_float(
            quote.get("market_cap")
        )

        name = (
            quote.get("name")
            or item.get("name")
            or ""
        )

        zt_count = zt_counts.get(
            code,
            0
        )

        lhb = lhb_data.get(
            code,
            {
                "on_board": False,
                "lhb_net": 0
            }
        )

        on_board = bool(
            lhb.get("on_board")
        )

        lhb_net = safe_float(
            lhb.get("lhb_net")
        )

        # =========================
        # 综合评分
        # =========================

        score = 0

        # ① 近3日涨停：评分项
        if zt_count >= 3:
            score += 35
        elif zt_count == 2:
            score += 30
        elif zt_count == 1:
            score += 25

        # ② 龙虎榜：评分项
        if on_board:
            score += 20

            if lhb_net > 1:
                score += 8
            elif lhb_net > 0:
                score += 5

        # ③ 市值：评分项
        if market_cap > 0:

            if market_cap <= 50:
                score += 15

            elif market_cap <= 100:
                score += 12

            elif market_cap <= 200:
                score += 8

            elif market_cap <= 300:
                score += 5

        # ④ 当日涨幅
        if pct >= 9.5:
            score += 15
        elif pct >= 7:
            score += 12
        elif pct >= 5:
            score += 8
        elif pct >= 3:
            score += 4

        # ⑤ 换手率
        if turnover > 29.25:
            score += 15
        elif turnover > 20:
            score += 10
        elif turnover > 15:
            score += 7
        elif turnover > 8:
            score += 4

        # 最大100分
        score = min(score, 100)

        # 三个核心指标仅用于展示
        hard_count = 0

        if zt_count > 0:
            hard_count += 1

        if on_board:
            hard_count += 1

        if market_cap > 0 and market_cap <= 300:
            hard_count += 1

        result.append({
            "code": code,
            "name": name,
            "price": round(price, 2),
            "pct": round(pct, 2),
            "turnover": round(turnover, 2),
            "market_cap": round(market_cap, 2),
            "zt_count": zt_count,
            "lhb": on_board,
            "lhb_net": round(lhb_net, 2),
            "hard_count": hard_count,
            "score": score
        })

    # =========================
    # 排名
    # =========================

    result.sort(
        key=lambda x: (
            x["score"],
            x["zt_count"],
            x["lhb_net"],
            x["pct"],
            x["turnover"]
        ),
        reverse=True
    )

    return result


# =========================
# 获取数据
# =========================

def get_data():
    global _cache

    with _cache_lock:

        if (
            time.time() - _cache["time"]
            < CACHE_SECONDS
            and _cache["data"]
        ):
            return _cache["data"]

    raw = mx_query()

    candidates = parse_candidates(raw)

    if not candidates:
        return []

    data = make_result(candidates)

    with _cache_lock:
        _cache["time"] = time.time()
        _cache["data"] = data

    return data


# =========================
# API
# =========================

@app.get("/api/scanner")
def scanner():

    data = get_data()

    # 直接按综合评分取TOP3
    top3 = data[:3]

    # 统计刚好满足三个指标的股票数量
    hard_condition_count = sum(
        1
        for x in data
        if x["hard_count"] == 3
    )

    return {
        "success": True,
        "top3": top3,
        "ranking": data,
        "hard_condition_count": hard_condition_count,
        "count": len(data),
        "updated": int(time.time())
    }


# =========================
# 页面
# =========================

@app.get("/", response_class=HTMLResponse)
def home():

    data = get_data()

    # TOP3直接按照综合评分
    top3 = data[:3]

    rows = ""

    for i, x in enumerate(data, 1):

        lhb_text = "是" if x["lhb"] else "否"

        rows += f"""
        <tr>
            <td>{i}</td>
            <td><b>{x["name"]}</b></td>
            <td>{x["code"]}</td>
            <td>{x["price"]:.2f}</td>
            <td>{x["pct"]:.2f}%</td>
            <td>{x["turnover"]:.2f}%</td>
            <td>{x["market_cap"]:.2f}亿</td>
            <td>{x["zt_count"]}</td>
            <td>{lhb_text}</td>
            <td>{x["lhb_net"]:.2f}亿</td>
            <td><b>{x["score"]}</b></td>
        </tr>
        """

    top_html = ""

    for i, x in enumerate(top3, 1):

        top_html += f"""
        <div class="top-card">
            <div class="rank">TOP {i}</div>
            <div class="stock-name">
                {x["name"] or x["code"]}
            </div>
            <div class="stock-code">
                {x["code"]}
            </div>
            <div class="score">
                妖股概率评分：{x["score"]}
            </div>
            <div class="small">
                涨幅 {x["pct"]:.2f}%
               　换手 {x["turnover"]:.2f}%
               　近3日涨停 {x["zt_count"]}次
            </div>
        </div>
        """

    if not top3:
        top_html = """
        <div class="empty">
            暂无符合条件的数据
        </div>
        """

    html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1.0">

<title>🔥 妖股雷达</title>

<style>

* {{
    box-sizing: border-box;
}}

body {{
    margin: 0;
    background: #090909;
    color: #eeeeee;
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "PingFang SC",
        "Microsoft YaHei",
        Arial,
        sans-serif;
}}

.container {{
    width: 100%;
    max-width: 1200px;
    margin: auto;
    padding: 16px;
}}

.header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    margin-bottom: 15px;
}}

.title {{
    font-size: 26px;
    font-weight: bold;
}}

.status {{
    color: #888;
    font-size: 13px;
}}

.controls {{
    display: flex;
    gap: 10px;
    margin-bottom: 15px;
}}

button {{
    border: 0;
    border-radius: 8px;
    padding: 10px 16px;
    background: #e60012;
    color: white;
    font-size: 14px;
}}

button:active {{
    opacity: .7;
}}

.conditions {{
    background: #111;
    border: 1px solid #252525;
    border-radius: 10px;
    padding: 14px;
    margin-bottom: 15px;
}}

.conditions-title {{
    font-weight: bold;
    margin-bottom: 8px;
}}

.condition {{
    line-height: 1.8;
    color: #ccc;
}}

.tip {{
    color: #888;
    font-size: 12px;
    margin-top: 8px;
}}

.top-title {{
    font-size: 20px;
    font-weight: bold;
    margin: 18px 0 10px;
}}

.top-list {{
    display: grid;
    grid-template-columns:
        repeat(3, 1fr);
    gap: 12px;
}}

.top-card {{
    background: #121212;
    border: 1px solid #292929;
    border-radius: 12px;
    padding: 16px;
}}

.rank {{
    color: #ff3344;
    font-size: 13px;
    font-weight: bold;
}}

.stock-name {{
    font-size: 21px;
    font-weight: bold;
    margin-top: 6px;
}}

.stock-code {{
    color: #777;
    margin-top: 4px;
}}

.score {{
    color: #ff3344;
    font-size: 18px;
    font-weight: bold;
    margin-top: 12px;
}}

.small {{
    color: #aaa;
    font-size: 12px;
    margin-top: 8px;
    line-height: 1.7;
}}

.table-wrap {{
    overflow-x: auto;
    margin-top: 12px;
}}

table {{
    width: 100%;
    border-collapse: collapse;
    min-width: 900px;
    background: #101010;
}}

th,
td {{
    padding: 10px 8px;
    border-bottom: 1px solid #222;
    text-align: center;
    white-space: nowrap;
}}

th {{
    color: #aaa;
    background: #161616;
}}

td {{
    color: #ddd;
}}

td:nth-child(5) {{
    color: #ff3344;
}}

.empty {{
    padding: 30px;
    text-align: center;
    color: #777;
    background: #111;
    border-radius: 10px;
}}

.footer {{
    color: #555;
    text-align: center;
    font-size: 12px;
    padding: 25px 0;
}}

@media (max-width: 700px) {{

    .container {{
        padding: 10px;
    }}

    .title {{
        font-size: 22px;
    }}

    .top-list {{
        grid-template-columns: 1fr;
    }}

    .top-card {{
        padding: 14px;
    }}

}}

</style>
</head>

<body>

<div class="container">

    <div class="header">
        <div class="title">🔥 妖股雷达</div>
        <div class="status">
            数据源：东方财富 + 妙想
        </div>
    </div>

    <div class="controls">
        <button onclick="location.reload()">
            开始扫描
        </button>
    </div>

    <div class="conditions">

        <div class="conditions-title">
            帮选股核心
        </div>

        <div class="condition">
            ① 近3个交易日出现涨停
        </div>

        <div class="condition">
            ② 上过龙虎榜
        </div>

        <div class="condition">
            ③ 总市值 ≤ 300亿
        </div>

        <div class="tip">
            以上三个指标作为综合评分项，不要求全部同时满足。
            最终按综合评分排序。
        </div>

    </div>

    <div class="top-title">
        🔥 TOP3 强势标的
    </div>

    <div class="top-list">
        {top_html}
    </div>

    <div class="top-title">
        📊 妖股概率排行
    </div>

    <div class="table-wrap">

        <table>

            <thead>
                <tr>
                    <th>排名</th>
                    <th>名称</th>
                    <th>代码</th>
                    <th>价格</th>
                    <th>涨幅</th>
                    <th>换手</th>
                    <th>市值</th>
                    <th>近3日涨停</th>
                    <th>龙虎榜</th>
                    <th>龙虎榜净额</th>
                    <th>评分</th>
                </tr>
            </thead>

            <tbody>
                {rows}
            </tbody>

        </table>

    </div>

    <div class="footer">
        数据源：东方财富公开行情接口 + 妙想
    </div>

</div>

</body>
</html>
"""

    return HTMLResponse(html)


# =========================
# 启动
# =========================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=10000
    )
