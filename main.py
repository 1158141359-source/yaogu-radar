import os
import re
import json
import time
from typing import Optional

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse


app = FastAPI(title="妖股雷达")


# =========================================================
# 配置
# =========================================================

MX_API_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"
MX_APIKEY = os.getenv("MX_APIKEY", "").strip()

TIMEOUT = 35


# =========================================================
# 5个硬条件 —— 固定，不改变
# =========================================================

HARD_RULES = [
    "① 连续5日上涨",
    "② 30日内有过涨停",
    "③ 收盘不破5日线",
    "④ 堆量成交量",
    "⑤ 底部筹码不动",
]


# =========================================================
# 工具函数
# =========================================================

def normalize(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    return str(v).strip()


def num(v, default=0.0):
    try:
        if v is None:
            return default

        s = str(v).replace(",", "").replace("%", "").strip()

        if s in ("", "-", "--", "None", "null"):
            return default

        return float(s)
    except Exception:
        return default


def val(row, *keys, default=""):
    if not isinstance(row, dict):
        return default

    for key in keys:
        if key in row:
            v = row[key]

            if v is not None and str(v).strip() != "":
                return v

    # 模糊匹配
    lower_map = {
        str(k).lower().replace(" ", ""): v
        for k, v in row.items()
    }

    for key in keys:
        k = str(key).lower().replace(" ", "")
        if k in lower_map:
            v = lower_map[k]
            if v is not None and str(v).strip() != "":
                return v

    return default


def as_list(v):
    if isinstance(v, list):
        return v

    if isinstance(v, dict):
        for k in [
            "dataList",
            "data",
            "rows",
            "result",
            "items",
            "list",
            "records",
        ]:
            if k in v:
                x = v[k]
                if isinstance(x, list):
                    return x
                if isinstance(x, dict):
                    y = as_list(x)
                    if y:
                        return y

    return []


def as_dict(v):
    if isinstance(v, dict):
        return v

    if isinstance(v, str):
        try:
            x = json.loads(v)
            if isinstance(x, dict):
                return x
        except Exception:
            pass

    return {}


def unwrap(data):
    """
    尽可能从妙想接口返回结构中找到真正的数据。
    """

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    # 常见结构
    candidates = [
        data.get("data"),
        data.get("result"),
        data.get("results"),
        data.get("rows"),
        data.get("dataList"),
        data.get("allResults"),
    ]

    for x in candidates:
        rows = as_list(x)
        if rows:
            return rows

        if isinstance(x, dict):
            for y in [
                x.get("data"),
                x.get("result"),
                x.get("dataList"),
                x.get("rows"),
                x.get("allResults"),
            ]:
                rows = as_list(y)
                if rows:
                    return rows

    # 深层搜索
    def walk(x, depth=0):
        if depth > 8:
            return []

        if isinstance(x, list):
            if x and all(isinstance(i, dict) for i in x):
                return x

            for i in x:
                r = walk(i, depth + 1)
                if r:
                    return r

        if isinstance(x, dict):
            for k, v in x.items():
                if k in (
                    "dataList",
                    "rows",
                    "items",
                    "records",
                ):
                    r = as_list(v)
                    if r:
                        return r

                r = walk(v, depth + 1)
                if r:
                    return r

        return []

    return walk(data)


# =========================================================
# Markdown 表格解析
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

        if any(
            x in cells
            for x in [
                "股票代码",
                "代码",
                "股票名称",
                "名称",
                "C1",
                "C2",
                "C3",
                "C4",
                "C5",
            ]
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

    rows = []

    for line in lines[header_index + 1:]:
        cells = [
            x.strip()
            for x in line.strip("|").split("|")
        ]

        if not cells:
            continue

        if all(
            re.fullmatch(r"[-: ]+", x or "")
            for x in cells
        ):
            continue

        if len(cells) != len(headers):
            continue

        row = {}

        for i, h in enumerate(headers):
            row[h] = cells[i]

        rows.append(row)

    return rows


# =========================================================
# 妙想 API
# =========================================================

def mx_search(query):
    if not MX_APIKEY:
        raise RuntimeError(
            "未读取到 MX_APIKEY，请在 Render 环境变量中检查。"
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
            r = requests.post(
                MX_API_URL,
                headers=headers,
                json=payload,
                timeout=TIMEOUT,
            )

            r.raise_for_status()

            data = r.json()

            return data

        except Exception as e:
            last_error = e

            if attempt < 2:
                time.sleep(1.5)

    raise RuntimeError(
        f"数据接口请求失败：{last_error}"
    )


# =========================================================
# 判断条件 TRUE / FALSE
# =========================================================

def bool_condition(v):
    """
    严格判断“是/否”，避免把普通文字误判。
    """

    if isinstance(v, bool):
        return v

    if isinstance(v, (int, float)):
        if v == 1:
            return True
        if v == 0:
            return False

    s = normalize(v).lower()

    if not s:
        return None

    # 去掉空格
    s = re.sub(r"\s+", "", s)

    yes_values = {
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

    no_values = {
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

    if s in yes_values:
        return True

    if s in no_values:
        return False

    # 允许“是（满足）”“否（不满足）”
    if re.search(r"^(是|满足|符合|有)[（(]", s):
        return True

    if re.search(r"^(否|不满足|不符合|无)[（(]", s):
        return False

    return None


# =========================================================
# 获取5个硬条件
# =========================================================

def get_conditions(row):
    """
    必须逐项拿到5个条件。
    无法确认的条件返回 None。
    不允许猜。
    """

    aliases = [
        [
            "C1",
            "条件1",
            "条件①",
            "连续5日上涨",
            "连续5日涨",
            "连续5天上涨",
            "连续上涨5日",
            "5日上涨",
        ],
        [
            "C2",
            "条件2",
            "条件②",
            "30日内有过涨停",
            "30日内有涨停",
            "30日涨停",
            "近30日涨停",
            "30天涨停",
        ],
        [
            "C3",
            "条件3",
            "条件③",
            "收盘不破5日线",
            "收盘价不破5日线",
            "不破5日线",
            "5日线",
        ],
        [
            "C4",
            "条件4",
            "条件④",
            "堆量成交量",
            "成交量堆量",
            "堆量",
            "量能堆量",
        ],
        [
            "C5",
            "条件5",
            "条件⑤",
            "底部筹码不动",
            "底部筹码",
            "筹码不动",
        ],
    ]

    result = []

    for keys in aliases:
        found = None

        for key in keys:
            if key in row:
                found = bool_condition(row[key])

                if found is not None:
                    break

        # 模糊寻找字段
        if found is None:
            for rk, rv in row.items():
                rk2 = str(rk).replace(" ", "")

                for key in keys:
                    key2 = str(key).replace(" ", "")

                    if rk2 == key2:
                        found = bool_condition(rv)
                        break

                if found is not None:
                    break

        result.append(found)

    return result


# =========================================================
# 股票基础字段
# =========================================================

def build_stock(row):
    conditions = get_conditions(row)

    # 任何一个条件无法确认，不参与 3/5 以上分类
    if any(x is None for x in conditions):
        return None

    hard_count = sum(
        1 for x in conditions
        if x is True
    )

    # 0-2 直接过滤
    if hard_count < 3:
        return None

    code = normalize(
        val(
            row,
            "股票代码",
            "代码",
            "证券代码",
            "code",
            "CODE",
        )
    )

    name = normalize(
        val(
            row,
            "股票名称",
            "名称",
            "证券名称",
            "name",
            "NAME",
        )
    )

    pct = num(
        val(
            row,
            "涨跌幅",
            "涨幅",
            "pct",
            "change_pct",
            "涨跌",
        )
    )

    price = num(
        val(
            row,
            "最新价",
            "现价",
            "收盘价",
            "price",
        )
    )

    turnover = num(
        val(
            row,
            "换手率",
            "turnover",
            "换手",
        )
    )

    market_cap = num(
        val(
            row,
            "总市值",
            "市值",
            "market_cap",
            "总市值(亿)",
        )
    )

    lhb_net = num(
        val(
            row,
            "龙虎榜净买额",
            "龙虎榜净额",
            "龙虎榜净买",
            "lhb_net",
        )
    )

    zt_count = num(
        val(
            row,
            "近30日涨停次数",
            "30日涨停次数",
            "涨停次数",
            "zt_count",
        )
    )

    # 综合评分不是硬条件数量
    score = score_stock(
        row,
        hard_count,
        pct,
        turnover,
        lhb_net,
    )

    missing = [
        HARD_RULES[i]
        for i, ok in enumerate(conditions)
        if not ok
    ]

    if hard_count == 5:
        category = "5/5"
        category_name = "🔴 强势"
    elif hard_count == 4:
        category = "4/5"
        category_name = "🟠 高度接近"
    else:
        category = "3/5"
        category_name = "🟡 观察"

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
        "missing_text": "、".join(missing)
        if missing
        else "无",

        "score": score,

        "buy": rule_buy(row),
        "hold": rule_hold(row),
        "sell": rule_sell(row),
        "signals": rule_signals(row),
    }


# =========================================================
# 综合评分
# =========================================================

def score_stock(
    row,
    hard_count,
    pct,
    turnover,
    lhb_net,
):
    """
    0-100 综合评分。

    注意：
    综合评分 ≠ 5个硬条件数量。
    """

    score = hard_count * 15

    # 涨幅
    if pct >= 9:
        score += 10
    elif pct >= 5:
        score += 7
    elif pct >= 3:
        score += 4

    # 换手
    if turnover >= 20:
        score += 10
    elif turnover >= 10:
        score += 7
    elif turnover >= 5:
        score += 4

    # 龙虎榜资金
    if lhb_net > 5000:
        score += 10
    elif lhb_net > 1000:
        score += 7
    elif lhb_net > 0:
        score += 4

    return min(round(score, 1), 100)


# =========================================================
# 买点 / 持有 / 卖点
# =========================================================

def rule_buy(row):
    text = json.dumps(
        row,
        ensure_ascii=False
    )

    signals = []

    if any(
        x in text
        for x in [
            "反转阴",
            "援军",
            "仙人指路",
            "双剑合璧",
            "倚天剑",
            "屠龙刀",
        ]
    ):
        signals.append("出现买点信号")

    if not signals:
        signals.append("等待回踩确认")

    return "；".join(signals)


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

    return found if found else ["趋势确认"]


# =========================================================
# 查询
# =========================================================

def build_query(date):
    return f"""
你是A股短线选股数据分析器。

交易日期：{date}

请不要只返回5/5股票。

必须筛选并返回满足以下条件中至少3项的股票：
3/5、4/5、5/5全部可以返回。

5个硬条件固定为：

C1：连续5日上涨
C2：30日内有过涨停
C3：收盘不破5日线
C4：堆量成交量
C5：底部筹码不动

重要要求：

1. 每只股票必须逐项判断C1、C2、C3、C4、C5。
2. C1-C5每项只能填写“是”或“否”。
3. 不允许用“可能”“基本”“接近”等模糊答案。
4. 不能因为股票满足3项就把其他条件自动判定为满足。
5. 只返回满足3项、4项、5项的股票。
6. 0-2项不要返回。
7. 同时返回股票代码、股票名称、最新价、涨跌幅、换手率、总市值、龙虎榜净买额。
8. 如果可以取得，返回近30日涨停次数。
9. 返回买点提示、持有提示、卖点提示。
10. 返回触发的战法名称。

请严格使用下面字段：

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
买点
持有
卖点
触发规则

只返回股票数据，不要返回长篇解释。
"""


# =========================================================
# 提取接口结果
# =========================================================

def extract_result(data):
    rows = unwrap(data)

    if rows:
        return rows

    # 尝试寻找字符串
    def walk_text(x):
        if isinstance(x, str):
            rows2 = parse_markdown_table(x)

            if rows2:
                return rows2

        if isinstance(x, dict):
            for v in x.values():
                r = walk_text(v)

                if r:
                    return r

        if isinstance(x, list):
            for v in x:
                r = walk_text(v)

                if r:
                    return r

        return []

    return walk_text(data)


# =========================================================
# 扫描
# =========================================================

def scan(date=None):
    if not date:
        date = time.strftime(
            "%Y-%m-%d",
            time.localtime()
        )

    query = build_query(date)

    raw = mx_search(query)

    rows = extract_result(raw)

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
            "message": "今日没有取得满足3/5以上的有效数据。",
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

    # =====================================================
    # 三个池子完全分开排序
    # =====================================================

    strong = [
        x for x in stocks
        if x["hard"] == 5
    ]

    near = [
        x for x in stocks
        if x["hard"] == 4
    ]

    watch = [
        x for x in stocks
        if x["hard"] == 3
    ]

    sort_key = lambda x: (
        -x["score"],
        -x["pct"],
        -x["turnover"],
    )

    strong.sort(key=sort_key)
    near.sort(key=sort_key)
    watch.sort(key=sort_key)

    # =====================================================
    # TOP3
    # =====================================================

    top3 = []
    top3_type = ""
    top3_note = ""

    if strong:
        top3 = strong[:3]
        top3_type = "5/5"
        top3_note = ""

    elif near:
        top3 = near[:3]
        top3_type = "4/5"
        top3_note = "今日无5/5，以下为4/5替补"

    else:
        top3 = []
        top3_type = ""
        top3_note = "今日没有5/5，也没有4/5。3/5仅进入观察池。"

    # =====================================================
    # 给每个池子添加独立排名
    # =====================================================

    for i, x in enumerate(strong, 1):
        x["rank"] = i
        x["pool"] = "5/5"

    for i, x in enumerate(near, 1):
        x["rank"] = i
        x["pool"] = "4/5"

    for i, x in enumerate(watch, 1):
        x["rank"] = i
        x["pool"] = "3/5"

    ranking = (
        [
            {
                **x,
                "pool_rank": i + 1
            }
            for i, x in enumerate(strong)
        ]
        +
        [
            {
                **x,
                "pool_rank": i + 1
            }
            for i, x in enumerate(near)
        ]
        +
        [
            {
                **x,
                "pool_rank": i + 1
            }
            for i, x in enumerate(watch)
        ]
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

        "ranking": ranking,

        "counts": {
            "5/5": len(strong),
            "4/5": len(near),
            "3/5": len(watch),
        },

        "message": "",
    }


# =========================================================
# API
# =========================================================

@app.get("/api/scanner")
def scanner(date: Optional[str] = None):
    try:
        return scan(date)

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


# =========================================================
# 页面
# =========================================================

HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1,
      maximum-scale=1,user-scalable=no">

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

button:active{
    transform:scale(.98);
}

.rules{
    display:grid;
    grid-template-columns:repeat(5,1fr);
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
    margin-top:16px;
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
    grid-template-columns:repeat(3,1fr);
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
    grid-template-columns:1fr 1fr;
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
        grid-template-columns:1fr 1fr;
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

<div class="title">🔥 妖股雷达</div>

<div class="sub">
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

</div>

<div class="rules">

<div class="rule">① 连续5日上涨</div>
<div class="rule">② 30日内有过涨停</div>
<div class="rule">③ 收盘不破5日线</div>
<div class="rule">④ 堆量成交量</div>
<div class="rule">⑤ 底部筹码不动</div>

</div>

</div>


<div id="loading"
     class="loading"
     style="display:none">
正在扫描……
</div>


<div id="error"></div>


<!-- TOP3 -->

<div class="section">

<div class="section-title">
🔴 强势 TOP3（5/5）
</div>

<div id="topNote"></div>

<div id="top3"
     class="cards">
</div>

</div>


<!-- 4/5 -->

<div class="section">

<div class="section-title">
🟠 高度接近（4/5）
</div>

<div id="near"
     class="cards">
</div>

</div>


<!-- 3/5 -->

<div class="section">

<div class="section-title">
🟡 观察池（3/5）
</div>

<div id="watch"
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

const today = new Date();

const yyyy = today.getFullYear();
const mm = String(today.getMonth()+1).padStart(2,"0");
const dd = String(today.getDate()).padStart(2,"0");

document.getElementById("date").value =
    `${yyyy}-${mm}-${dd}`;


function esc(v){

    if(v === null || v === undefined)
        return "";

    return String(v)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;")
        .replaceAll("'","&#039;");
}


function card(stock){

    let color = "yellow";
    let scoreClass = "yellow-text";

    if(stock.hard === 5){
        color = "red";
        scoreClass = "red-text";
    }

    if(stock.hard === 4){
        color = "orange";
        scoreClass = "orange-text";
    }

    let missing = "";

    if(stock.hard < 5){

        missing = `
        <div class="missing">
        缺少条件：${esc(stock.missing_text)}
        </div>
        `;

    }else{

        missing = `
        <div class="good">
        5个硬条件全部满足
        </div>
        `;

    }

    const signals =
        (stock.signals || []).join("、");

    return `

    <div class="card ${color}">

        <div class="stock-head">

            <div>
                <div class="stock-name">
                    ${esc(stock.name || "--")}
                </div>

                <div class="code">
                    ${esc(stock.code || "--")}
                </div>
            </div>

            <div class="${scoreClass}">
                ${esc(stock.score)}
            </div>

        </div>


        <div class="hard">
            ${esc(stock.category_name)}
            · 硬条件 ${esc(stock.hard_text)}
        </div>


        ${missing}


        <div class="info">

            <div>
                现价：
                ${esc(stock.price)}
            </div>

            <div>
                涨跌：
                ${esc(stock.pct)}%
            </div>

            <div>
                换手：
                ${esc(stock.turnover)}%
            </div>

            <div>
                市值：
                ${esc(stock.market_cap)}
            </div>

            <div>
                龙虎榜净额：
                ${esc(stock.lhb_net)}
            </div>

            <div>
                30日涨停：
                ${esc(stock.zt_count)}
            </div>

        </div>


        <div class="rules-box">
            触发规则：${esc(signals)}
        </div>


        <div class="trade">

            <b>买点：</b>
            ${esc(stock.buy)}

            <br>

            <b>持有：</b>
            ${esc(stock.hold)}

            <br>

            <b>卖点：</b>
            ${esc(stock.sell)}

        </div>

    </div>

    `;
}


function renderCards(id, rows){

    const box = document.getElementById(id);

    if(!rows || rows.length === 0){

        box.innerHTML = `
        <div class="empty">
        暂无符合条件的股票
        </div>
        `;

        return;
    }

    box.innerHTML =
        rows.map(card).join("");

}


async function scan(){

    const date =
        document.getElementById("date").value;

    document.getElementById("loading")
        .style.display = "block";

    document.getElementById("error")
        .innerHTML = "";

    document.getElementById("top3")
        .innerHTML = "";

    document.getElementById("near")
        .innerHTML = "";

    document.getElementById("watch")
        .innerHTML = "";

    try{

        const res =
            await fetch(
                `/api/scanner?date=${encodeURIComponent(date)}`
            );

        const data =
            await res.json();

        if(!data.ok){

            throw new Error(
                data.error || "扫描失败"
            );

        }


        /*
         * TOP3
         */

        let title =
            "🔴 强势 TOP3（5/5）";

        if(data.top3_type === "4/5"){

            title =
                "🟠 TOP3（4/5替补）";

            document.getElementById("topNote")
                .innerHTML = `
                <div class="notice">
                今日无5/5，以下为4/5替补
                </div>
                `;

        }else{

            document.getElementById("topNote")
                .innerHTML = "";

        }


        document.querySelector(
            ".section .section-title"
        ).innerText = title;


        renderCards(
            "top3",
            data.top3 || []
        );


        /*
         * 4/5
         */

        renderCards(
            "near",
            data.near || []
        );


        /*
         * 3/5
         */

        renderCards(
            "watch",
            data.watch || []
        );


        /*
         * 排名
         */

        const ranking =
            document.getElementById("ranking");

        const rows =
            data.ranking || [];

        if(rows.length === 0){

            ranking.innerHTML = `
            <tr>
                <td colspan="8">
                暂无3/5以上股票
                </td>
            </tr>
            `;

        }else{

            ranking.innerHTML =
                rows.map(stock => {

                    let cls = "pool3";

                    if(stock.hard === 5)
                        cls = "pool5";

                    else if(stock.hard === 4)
                        cls = "pool4";

                    return `
                    <tr>

                        <td class="${cls}">
                            ${esc(stock.pool)}
                        </td>

                        <td>
                            ${esc(stock.rank)}
                        </td>

                        <td>
                            ${esc(stock.name)}
                            <br>
                            <span style="color:#777">
                            ${esc(stock.code)}
                            </span>
                        </td>

                        <td>
                            ${esc(stock.hard_text)}
                        </td>

                        <td>
                            ${esc(stock.score)}
                        </td>

                        <td>
                            ${esc(stock.pct)}%
                        </td>

                        <td>
                            ${esc(stock.turnover)}%
                        </td>

                        <td>
                            ${esc(stock.missing_text)}
                        </td>

                    </tr>
                    `;

                }).join("");

        }

    }catch(e){

        document.getElementById("error")
            .innerHTML = `
            <div class="notice">
            数据接口错误：${esc(e.message)}
            </div>
            `;

    }finally{

        document.getElementById("loading")
            .style.display = "none";

    }

}

</script>

</body>
</html>
"""


# =========================================================
# 首页
# =========================================================

@app.get("/", response_class=HTMLResponse)
def home():
    return HTML
