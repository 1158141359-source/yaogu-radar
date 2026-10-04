import os
import json
import re
import time
from typing import Optional

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="妖股雷达")

MX_API_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"
MX_APIKEY = os.getenv("MX_APIKEY", "").strip()
TIMEOUT = 40

# =========================================================
# 5个硬条件：固定不变
# =========================================================

HARD_RULES = [
    "① 连续5日上涨",
    "② 30日内有过涨停",
    "③ 收盘不破5日线",
    "④ 堆量成交量",
    "⑤ 底部筹码不动",
]

ALIASES = [
    ["C1", "条件1", "条件①", "连续5日上涨", "连续5日涨", "连续5天上涨",
     "连续上涨5日", "5日上涨"],
    ["C2", "条件2", "条件②", "30日内有过涨停", "30日内有涨停",
     "30日涨停", "近30日涨停", "30天涨停"],
    ["C3", "条件3", "条件③", "收盘不破5日线", "收盘价不破5日线",
     "不破5日线", "5日线"],
    ["C4", "条件4", "条件④", "堆量成交量", "成交量堆量",
     "堆量", "量能堆量"],
    ["C5", "条件5", "条件⑤", "底部筹码不动", "底部筹码",
     "筹码不动"],
]


# =========================================================
# 基础工具
# =========================================================

def normalize(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return v
    return str(v).strip()


def num(v, default=0.0):
    try:
        if v is None:
            return default

        s = str(v).replace(",", "").replace("%", "").strip()

        if s in ("", "-", "--", "None", "null", "nan"):
            return default

        return float(s)
    except Exception:
        return default


def val(row, *keys, default=""):
    if not isinstance(row, dict):
        return default

    for key in keys:
        if key in row and str(row[key]).strip():
            return row[key]

    wanted = {
        str(k).lower().replace(" ", "")
        for k in keys
    }

    for k, v in row.items():
        kk = str(k).lower().replace(" ", "")

        if kk in wanted and str(v).strip():
            return v

    return default


def as_list(v):
    if isinstance(v, list):
        return v

    if isinstance(v, dict):
        for k in [
            "dataList",
            "rows",
            "items",
            "records",
            "list",
            "data",
            "result",
            "results",
        ]:
            x = v.get(k)

            if isinstance(x, list):
                return x

            if isinstance(x, dict):
                y = as_list(x)

                if y:
                    return y

    return []


# =========================================================
# Markdown 表格
# =========================================================

def parse_markdown_table(text):
    if not isinstance(text, str):
        return []

    lines = [
        x.strip()
        for x in text.splitlines()
        if "|" in x
    ]

    if len(lines) < 2:
        return []

    header_index = None

    for i, line in enumerate(lines):

        cells = [
            x.strip()
            for x in line.strip("|").split("|")
        ]

        joined = " ".join(cells)

        if (
            "股票代码" in joined
            or "股票名称" in joined
            or "C1" in cells
            or "C2" in cells
            or "C3" in cells
            or "C4" in cells
            or "C5" in cells
        ):
            header_index = i
            break

    if header_index is None:
        return []

    headers = [
        x.strip()
        for x in lines[header_index]
        .strip("|")
        .split("|")
    ]

    result = []

    for line in lines[header_index + 1:]:

        cells = [
            x.strip()
            for x in line.strip("|").split("|")
        ]

        if len(cells) != len(headers):
            continue

        if all(
            re.fullmatch(r"[-: ]+", x or "")
            for x in cells
        ):
            continue

        result.append(
            dict(zip(headers, cells))
        )

    return result


# =========================================================
# 解析接口返回
# =========================================================

def unwrap(data):

    rows = as_list(data)

    if rows:
        return rows

    def walk(x, depth=0):

        if depth > 10:
            return []

        if isinstance(x, list):

            if x and all(
                isinstance(i, dict)
                for i in x
            ):
                return x

            for item in x:
                r = walk(item, depth + 1)

                if r:
                    return r

        if isinstance(x, dict):

            for k in [
                "dataList",
                "rows",
                "items",
                "records",
                "list",
                "result",
                "results",
                "data",
            ]:

                if k in x:

                    r = as_list(x[k])

                    if r:
                        return r

            for v in x.values():

                r = walk(v, depth + 1)

                if r:
                    return r

        if isinstance(x, str):
            return parse_markdown_table(x)

        return []

    return walk(data)


# =========================================================
# 是 / 否判断
# =========================================================

def bool_condition(v):

    if isinstance(v, bool):
        return v

    if isinstance(v, (int, float)):

        if v == 1:
            return True

        if v == 0:
            return False

    s = normalize(v)

    s = re.sub(
        r"\s+",
        "",
        s
    ).lower()

    if not s:
        return None

    yes = {
        "是",
        "满足",
        "符合",
        "有",
        "true",
        "1",
        "yes",
        "y",
        "√",
        "✓",
        "✔",
        "满足条件",
        "符合条件",
    }

    no = {
        "否",
        "不满足",
        "不符合",
        "无",
        "false",
        "0",
        "no",
        "n",
        "×",
        "✕",
        "✖",
        "不满足条件",
        "不符合条件",
    }

    if s in yes:
        return True

    if s in no:
        return False

    return None


# =========================================================
# 缺少条件
# =========================================================

def get_missing_text(row):

    result = []

    for key in [
        "缺少条件",
        "缺失条件",
        "未满足条件",
        "不满足条件",
        "差的条件",
        "missing",
        "missing_conditions",
    ]:

        if key in row:

            text = str(row[key]).strip()

            if text:
                result.append(text)

    return " ".join(result)


def missing_indices(text):

    result = set()

    if not text:
        return result

    text = str(text)

    for i, aliases in enumerate(ALIASES):

        for alias in aliases:

            if alias in text:

                result.add(i)

                break

    for i in range(5):

        n = i + 1

        if re.search(
            rf"\bC{n}\b",
            text,
            re.I
        ):
            result.add(i)

        if f"条件{n}" in text:
            result.add(i)

    return result


# =========================================================
# 获取接口返回的 3/5、4/5、5/5
# =========================================================

def hard_count_from_row(row):

    for key in [
        "硬条件",
        "硬条件数量",
        "条件数量",
        "满足条件数",
        "hard",
        "hard_count",
    ]:

        if key not in row:
            continue

        text = str(row[key])

        m = re.search(
            r"([0-5])\s*/\s*5",
            text
        )

        if m:
            return int(m.group(1))

        m = re.fullmatch(
            r"\s*([0-5])\s*",
            text
        )

        if m:
            return int(m.group(1))

    text = json.dumps(
        row,
        ensure_ascii=False
    )

    m = re.search(
        r"([0-5])\s*/\s*5",
        text
    )

    if m:
        return int(m.group(1))

    return None


# =========================================================
# 判断5个硬条件
# =========================================================

def get_conditions(row):

    conditions = [None] * 5

    # 第一层：直接读取 C1-C5
    for i, aliases in enumerate(ALIASES):

        for key in aliases:

            if key in row:

                value = bool_condition(
                    row[key]
                )

                if value is not None:

                    conditions[i] = value

                    break

        if conditions[i] is not None:
            continue

        wanted = {
            str(x).lower().replace(" ", "")
            for x in aliases
        }

        for rk, rv in row.items():

            kk = str(rk).lower().replace(" ", "")

            if kk in wanted:

                value = bool_condition(rv)

                if value is not None:

                    conditions[i] = value

                    break

    # 第二层：从文字中识别
    text = json.dumps(
        row,
        ensure_ascii=False
    )

    for i, aliases in enumerate(ALIASES):

        if conditions[i] is not None:
            continue

        for alias in aliases:

            pattern = (
                re.escape(alias)
                + r"\s*[:：=]\s*"
                + r"(是|否|满足|不满足|符合|不符合)"
            )

            m = re.search(
                pattern,
                text,
                re.I
            )

            if m:

                conditions[i] = bool_condition(
                    m.group(1)
                )

                break

    # 第三层：利用“缺少条件”
    missing_text = get_missing_text(row)

    for i in missing_indices(
        missing_text
    ):
        conditions[i] = False

    # 第四层：利用接口直接返回的 3/5、4/5、5/5
    count = hard_count_from_row(row)

    if count is not None:

        true_count = sum(
            x is True
            for x in conditions
        )

        unknown = [
            i
            for i, x in enumerate(conditions)
            if x is None
        ]

        need_true = count - true_count

        # 逻辑可以唯一确定时才补
        if need_true == 0:

            for i in unknown:
                conditions[i] = False

        elif need_true == len(unknown):

            for i in unknown:
                conditions[i] = True

    return conditions


# =========================================================
# 综合评分
# =========================================================

def score_stock(
    hard_count,
    pct,
    turnover,
    lhb_net
):

    score = hard_count * 15

    if pct >= 9:
        score += 10

    elif pct >= 5:
        score += 7

    elif pct >= 3:
        score += 4

    if turnover >= 20:
        score += 10

    elif turnover >= 10:
        score += 7

    elif turnover >= 5:
        score += 4

    if lhb_net > 5000:
        score += 10

    elif lhb_net > 1000:
        score += 7

    elif lhb_net > 0:
        score += 4

    return min(
        round(score, 1),
        100
    )


# =========================================================
# 买点 / 持有 / 卖点
# =========================================================

def rule_buy(row):

    text = json.dumps(
        row,
        ensure_ascii=False
    )

    names = [
        "援军战法",
        "反转阴",
        "仙人指路",
        "双剑合璧",
        "倚天剑",
        "屠龙刀",
    ]

    found = [
        x for x in names
        if x in text
    ]

    if found:
        return "；".join(found)

    return "等待回踩确认"


def rule_hold(row):

    return (
        "高点高、低点高、收盘高；"
        "保持趋势向上"
    )


def rule_sell(row):

    return (
        "高点不再创新高，"
        "收盘未站上前一日，"
        "低点跌破前一日低点时重点观察"
    )


def rule_signals(row):

    text = json.dumps(
        row,
        ensure_ascii=False
    )

    names = [
        "援军战法",
        "破位阴",
        "加速阴",
        "反转阴",
        "九阴九阳",
        "仙人指路",
        "双剑合璧",
        "倚天剑",
        "屠龙刀",
    ]

    found = [
        x for x in names
        if x in text
    ]

    return found or ["趋势确认"]


# =========================================================
# 单只股票
# =========================================================

def build_stock(row):

    conditions = get_conditions(row)

    # 完全拿不到条件，才放弃
    if all(
        x is None
        for x in conditions
    ):
        return None

    # 未知条件绝不算“满足”
    conditions = [
        False if x is None else x
        for x in conditions
    ]

    hard_count = sum(
        1
        for x in conditions
        if x
    )

    # 0-2 过滤
    if hard_count < 3:
        return None

    code = normalize(
        val(
            row,
            "股票代码",
            "代码",
            "证券代码",
            "code",
            "CODE"
        )
    )

    name = normalize(
        val(
            row,
            "股票名称",
            "名称",
            "证券名称",
            "name",
            "NAME"
        )
    )

    price = num(
        val(
            row,
            "最新价",
            "现价",
            "收盘价",
            "price"
        )
    )

    pct = num(
        val(
            row,
            "涨跌幅",
            "涨幅",
            "pct",
            "change_pct",
            "涨跌"
        )
    )

    turnover = num(
        val(
            row,
            "换手率",
            "turnover",
            "换手"
        )
    )

    market_cap = num(
        val(
            row,
            "总市值",
            "市值",
            "market_cap",
            "总市值(亿)"
        )
    )

    lhb_net = num(
        val(
            row,
            "龙虎榜净买额",
            "龙虎榜净额",
            "龙虎榜净买",
            "lhb_net"
        )
    )

    zt_count = num(
        val(
            row,
            "近30日涨停次数",
            "30日涨停次数",
            "涨停次数",
            "zt_count"
        )
    )

    if hard_count == 5:

        category = "5/5"
        category_name = "🔴 强势"

    elif hard_count == 4:

        category = "4/5"
        category_name = "🟠 高度接近"

    else:

        category = "3/5"
        category_name = "🟡 观察"

    missing = [
        HARD_RULES[i]
        for i, ok in enumerate(conditions)
        if not ok
    ]

    buy = normalize(
        val(
            row,
            "买点",
            "买入点",
            "买点提示"
        )
    )

    hold = normalize(
        val(
            row,
            "持有",
            "持股",
            "持有提示"
        )
    )

    sell = normalize(
        val(
            row,
            "卖点",
            "卖出",
            "卖点提示"
        )
    )

    return {
        "code": code,
        "name": name,
        "price": price,
        "pct": pct,
        "turnover": turnover,
        "market_cap": market_cap,
        "lhb_net": lhb_net,
        "zt_count": zt_count,

        "conditions": conditions,

        "hard": hard_count,
        "hard_text": f"{hard_count}/5",

        "category": category,
        "category_name": category_name,

        "missing": missing,

        "missing_text": (
            "、".join(missing)
            if missing
            else "无"
        ),

        "score": score_stock(
            hard_count,
            pct,
            turnover,
            lhb_net
        ),

        "buy": buy or rule_buy(row),
        "hold": hold or rule_hold(row),
        "sell": sell or rule_sell(row),

        "signals": rule_signals(row),
    }


# =========================================================
# 妙想查询
# =========================================================

def build_query(date):

    return f"""
你是A股短线选股数据分析器。

交易日期：{date}

请返回满足3/5、4/5、5/5的股票。
不要只返回5/5。

五个硬条件固定为：

C1：连续5日上涨
C2：30日内有过涨停
C3：收盘不破5日线
C4：堆量成交量
C5：底部筹码不动

严格要求：

1、每只股票必须明确返回C1、C2、C3、C4、C5。
2、C1-C5只能填写“是”或“否”。
3、返回“满足条件数”，格式为3/5、4/5或5/5。
4、返回“缺少条件”。
5、4/5只能缺1项。
6、3/5只能缺2项。
7、不能把4/5或3/5标成5/5。
8、0-2/5不要返回。

同时返回：

股票代码
股票名称
最新价
涨跌幅
换手率
总市值
龙虎榜净买额
近30日涨停次数
C1
C2
C3
C4
C5
满足条件数
缺少条件
买点
持有
卖点
触发规则

只返回股票数据。
"""


def mx_search(query):

    if not MX_APIKEY:

        raise RuntimeError(
            "未读取到 MX_APIKEY，请检查 Render 环境变量。"
        )

    headers = {
        "Content-Type": "application/json",
        "apikey": MX_APIKEY,
        "Authorization": f"Bearer {MX_APIKEY}",
    }

    payload = {
        "query": query
    }

    last_error = None

    for attempt in range(3):

        try:

            response = requests.post(
                MX_API_URL,
                headers=headers,
                json=payload,
                timeout=TIMEOUT
            )

            response.raise_for_status()

            return response.json()

        except Exception as e:

            last_error = e

            if attempt < 2:
                time.sleep(1.5)

    raise RuntimeError(
        f"数据接口请求失败：{last_error}"
    )


# =========================================================
# 扫描
# =========================================================

def scan(date=None):

    if not date:

        date = time.strftime(
            "%Y-%m-%d"
        )

    raw = mx_search(
        build_query(date)
    )

    rows = unwrap(raw)

    if not rows:

        return {
            "ok": True,
            "date": date,
            "top3": [],
            "top3_type": "",
            "top3_note": "",
            "strong": [],
            "near": [],
            "watch": [],
            "ranking": [],
            "counts": {
                "5/5": 0,
                "4/5": 0,
                "3/5": 0
            },
            "message": "接口没有返回有效股票数据。",
        }

    stocks = []

    for row in rows:

        if not isinstance(row, dict):
            continue

        try:

            stock = build_stock(row)

            if stock:
                stocks.append(stock)

        except Exception:
            continue

    # 同一股票去重
    unique = {}

    for stock in stocks:

        key = (
            stock["code"]
            or stock["name"]
        )

        if not key:
            continue

        if (
            key not in unique
            or stock["score"]
            > unique[key]["score"]
        ):
            unique[key] = stock

    stocks = list(
        unique.values()
    )

    # 排序：硬条件优先，再综合评分
    def sort_key(x):

        return (
            -x["hard"],
            -x["score"],
            -x["pct"],
            -x["turnover"]
        )

    strong = sorted(
        [
            x for x in stocks
            if x["hard"] == 5
        ],
        key=sort_key
    )

    near = sorted(
        [
            x for x in stocks
            if x["hard"] == 4
        ],
        key=sort_key
    )

    watch = sorted(
        [
            x for x in stocks
            if x["hard"] == 3
        ],
        key=sort_key
    )

    # 各池独立排名
    for i, stock in enumerate(
        strong,
        1
    ):

        stock["rank"] = i
        stock["pool"] = "5/5"

    for i, stock in enumerate(
        near,
        1
    ):

        stock["rank"] = i
        stock["pool"] = "4/5"

    for i, stock in enumerate(
        watch,
        1
    ):

        stock["rank"] = i
        stock["pool"] = "3/5"

    # =====================================================
    # TOP3
    # =====================================================

    if strong:

        top3 = strong[:3]

        top3_type = "5/5"

        top3_note = ""

    elif near:

        top3 = near[:3]

        top3_type = "4/5"

        top3_note = (
            "今日无5/5，以下为4/5替补"
        )

    else:

        top3 = []

        top3_type = ""

        top3_note = (
            "今日没有5/5和4/5，"
            "3/5仅作为观察池。"
        )

    return {
        "ok": True,
        "date": date,

        "top3": top3,
        "top3_type": top3_type,
        "top3_note": top3_note,

        "strong": strong,
        "near": near,
        "watch": watch,

        "ranking":
            strong + near + watch,

        "counts": {
            "5/5": len(strong),
            "4/5": len(near),
            "3/5": len(watch)
        },

        "message": "",
    }


# =========================================================
# API
# =========================================================

@app.get("/api/scanner")
def scanner(
    date: Optional[str] = None
):

    try:

        return scan(date)

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


# =========================================================
# 手机网页
# =========================================================

HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">

<head>

<meta charset="UTF-8">

<meta name="viewport"
content="width=device-width,
initial-scale=1,
maximum-scale=1,
user-scalable=no">

<title>🔥 妖股雷达</title>

<style>

*{
box-sizing:border-box;
}

body{
margin:0;
background:#08090b;
color:#eee;
font-family:
-apple-system,
BlinkMacSystemFont,
"PingFang SC",
"Microsoft YaHei",
sans-serif;
}

.container{
width:min(1200px,94%);
margin:auto;
padding:18px 0 50px;
}

.header{
padding:18px;
background:#111318;
border:1px solid #252932;
border-radius:16px;
margin-bottom:14px;
}

.title{
font-size:28px;
font-weight:800;
}

.sub{
color:#8f96a3;
margin-top:6px;
font-size:13px;
}

.toolbar{
display:flex;
gap:10px;
margin-top:16px;
flex-wrap:wrap;
}

input,
button{
border:0;
border-radius:10px;
padding:11px 14px;
font-size:15px;
}

input{
background:#080a0d;
border:1px solid #30343d;
color:#fff;
}

button{
background:#e53935;
color:#fff;
font-weight:700;
}

.rules{
display:grid;
grid-template-columns:
repeat(5,1fr);
gap:8px;
margin-top:15px;
}

.rule{
background:#0b0d11;
border:1px solid #252932;
border-radius:10px;
padding:10px;
text-align:center;
font-size:13px;
}

.section{
margin-top:18px;
}

.section-title{
font-size:20px;
font-weight:800;
margin:12px 0;
}

.notice{
background:#17130b;
border:1px solid #4b3511;
color:#ffbd4a;
padding:12px;
border-radius:10px;
margin-bottom:12px;
}

.cards{
display:grid;
grid-template-columns:
repeat(3,1fr);
gap:12px;
}

.card{
background:#111318;
border:1px solid #292d36;
border-radius:15px;
padding:15px;
}

.card.red{
border-color:#6b2020;
}

.card.orange{
border-color:#704817;
}

.card.yellow{
border-color:#665616;
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

.code{
color:#858c99;
font-size:12px;
margin-top:3px;
}

.score{
font-size:22px;
font-weight:900;
}

.red-text{
color:#ff4d4f;
}

.orange-text{
color:#ff9f43;
}

.yellow-text{
color:#ffd84d;
}

.info{
display:grid;
grid-template-columns:
1fr 1fr;
gap:7px;
margin-top:13px;
}

.info div{
background:#0b0d11;
padding:8px;
border-radius:8px;
font-size:12px;
}

.hard{
margin-top:12px;
font-size:16px;
font-weight:800;
}

.missing{
margin-top:8px;
padding:9px;
border-radius:8px;
background:#20110f;
color:#ff887e;
font-size:13px;
}

.good{
margin-top:8px;
padding:9px;
border-radius:8px;
background:#0c1b12;
color:#58dc88;
font-size:13px;
}

.rules-box{
margin-top:10px;
color:#b7bdc9;
font-size:12px;
line-height:1.7;
}

.trade{
margin-top:12px;
border-top:1px solid #292d36;
padding-top:10px;
font-size:12px;
line-height:1.7;
}

.table-wrap{
overflow:auto;
background:#111318;
border:1px solid #292d36;
border-radius:12px;
}

table{
width:100%;
border-collapse:collapse;
min-width:850px;
}

th,
td{
padding:10px;
border-bottom:1px solid #242832;
text-align:left;
font-size:12px;
}

th{
color:#8f96a3;
background:#0d0f13;
}

.pool5{
color:#ff4d4f;
font-weight:800;
}

.pool4{
color:#ff9f43;
font-weight:800;
}

.pool3{
color:#ffd84d;
font-weight:800;
}

.empty{
padding:25px;
text-align:center;
color:#777f8c;
background:#111318;
border:1px solid #292d36;
border-radius:12px;
}

.loading{
padding:20px;
text-align:center;
color:#aaa;
}

@media(max-width:800px){

.rules{
grid-template-columns:
1fr 1fr;
}

.cards{
grid-template-columns:1fr;
}

.title{
font-size:24px;
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
5条硬条件 + 买点/持有/卖点规则 · 东方财富公开行情数据
</div>

<div class="toolbar">

<input
id="date"
type="date">

<button
onclick="scan()">
开始扫描
</button>

</div>

<div class="rules">

<div class="rule">
① 连续5日上涨
</div>

<div class="rule">
② 30日内有过涨停
</div>

<div class="rule">
③ 收盘不破5日线
</div>

<div class="rule">
④ 堆量成交量
</div>

<div class="rule">
⑤ 底部筹码不动
</div>

</div>

</div>


<div
id="loading"
class="loading"
style="display:none">

正在扫描……

</div>


<div id="error"></div>


<!-- TOP3 -->

<div class="section">

<div
id="topTitle"
class="section-title">

🔴 强势 TOP3（5/5）

</div>

<div id="topNote"></div>

<div
id="top3"
class="cards">
</div>

</div>


<!-- 4/5 -->

<div class="section">

<div class="section-title">

🟠 高度接近（4/5）

</div>

<div
id="near"
class="cards">
</div>

</div>


<!-- 3/5 -->

<div class="section">

<div class="section-title">

🟡 观察池（3/5）

</div>

<div
id="watch"
class="cards">
</div>

</div>


<!-- 排名 -->

<div class="section">

<div class="section-title">

📊 分池排名

</div>

<div class="table-wrap">

<table>

<thead>

<tr>

<th>池子</th>

<th>排名</th>

<th>股票</th>

<th>硬条件</th>

<th>综合评分</th>

<th>涨跌幅</th>

<th>换手率</th>

<th>缺少条件</th>

</tr>

</thead>

<tbody id="ranking">

</tbody>

</table>

</div>

</div>

</div>


<script>

const d = new Date();

document.getElementById(
"date"
).value =
d.getFullYear()
+
"-"
+
String(
d.getMonth()+1
).padStart(2,"0")
+
"-"
+
String(
d.getDate()
).padStart(2,"0");


function esc(v){

if(
v === null ||
v === undefined
){
return "";
}

return String(v)

.replaceAll(
"&",
"&amp;"
)

.replaceAll(
"<",
"&lt;"
)

.replaceAll(
">",
"&gt;"
)

.replaceAll(
'"',
"&quot;"
)

.replaceAll(
"'",
"&#039;"
);

}


function card(s){

let color =
"yellow";

let cls =
"yellow-text";

if(s.hard === 5){

color =
"red";

cls =
"red-text";

}

if(s.hard === 4){

color =
"orange";

cls =
"orange-text";

}


let missing = "";

if(s.hard < 5){

missing =
`
<div class="missing">

缺少条件：
${esc(
s.missing_text
)}

</div>
`;

}else{

missing =
`
<div class="good">

5个硬条件全部满足

</div>
`;

}


let signals =
(s.signals || [])
.join("、");


return `

<div class="card ${color}">

<div class="stock-head">

<div>

<div class="stock-name">

${esc(
s.name || "--"
)}

</div>

<div class="code">

${esc(
s.code || "--"
)}

</div>

</div>

<div class="${cls}">

${esc(s.score)}

</div>

</div>


<div class="hard">

${esc(
s.category_name
)}

· 硬条件

${esc(
s.hard_text
)}

</div>


${missing}


<div class="info">

<div>
现价：
${esc(s.price)}
</div>

<div>
涨跌：
${esc(s.pct)}%
</div>

<div>
换手：
${esc(s.turnover)}%
</div>

<div>
市值：
${esc(s.market_cap)}
</div>

<div>
龙虎榜净额：
${esc(s.lhb_net)}
</div>

<div>
30日涨停：
${esc(s.zt_count)}
</div>

</div>


<div class="rules-box">

触发规则：
${esc(signals)}

</div>


<div class="trade">

<b>买点：</b>
${esc(s.buy)}

<br>

<b>持有：</b>
${esc(s.hold)}

<br>

<b>卖点：</b>
${esc(s.sell)}

</div>

</div>

`;

}


function renderCards(
id,
rows
){

const box =
document.getElementById(
id
);

if(
!rows ||
!rows.length
){

box.innerHTML =
`
<div class="empty">

暂无符合条件的股票

</div>
`;

return;

}

box.innerHTML =
rows
.map(card)
.join("");

}


async function scan(){

const date =
document.getElementById(
"date"
).value;

document.getElementById(
"loading"
).style.display =
"block";

document.getElementById(
"error"
).innerHTML = "";

document.getElementById(
"topNote"
).innerHTML = "";


try{

const response =
await fetch(
"/api/scanner?date="
+
encodeURIComponent(date)
);

const data =
await response.json();


if(!data.ok){

throw new Error(
data.error ||
"扫描失败"
);

}


const title =
document.getElementById(
"topTitle"
);


if(
data.top3_type === "4/5"
){

title.innerText =
"🟠 TOP3（4/5替补）";

document.getElementById(
"topNote"
).innerHTML =

`
<div class="notice">

今日无5/5，
以下为4/5替补

</div>
`;

}else{

title.innerText =
"🔴 强势 TOP3（5/5）";

}


renderCards(
"top3",
data.top3 || []
);


renderCards(
"near",
data.near || []
);


renderCards(
"watch",
data.watch || []
);


const rows =
data.ranking || [];

const tbody =
document.getElementById(
"ranking"
);


if(!rows.length){

tbody.innerHTML =
`
<tr>

<td colspan="8">

暂无3/5以上股票

</td>

</tr>
`;

}else{

tbody.innerHTML =
rows.map(
s => {

let cls =
"pool3";

if(s.hard === 5){

cls =
"pool5";

}else if(
s.hard === 4
){

cls =
"pool4";

}


return `

<tr>

<td class="${cls}">

${esc(s.pool)}

</td>

<td>

${esc(s.rank)}

</td>

<td>

${esc(s.name)}

<br>

<span
style="color:#777">

${esc(s.code)}

</span>

</td>

<td>

${esc(
s.hard_text
)}

</td>

<td>

${esc(
s.score
)}

</td>

<td>

${esc(
s.pct
)}%

</td>

<td>

${esc(
s.turnover
)}%

</td>

<td>

${esc(
s.missing_text
)}

</td>

</tr>

`;

}
).join("");

}


}catch(e){

document.getElementById(
"error"
).innerHTML =

`
<div class="notice">

数据接口错误：

${esc(e.message)}

</div>
`;

}finally{

document.getElementById(
"loading"
).style.display =
"none";

}

}

</script>

</body>

</html>
"""


# =========================================================
# 首页
# =========================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def home():

    return HTML
