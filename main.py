import os
import json
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

# =========================================================
# 配置
# =========================================================

MX_API_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"
MX_APIKEY = os.getenv("MX_APIKEY", "").strip()

EASTMONEY_KLINE = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
)

# 五条硬条件
HARD_RULES = [
    "连续5日上涨",
    "30日内有过涨停",
    "收盘价不破5日线",
    "成交量堆量",
    "底部筹码不动",
]

ALIASES = {
    "C1": ["连续5日上涨", "5日连续上涨", "连续五日上涨"],
    "C2": ["30日内有过涨停", "30日涨停", "近30日涨停"],
    "C3": ["收盘价不破5日线", "收盘不破5日线", "不破5日线"],
    "C4": ["成交量堆量", "堆量", "量能堆量"],
    "C5": ["底部筹码不动", "底部筹码", "筹码不动"],
}


# =========================================================
# 基础工具
# =========================================================

def normalize(x):
    if x is None:
        return ""
    return str(x).strip()


def num(x, default=None):
    try:
        if x is None or x == "":
            return default
        s = str(x).replace(",", "").replace("%", "").strip()
        return float(s)
    except Exception:
        return default


def as_list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        return [x]
    return []


def unwrap(obj):
    """
    兼容东方财富妙想不同返回结构
    """
    if not isinstance(obj, dict):
        return []

    candidates = [
        obj.get("data", {}).get("data", {}).get("result", {}).get("dataList", []),
        obj.get("data", {}).get("result", {}).get("dataList", []),
        obj.get("data", {}).get("result", {}).get("data", []),
        obj.get("data", {}).get("dataList", []),
        obj.get("dataList", []),
        obj.get("result", {}).get("dataList", []),
        obj.get("result", []),
    ]

    for x in candidates:
        if isinstance(x, list):
            return x

    return []


def parse_markdown_table(text):
    rows = []

    if not isinstance(text, str):
        return rows

    lines = [
        x.strip()
        for x in text.splitlines()
        if "|" in x
    ]

    if len(lines) < 2:
        return rows

    header = [
        x.strip()
        for x in lines[0].strip("|").split("|")
    ]

    for line in lines[1:]:
        if re.match(r"^\|?\s*:?-+:?", line):
            continue

        values = [
            x.strip()
            for x in line.strip("|").split("|")
        ]

        if len(values) != len(header):
            continue

        rows.append(dict(zip(header, values)))

    return rows


def find_value(row, names):
    if not isinstance(row, dict):
        return None

    for k, v in row.items():
        key = normalize(k).lower()

        for name in names:
            if normalize(name).lower() == key:
                return v

    # 模糊匹配
    for k, v in row.items():
        key = normalize(k)

        for name in names:
            if normalize(name) in key:
                return v

    return None


# =========================================================
# 条件判断
# =========================================================

def bool_condition(v):
    if isinstance(v, bool):
        return v

    if v is None:
        return None

    s = normalize(v).lower()

    if s in [
        "是",
        "满足",
        "符合",
        "true",
        "yes",
        "1",
        "y",
        "有",
        "满足条件",
    ]:
        return True

    if s in [
        "否",
        "不满足",
        "不符合",
        "false",
        "no",
        "0",
        "n",
        "无",
        "不满足条件",
    ]:
        return False

    return None


def get_conditions(row):
    result = {}

    for i in range(1, 6):
        code = f"C{i}"
        aliases = ALIASES[code]

        value = find_value(
            row,
            [code] + aliases
        )

        result[code] = bool_condition(value)

    # 如果接口返回的是“满足条件”文字
    condition_text = find_value(
        row,
        [
            "满足条件",
            "条件",
            "选股条件",
            "condition",
            "conditions",
        ],
    )

    if condition_text:
        txt = normalize(condition_text)

        for i in range(1, 6):
            code = f"C{i}"

            if result[code] is None:
                hit = any(
                    a in txt
                    for a in ALIASES[code]
                )

                result[code] = hit

    return result


def missing_indices(conditions):
    missing = []

    for i in range(1, 6):
        if conditions.get(f"C{i}") is not True:
            missing.append(i)

    return missing


def hard_count(conditions):
    return sum(
        1
        for i in range(1, 6)
        if conditions.get(f"C{i}") is True
    )


# =========================================================
# 妖股雷达评分
# =========================================================

def score_stock(conditions):
    count = hard_count(conditions)

    # 综合评分，不叫“概率”
    score = count * 20

    return score


def get_missing_text(conditions):
    missing = missing_indices(conditions)

    if not missing:
        return "无"

    return "、".join(
        HARD_RULES[i - 1]
        for i in missing
    )


# =========================================================
# 买点 / 持有 / 卖点
# =========================================================

def rule_buy(row):
    text = json.dumps(
        row,
        ensure_ascii=False
    )

    signals = []

    if "援军" in text:
        signals.append("援军战法")

    if "反转阴" in text:
        signals.append("反转阴低点")

    if "仙人指路" in text:
        signals.append("仙人指路")

    if "双剑合璧" in text:
        signals.append("双剑合璧")

    if "倚天剑" in text:
        signals.append("倚天剑")

    if "屠龙刀" in text:
        signals.append("屠龙刀")

    if "九阴九阳" in text:
        signals.append("九阴九阳")

    if not signals:
        return "等待回踩确认"

    return "、".join(signals)


def rule_hold(row):
    return "高点高、低点高、收盘高"


def rule_sell(row):
    return (
        "高点不创新高，"
        "收盘不高于前一日，"
        "低点不高于前一日低点"
    )


def get_signal_text(row):
    text = json.dumps(
        row,
        ensure_ascii=False
    )

    signals = []

    for x in [
        "援军战法",
        "破位阴",
        "加速阴",
        "反转阴",
        "九阴九阳",
        "仙人指路",
        "双剑合璧",
        "倚天剑",
        "屠龙刀",
    ]:
        if x in text:
            signals.append(x)

    return "、".join(signals) if signals else "暂无"


# =========================================================
# 股票对象
# =========================================================

def build_stock(row):
    conditions = get_conditions(row)

    # 至少有一个条件才能进入候选
    if all(v is None for v in conditions.values()):
        return None

    # 未明确返回的条件不算满足
    for i in range(1, 6):
        if conditions.get(f"C{i}") is None:
            conditions[f"C{i}"] = False

    score = score_stock(conditions)

    # 只显示 3/5 以上
    if score < 60:
        return None

    code = find_value(
        row,
        [
            "SECURITY_CODE",
            "股票代码",
            "证券代码",
            "代码",
            "CODE",
        ],
    )

    name = find_value(
        row,
        [
            "SECURITY_SHORT_NAME",
            "股票简称",
            "证券简称",
            "名称",
            "股票名称",
            "NAME",
        ],
    )

    price = find_value(
        row,
        [
            "NEWEST_PRICE",
            "最新价",
            "现价",
            "价格",
        ],
    )

    pct = find_value(
        row,
        [
            "CHG",
            "涨跌幅",
            "涨幅",
        ],
    )

    market_cap = find_value(
        row,
        [
            "TOTAL_MARKET_CAP",
            "TOTAL_CAP",
            "总市值",
            "市值",
        ],
    )

    if not code:
        return None

    code = normalize(code)
    name = normalize(name) or code

    return {
        "code": code,
        "name": name,
        "price": num(price),
        "pct": num(pct),
        "market_cap": num(market_cap),
        "score": score,
        "count": hard_count(conditions),
        "conditions": conditions,
        "missing": get_missing_text(conditions),
        "buy": rule_buy(row),
        "hold": rule_hold(row),
        "sell": rule_sell(row),
        "signals": get_signal_text(row),
    }


# =========================================================
# 妙想选股
# =========================================================

def build_query(date_str):
    return f"""
请严格按照截至 {date_str} 的历史收盘数据进行选股。

只允许使用以下5条硬条件：

C1：连续5日上涨
C2：30日内有过涨停
C3：收盘价不破5日线
C4：成交量堆量
C5：底部筹码不动

必须逐只股票明确返回：
C1、C2、C3、C4、C5，值只能是“是”或“否”。

同时返回：
股票代码
股票简称
最新价
涨跌幅
总市值

请返回满足3/5、4/5、5/5的股票。
不要把买点、持有、卖点规则作为硬条件。

底部筹码不动如果没有完整历史筹码数据，不允许伪造，
可以按K线量价结构给出“代理判断”，但必须明确。

最终优先返回5/5，其次4/5，其次3/5。
"""


def mx_search(query):
    if not MX_APIKEY:
        return []

    try:
        response = requests.post(
            MX_API_URL,
            headers={
                "Content-Type": "application/json",
                "apikey": MX_APIKEY,
            },
            json={
                "keyword": query,
                "pageNo": 1,
                "pageSize": 100,
            },
            timeout=45,
        )

        if response.status_code != 200:
            return []

        data = response.json()

        rows = unwrap(data)

        if rows:
            return rows

        # 有些返回结果把表格放在文本中
        text = json.dumps(
            data,
            ensure_ascii=False
        )

        return parse_markdown_table(text)

    except Exception:
        return []


# =========================================================
# 当前扫描
# =========================================================

def scan(date_str=None):
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    rows = mx_search(
        build_query(date_str)
    )

    stocks = []

    for row in rows:
        stock = build_stock(row)

        if stock:
            stocks.append(stock)

    # 去重
    unique = {}

    for stock in stocks:
        unique[stock["code"]] = stock

    stocks = list(unique.values())

    stocks.sort(
        key=lambda x: (
            x["score"],
            x["pct"] or -999
        ),
        reverse=True,
    )

    strong = [
        x for x in stocks
        if x["count"] == 5
    ]

    near = [
        x for x in stocks
        if x["count"] == 4
    ]

    watch = [
        x for x in stocks
        if x["count"] == 3
    ]

    # TOP3
    if strong:
        top3 = strong[:3]
        top3_title = "5/5 强势池 TOP3"
    else:
        top3 = near[:3]
        top3_title = "今日无5/5，4/5替补 TOP3"

    for i, x in enumerate(top3, 1):
        x["rank"] = i

    for i, x in enumerate(
        strong + near + watch,
        1,
    ):
        x["global_rank"] = i

    return {
        "date": date_str,
        "top3": top3,
        "top3_title": top3_title,
        "strong": strong,
        "near": near,
        "watch": watch,
        "ranking": strong + near + watch,
    }


# =========================================================
# 东方财富历史K线
# =========================================================

def market_sec_id(code):
    code = normalize(code)

    if code.startswith(("6", "68", "689")):
        return f"1.{code}"

    if code.startswith(("0", "2", "3")):
        return f"0.{code}"

    return None


def get_kline(code, beg="20200101", end="29991231"):
    secid = market_sec_id(code)

    if not secid:
        return []

    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": beg,
        "end": end,
        "lmt": "1000",
    }

    try:
        r = requests.get(
            EASTMONEY_KLINE,
            params=params,
            timeout=20,
        )

        if r.status_code != 200:
            return []

        data = r.json()

        klines = (
            data.get("data", {})
            .get("klines", [])
        )

        result = []

        for item in klines:
            p = item.split(",")

            if len(p) < 11:
                continue

            result.append({
                "date": p[0],
                "open": num(p[1]),
                "close": num(p[2]),
                "high": num(p[3]),
                "low": num(p[4]),
                "volume": num(p[5]),
                "amount": num(p[6]),
                "amplitude": num(p[7]),
                "pct": num(p[8]),
                "change": num(p[9]),
                "turnover": num(p[10]),
            })

        return result

    except Exception:
        return []


# =========================================================
# 历史回测
# =========================================================

def is_win(entry_price, close_price):
    if entry_price is None:
        return False

    if close_price is None:
        return False

    return close_price > entry_price


def backtest_one_stock(stock, signal_date):
    """
    信号日收盘确认。
    下一交易日开盘买入。
    """

    code = stock["code"]

    try:
        d = datetime.strptime(
            signal_date,
            "%Y-%m-%d"
        )

        beg = (
            d - timedelta(days=10)
        ).strftime("%Y%m%d")

        end = (
            d + timedelta(days=15)
        ).strftime("%Y%m%d")

    except Exception:
        return None

    klines = get_kline(
        code,
        beg,
        end
    )

    if not klines:
        return None

    signal_index = None

    for i, k in enumerate(klines):
        if k["date"] == signal_date:
            signal_index = i
            break

    if signal_index is None:
        return None

    # 至少需要下一个交易日
    if signal_index + 1 >= len(klines):
        return None

    entry_day = klines[
        signal_index + 1
    ]

    entry_price = entry_day["open"]

    result = {
        "code": code,
        "name": stock["name"],
        "signal_date": signal_date,
        "entry_date": entry_day["date"],
        "entry_price": entry_price,
        "score": stock["score"],
        "count": stock["count"],
    }

    for horizon in [1, 3, 5]:
        idx = signal_index + horizon

        if idx >= len(klines):
            result[f"win_{horizon}d"] = None
            result[f"return_{horizon}d"] = None
            continue

        close_price = klines[idx]["close"]

        result[f"win_{horizon}d"] = is_win(
            entry_price,
            close_price
        )

        if entry_price and close_price:
            result[f"return_{horizon}d"] = (
                close_price / entry_price - 1
            ) * 100
        else:
            result[f"return_{horizon}d"] = None

    return result


def get_trade_dates(days=60):
    """
    取得最近交易日。
    用上证指数K线作为交易日历。
    """

    end = datetime.now()
    beg = end - timedelta(days=120)

    klines = get_kline(
        "000001",
        beg.strftime("%Y%m%d"),
        end.strftime("%Y%m%d"),
    )

    dates = [
        x["date"]
        for x in klines
        if x.get("date")
    ]

    return dates[-days:]


def calculate_stats(results):
    groups = {
        "5/5": [],
        "4/5": [],
        "3/5": [],
    }

    for r in results:
        count = r.get("count")

        if count == 5:
            groups["5/5"].append(r)
        elif count == 4:
            groups["4/5"].append(r)
        elif count == 3:
            groups["3/5"].append(r)

    output = {}

    for group, arr in groups.items():

        row = {
            "样本数": len(arr),
        }

        for horizon in [1, 3, 5]:

            valid = [
                x for x in arr
                if x.get(
                    f"win_{horizon}d"
                ) is not None
            ]

            wins = [
                x for x in valid
                if x.get(
                    f"win_{horizon}d"
                )
            ]

            returns = [
                x.get(
                    f"return_{horizon}d"
                )
                for x in valid
                if x.get(
                    f"return_{horizon}d"
                ) is not None
            ]

            win_rate = (
                len(wins) / len(valid) * 100
                if valid
                else None
            )

            avg_return = (
                sum(returns) / len(returns)
                if returns
                else None
            )

            row[f"{horizon}日胜率"] = win_rate
            row[f"{horizon}日平均收益"] = avg_return

        output[group] = row

    return output


def run_backtest(days=60):
    dates = get_trade_dates(days)

    all_results = []

    # 防止请求过多
    for signal_date in dates:

        try:
            rows = mx_search(
                build_query(signal_date)
            )

            stocks = []

            for row in rows:
                stock = build_stock(row)

                if stock:
                    stocks.append(stock)

            # 同一天最多回测前20只
            stocks = sorted(
                stocks,
                key=lambda x: x["score"],
                reverse=True,
            )[:20]

            for stock in stocks:

                result = backtest_one_stock(
                    stock,
                    signal_date
                )

                if result:
                    all_results.append(result)

                time.sleep(0.05)

        except Exception:
            continue

    stats = calculate_stats(
        all_results
    )

    return {
        "days": days,
        "trade_days": len(dates),
        "sample_count": len(all_results),
        "stats": stats,
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
      content="width=device-width,initial-scale=1.0">

<title>🔥 妖股雷达</title>

<style>

*{
    box-sizing:border-box;
}

body{
    margin:0;
    background:#080808;
    color:#eee;
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "PingFang SC",
        "Microsoft YaHei",
        sans-serif;
}

.container{
    max-width:1100px;
    margin:auto;
    padding:15px;
}

.header{
    background:#111;
    border:1px solid #262626;
    border-radius:15px;
    padding:18px;
    margin-bottom:15px;
}

.title{
    font-size:27px;
    font-weight:800;
}

.subtitle{
    color:#999;
    margin-top:7px;
    font-size:13px;
}

.toolbar{
    display:flex;
    gap:8px;
    margin-top:15px;
    flex-wrap:wrap;
}

input,
select,
button{
    background:#181818;
    border:1px solid #333;
    color:#fff;
    padding:10px 12px;
    border-radius:9px;
}

button{
    background:#d71920;
    border-color:#d71920;
    font-weight:700;
}

button.secondary{
    background:#202020;
    border-color:#444;
}

.section{
    margin-top:16px;
}

.section-title{
    font-size:19px;
    font-weight:800;
    margin-bottom:9px;
}

.grid{
    display:grid;
    grid-template-columns:
        repeat(auto-fit,minmax(280px,1fr));
    gap:10px;
}

.card{
    background:#111;
    border:1px solid #292929;
    border-radius:14px;
    padding:14px;
}

.card.hot{
    border-color:#c51b24;
}

.card.orange{
    border-color:#b96a13;
}

.card.yellow{
    border-color:#b5a018;
}

.stock-head{
    display:flex;
    justify-content:space-between;
    align-items:center;
}

.stock-name{
    font-size:20px;
    font-weight:800;
}

.score{
    font-size:18px;
    font-weight:900;
    color:#ff4242;
}

.meta{
    color:#999;
    font-size:12px;
    margin-top:5px;
}

.rules{
    margin-top:12px;
    line-height:1.8;
    font-size:13px;
}

.buy{
    color:#ff4545;
}

.hold{
    color:#f4c542;
}

.sell{
    color:#48d597;
}

.missing{
    color:#ff9d35;
}

.table-wrap{
    overflow:auto;
    background:#111;
    border:1px solid #292929;
    border-radius:14px;
}

table{
    width:100%;
    border-collapse:collapse;
    min-width:650px;
}

th,
td{
    padding:10px;
    border-bottom:1px solid #222;
    text-align:left;
    font-size:13px;
}

th{
    color:#999;
}

.red{
    color:#ff4545;
}

.green{
    color:#48d597;
}

.orange-text{
    color:#ff9d35;
}

.yellow-text{
    color:#f4d35e;
}

.empty{
    color:#777;
    padding:20px;
    text-align:center;
}

.loading{
    color:#aaa;
    padding:20px;
}

.backtest-box{
    background:#101010;
    border:1px solid #292929;
    border-radius:14px;
    padding:15px;
}

.stats-grid{
    display:grid;
    grid-template-columns:
        repeat(auto-fit,minmax(240px,1fr));
    gap:10px;
    margin-top:10px;
}

.stat-card{
    background:#171717;
    border:1px solid #292929;
    border-radius:12px;
    padding:14px;
}

.stat-title{
    font-size:17px;
    font-weight:800;
    margin-bottom:10px;
}

.stat-row{
    display:flex;
    justify-content:space-between;
    padding:6px 0;
    border-bottom:1px solid #252525;
}

.notice{
    color:#888;
    font-size:12px;
    line-height:1.7;
    margin-top:10px;
}

</style>
</head>

<body>

<div class="container">

<div class="header">

<div class="title">
🔥 妖股雷达
</div>

<div class="subtitle">
5条硬条件 + 买点/持有/卖点规则 · 东方财富公开行情数据
</div>

<div class="toolbar">

<input
    id="date"
    type="date"
/>

<button onclick="scan()">
开始扫描
</button>

<button
    class="secondary"
    onclick="backtest()">
历史回测胜率
</button>

</div>

</div>


<div class="section">

<div class="section-title">
① 今日硬条件
</div>

<div class="card">

<div class="rules">

① 连续5日上涨<br>
② 30日内有过涨停<br>
③ 收盘价不破5日线<br>
④ 成交量堆量<br>
⑤ 底部筹码不动

</div>

</div>

</div>


<div class="section">

<div class="section-title" id="topTitle">
🔥 TOP3
</div>

<div id="top3"
     class="grid">

<div class="empty">
暂无数据
</div>

</div>

</div>


<div class="section">

<div class="section-title">
🟠 4/5 高度接近
</div>

<div id="near"
     class="grid">

<div class="empty">
暂无符合条件的股票
</div>

</div>

</div>


<div class="section">

<div class="section-title">
🟡 3/5 观察池
</div>

<div id="watch"
     class="grid">

<div class="empty">
暂无符合条件的股票
</div>

</div>

</div>


<div class="section">

<div class="section-title">
📊 综合评分排行
</div>

<div class="table-wrap">

<table>

<thead>
<tr>
<th>排名</th>
<th>股票</th>
<th>评分</th>
<th>硬条件</th>
<th>涨跌幅</th>
<th>缺少条件</th>
</tr>
</thead>

<tbody id="ranking">

<tr>
<td colspan="6"
    class="empty">
暂无数据
</td>
</tr>

</tbody>

</table>

</div>

</div>


<div class="section">

<div class="section-title">
📈 历史回测胜率
</div>

<div class="backtest-box">

<div class="toolbar">

<select id="btDays">

<option value="30">
最近30个交易日
</option>

<option value="60" selected>
最近60个交易日
</option>

</select>

<button
    onclick="backtest()">
开始回测
</button>

</div>

<div id="backtestResult">

<div class="empty">
点击“历史回测胜率”开始计算
</div>

</div>

<div class="notice">

回测规则：信号日收盘确认，下一交易日开盘买入。
1日/3日/5日胜率分别按对应交易日收盘价高于买入价计算。
仅作为历史统计，不代表未来收益。

</div>

</div>

</div>

</div>


<script>

function money(v){
    if(v === null || v === undefined){
        return "--";
    }
    return Number(v).toFixed(2);
}

function pct(v){
    if(v === null || v === undefined){
        return "--";
    }

    return Number(v).toFixed(2) + "%";
}


function card(stock){

    let cls = "card";

    if(stock.count === 5){
        cls += " hot";
    }
    else if(stock.count === 4){
        cls += " orange";
    }
    else{
        cls += " yellow";
    }

    return `
    <div class="${cls}">

        <div class="stock-head">

            <div>
                <div class="stock-name">
                    ${stock.name}
                </div>

                <div class="meta">
                    ${stock.code}
                </div>
            </div>

            <div class="score">
                ${stock.score}分
            </div>

        </div>

        <div class="meta">
            ${stock.count}/5
            · 涨跌幅 ${pct(stock.pct)}
        </div>

        <div class="rules">

            <div class="buy">
                买点：${stock.buy}
            </div>

            <div class="hold">
                持有：${stock.hold}
            </div>

            <div class="sell">
                卖点：${stock.sell}
            </div>

            <div>
                触发：${stock.signals}
            </div>

            <div class="missing">
                缺少条件：${stock.missing}
            </div>

        </div>

    </div>
    `;
}


function renderList(id, list){

    const el =
        document.getElementById(id);

    if(!list || list.length === 0){

        el.innerHTML =
            `<div class="empty">
                暂无符合条件的股票
             </div>`;

        return;
    }

    el.innerHTML =
        list.map(card).join("");
}


async function scan(){

    const date =
        document.getElementById("date").value;

    document.getElementById("top3").innerHTML =
        `<div class="loading">
            正在扫描...
         </div>`;

    try{

        const url =
            "/api/scan?date="
            + encodeURIComponent(date);

        const r =
            await fetch(url);

        const data =
            await r.json();

        document.getElementById("topTitle")
            .innerText =
            "🔥 " + data.top3_title;

        renderList(
            "top3",
            data.top3
        );

        renderList(
            "near",
            data.near
        );

        renderList(
            "watch",
            data.watch
        );

        const tbody =
            document.getElementById(
                "ranking"
            );

        if(!data.ranking ||
           data.ranking.length === 0){

            tbody.innerHTML =
                `<tr>
                    <td colspan="6"
                        class="empty">
                        暂无3/5以上股票
                    </td>
                 </tr>`;

            return;
        }

        tbody.innerHTML =
            data.ranking.map(
                (x,i) => `
                <tr>

                    <td>${i+1}</td>

                    <td>
                        ${x.name}
                        <br>
                        <span class="meta">
                            ${x.code}
                        </span>
                    </td>

                    <td class="red">
                        ${x.score}
                    </td>

                    <td>
                        ${x.count}/5
                    </td>

                    <td class="${
                        x.pct >= 0
                        ? "red"
                        : "green"
                    }">
                        ${pct(x.pct)}
                    </td>

                    <td class="orange-text">
                        ${x.missing}
                    </td>

                </tr>
                `
            ).join("");

    }
    catch(e){

        document.getElementById("top3")
            .innerHTML =
            `<div class="empty">
                扫描失败，请稍后再试
             </div>`;
    }
}


async function backtest(){

    const days =
        document.getElementById(
            "btDays"
        ).value;

    const box =
        document.getElementById(
            "backtestResult"
        );

    box.innerHTML =
        `<div class="loading">
            正在进行历史回测，请稍候……
            <br>
            正在读取历史交易日、历史选股结果和K线。
         </div>`;

    try{

        const r =
            await fetch(
                "/api/backtest?days="
                + days
            );

        const data =
            await r.json();

        if(data.error){

            box.innerHTML =
                `<div class="empty">
                    ${data.error}
                 </div>`;

            return;
        }

        let html =
            `<div class="stats-grid">`;

        for(
            const [group,stat]
            of Object.entries(data.stats)
        ){

            html += `
            <div class="stat-card">

                <div class="stat-title">
                    ${group}
                </div>

                <div class="stat-row">
                    <span>样本数</span>
                    <b>${stat["样本数"]}</b>
                </div>

                <div class="stat-row">
                    <span>次日胜率</span>
                    <b class="red">
                        ${
                            stat["1日胜率"] == null
                            ? "--"
                            : stat["1日胜率"].toFixed(2)+"%"
                        }
                    </b>
                </div>

                <div class="stat-row">
                    <span>3日胜率</span>
                    <b class="red">
                        ${
                            stat["3日胜率"] == null
                            ? "--"
                            : stat["3日胜率"].toFixed(2)+"%"
                        }
                    </b>
                </div>

                <div class="stat-row">
                    <span>5日胜率</span>
                    <b class="red">
                        ${
                            stat["5日胜率"] == null
                            ? "--"
                            : stat["5日胜率"].toFixed(2)+"%"
                        }
                    </b>
                </div>

                <div class="stat-row">
                    <span>5日平均收益</span>
                    <b>
                        ${
                            stat["5日平均收益"] == null
                            ? "--"
                            : stat["5日平均收益"].toFixed(2)+"%"
                        }
                    </b>
                </div>

            </div>
            `;
        }

        html += `</div>`;

        html += `
        <div class="notice">
            共检查 ${data.trade_days} 个交易日，
            有效回测样本 ${data.sample_count} 条。
        </div>
        `;

        box.innerHTML = html;

    }
    catch(e){

        box.innerHTML =
            `<div class="empty">
                回测失败，请稍后重试
             </div>`;
    }
}


// 默认今天
(function(){

    const d = new Date();

    const yyyy =
        d.getFullYear();

    const mm =
        String(
            d.getMonth()+1
        ).padStart(2,"0");

    const dd =
        String(
            d.getDate()
        ).padStart(2,"0");

    document.getElementById(
        "date"
    ).value =
        `${yyyy}-${mm}-${dd}`;

})();

</script>

</body>
</html>
"""


# =========================================================
# API
# =========================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def home():

    return HTMLResponse(
        HTML
    )


@app.get("/api/scan")
def api_scan(
    date: Optional[str] = None
):

    try:
        return scan(date)

    except Exception as e:

        return {
            "date": date,
            "top3": [],
            "top3_title": "扫描失败",
            "strong": [],
            "near": [],
            "watch": [],
            "ranking": [],
            "error": str(e),
        }


@app.get("/api/backtest")
def api_backtest(
    days: int = 60
):

    try:

        days = max(
            10,
            min(days, 120)
        )

        result = run_backtest(
            days
        )

        return result

    except Exception as e:

        return {
            "error":
                "历史回测暂时无法完成："
                + str(e)
        }
