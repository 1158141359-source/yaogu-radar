

import os

import math

import time

from datetime import datetime, timedelta

from typing import Optional, Dict, List, Any

from concurrent.futures import ThreadPoolExecutor, as_completed

from zoneinfo import ZoneInfo



import requests

from fastapi import FastAPI, Query

from fastapi.responses import HTMLResponse



app = FastAPI(title="妖股雷达", version="3.0")



EASTMONEY_QT = "https://push2.eastmoney.com/api/qt/stock/get"

EASTMONEY_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"

EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

EASTMONEY_LHB = "https://datacenter-web.eastmoney.com/api/data/v1/get"



HEADERS = {

    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "

                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"

}



# =========================

# 基础工具

# =========================



def get_json(url: str, params: Dict[str, Any], timeout: int = 12) -> Dict[str, Any]:

    r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)

    r.raise_for_status()

    return r.json()





def clean_code(code: str) -> str:

    return str(code).strip().zfill(6)





def market_prefix(code: str) -> str:

    code = clean_code(code)

    if code.startswith(("60", "68")):

        return "1"

    if code.startswith(("00", "30")):

        return "0"

    if code.startswith(("8", "4")):

        return "0"

    return "1"





def secid(code: str) -> str:

    return f"{market_prefix(code)}.{clean_code(code)}"





def normalize_date(d: Optional[str]) -> str:

    if not d:

        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

    try:

        return datetime.strptime(d[:10], "%Y-%m-%d").strftime("%Y-%m-%d")

    except Exception:

        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")





def get_trading_dates(end_date: str, days: int = 70) -> List[str]:

    # 使用上证指数判断交易日：1.000001

    end_date = normalize_date(end_date)

    start = (

        datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=days)

    ).strftime("%Y-%m-%d")



    data = get_json(

        EASTMONEY_KLINE,

        {

            "secid": "1.000001",

            "klt": 101,

            "fqt": 1,

            "beg": start.replace("-", ""),

            "end": end_date.replace("-", ""),

            "fields1": "f1,f2,f3,f4,f5,f6",

            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",

        },

    )

    rows = (data.get("data") or {}).get("klines") or []

    return [x.split(",")[0] for x in rows]





def actual_target_date(date_str: Optional[str]) -> str:

    target = normalize_date(date_str)

    dates = get_trading_dates(target, 90)

    if dates:

        return dates[-1]

    return target





# =========================

# 行情 / K线 / 龙虎榜

# =========================



def get_stock_list(limit: int = 1200) -> List[Dict[str, Any]]:

    """

    当前股票池。

    market_cap 单位：亿元。

    为控制东方财富公开接口压力，先用当前行情筛选，再对候选做历史K线计算。

    """

    all_rows = []

    pages = max(1, math.ceil(limit / 500))



    for page in range(1, pages + 1):

        data = get_json(

            EASTMONEY_CLIST,

            {

                "pn": page,

                "pz": 500,

                "po": 1,

                "np": 1,

                "fltt": 2,

                "invt": 2,

                "fid": "f3",

                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",

                "fields": "f2,f3,f8,f12,f14,f20,f21",

            },

        )

        diff = (data.get("data") or {}).get("diff") or []

        if not diff:

            break



        for x in diff:

            code = clean_code(x.get("f12", ""))

            name = str(x.get("f14", ""))

            cap = float(x.get("f20") or 0) / 1e8

            pct = float(x.get("f3") or 0)

            turnover = float(x.get("f8") or 0)



            if not code or "ST" in name.upper() or "退" in name:

                continue

            if cap <= 0 or cap > 300:

                continue



            all_rows.append({

                "code": code,

                "name": name,

                "market_cap": round(cap, 2),

                "pct": pct,

                "turnover": turnover,

            })



        if len(all_rows) >= limit:

            break

        time.sleep(0.15)



    # 优先活跃股票，降低请求量

    all_rows.sort(key=lambda x: (x["pct"], x["turnover"]), reverse=True)

    return all_rows[:limit]





def get_kline(code: str, beg: str, end: str) -> List[Dict[str, float]]:

    data = get_json(

        EASTMONEY_KLINE,

        {

            "secid": secid(code),

            "klt": 101,

            "fqt": 1,

            "beg": beg.replace("-", ""),

            "end": end.replace("-", ""),

            "fields1": "f1,f2,f3,f4,f5,f6",

            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",

        },

    )

    rows = (data.get("data") or {}).get("klines") or []

    out = []



    for row in rows:

        p = row.split(",")

        if len(p) < 11:

            continue

        try:

            out.append({

                "date": p[0],

                "open": float(p[1]),

                "close": float(p[2]),

                "high": float(p[3]),

                "low": float(p[4]),

                "volume": float(p[5]),

                "amount": float(p[6]),

                "amplitude": float(p[7]),

                "pct": float(p[8]),

                "change": float(p[9]),

                "turnover": float(p[10]),

            })

        except Exception:

            continue



    return out





def get_lhb(target_date: str) -> Dict[str, Dict[str, float]]:

    """

    东方财富历史龙虎榜。

    """

    result = {}

    try:

        data = get_json(

            EASTMONEY_LHB,

            {

                "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",

                "columns": "ALL",

                "filter": f"(TRADE_DATE='{target_date}')",

                "pageNumber": 1,

                "pageSize": 500,

                "sortColumns": "BILLBOARD_NET_AMT",

                "sortTypes": "-1",

            },

        )



        rows = (data.get("result") or {}).get("data") or []

        for x in rows:

            code = clean_code(x.get("SECURITY_CODE", ""))

            if not code:

                continue

            result[code] = {

                "net": float(x.get("BILLBOARD_NET_AMT") or 0) / 1e4,

                "buy": float(x.get("BILLBOARD_BUY_AMT") or 0) / 1e4,

                "sell": float(x.get("BILLBOARD_SELL_AMT") or 0) / 1e4,

            }

    except Exception:

        pass

    return result





# =========================

# 五条硬条件

# =========================



HARD_NAMES = [

    "①连续5日上涨",

    "②30日内有过涨停",

    "③收盘价不破5日线",

    "④成交量堆量",

    "⑤底部筹码不动",

]





def is_limit_up(row: Dict[str, float]) -> bool:

    # 普通A股近似涨停判断。

    # ST/特殊股票已在股票池中排除。

    return row["pct"] >= 9.7





def calc_hard_conditions(k: List[Dict[str, float]]) -> List[bool]:

    if len(k) < 35:

        return [False] * 5



    last = k[-1]



    # ① 连续5日上涨：最近5根收盘价逐日上涨

    five_up = all(k[i]["close"] > k[i - 1]["close"] for i in range(len(k) - 4, len(k)))



    # ② 30日内有过涨停

    last30 = k[-30:]

    zt30 = any(is_limit_up(x) for x in last30)



    # ③ 收盘不破5日线

    closes5 = [x["close"] for x in k[-5:]]

    ma5 = sum(closes5) / 5

    above_ma5 = last["close"] >= ma5 * 0.998



    # ④ 成交量堆量：最近3日平均量 > 前5日平均量，且最近一天不明显缩量

    recent3 = sum(x["volume"] for x in k[-3:]) / 3

    prev5 = sum(x["volume"] for x in k[-8:-3]) / 5

    volume_stack = recent3 >= prev5 * 1.15 and last["volume"] >= prev5 * 0.90



    # ⑤ 底部筹码不动：

    # 公开K线接口没有完整历史筹码分布，因此使用“底部区间低点稳定+

    # 回撤较小+放量上涨”的透明K线代理，不伪造真实筹码数据。

    look = k[-20:]

    low20 = min(x["low"] for x in look)

    recent_low = min(x["low"] for x in k[-5:])

    chip_proxy = (

        recent_low >= low20 * 0.97

        and last["close"] >= low20 * 1.05

        and recent3 >= prev5 * 0.90

    )



    return [five_up, zt30, above_ma5, volume_stack, chip_proxy]





# =========================

# 买点 / 持有 / 卖点规则

# =========================



def candle_body(row):

    return abs(row["close"] - row["open"])





def get_signals(k: List[Dict[str, float]]) -> Dict[str, Any]:

    if len(k) < 12:

        return {

            "buy": [],

            "hold": [],

            "sell": [],

            "rules": [],

        }



    a = k[-1]

    b = k[-2]

    c = k[-3]



    buy = []

    hold = []

    sell = []

    rules = []



    # 援军战法：重挫后止跌，不再创新低

    recent = k[-8:]

    drop = max(x["high"] for x in recent[:-2]) > a["close"] * 1.08

    stabilized = a["low"] >= min(x["low"] for x in recent[:-2]) and a["close"] > a["open"]

    if drop and stabilized:

        buy.append("援军战法")

        rules.append("援军战法")



    # 三种阴线

    if a["close"] < a["open"]:

        body = candle_body(a)

        prev_body = max(candle_body(b), 0.0001)



        if a["close"] < min(b["low"], c["low"]):

            rules.append("破位阴")

        elif body > prev_body * 1.5:

            rules.append("加速阴")

        else:

            rules.append("反转阴")



        # 反转阴低点附近作为潜在买点

        if "反转阴" in rules and a["close"] >= a["low"] * 1.005:

            buy.append("反转阴低点附近")



    # 九阴九阳：最近9日多空交替/阳线占优的简化K线代理

    last9 = k[-9:]

    bull9 = sum(1 for x in last9 if x["close"] > x["open"])

    if bull9 >= 6:

        rules.append("九阴九阳")



    # 仙人指路：长上影、实体较小、收盘仍在前高附近

    upper = a["high"] - max(a["open"], a["close"])

    body = max(candle_body(a), 0.0001)

    if upper >= body * 2 and a["close"] >= max(x["high"] for x in k[-6:-1]) * 0.97:

        rules.append("仙人指路")



    # 双剑合璧：连续两日明显放量且收盘抬高

    if (

        a["volume"] > b["volume"] * 1.15

        and b["volume"] > c["volume"] * 1.05

        and a["close"] > b["close"] > c["close"]

    ):

        rules.append("双剑合璧")



    # 倚天剑：强势突破近20日高点

    high20 = max(x["high"] for x in k[-21:-1])

    if a["close"] > high20 and a["volume"] > sum(x["volume"] for x in k[-6:-1]) / 5:

        rules.append("倚天剑")



    # 屠龙刀：放量冲高后明显回落

    if (

        a["high"] >= high20 * 1.01

        and a["close"] < a["high"] * 0.97

        and a["volume"] > sum(x["volume"] for x in k[-6:-1]) / 5 * 1.3

    ):

        rules.append("屠龙刀")



    # 三不高 / 三不低

    high_higher = a["high"] > b["high"]

    low_higher = a["low"] > b["low"]

    close_higher = a["close"] > b["close"]



    if high_higher and low_higher and close_higher:

        hold.append("高点高 + 低点高 + 收盘价高")



    # 卖点：按用户要求逐项识别

    if a["high"] <= b["high"]:

        sell.append("高点不创新高")

    if a["close"] <= b["close"]:

        sell.append("收盘价不高于昨日")

    if a["low"] <= b["low"]:

        sell.append("最低点不高于昨日最低点")



    return {

        "buy": buy,

        "hold": hold,

        "sell": sell,

        "rules": list(dict.fromkeys(rules)),

        "trend": {

            "high_higher": high_higher,

            "low_higher": low_higher,

            "close_higher": close_higher,

        },

    }





# =========================

# 综合评分

# =========================



def score_stock(hard: List[bool], k: List[Dict[str, float]], lhb: Dict[str, float]) -> int:

    """

    评分100分。

    5条硬条件是主体；买卖形态只作为加减分。

    """

    score = sum(hard) * 16



    if not k:

        return max(0, min(100, score))



    last = k[-1]

    prev5 = sum(x["volume"] for x in k[-6:-1]) / 5 if len(k) >= 6 else last["volume"]

    if last["volume"] > prev5 * 1.5:

        score += 8

    elif last["volume"] > prev5 * 1.15:

        score += 5



    if last["pct"] >= 5:

        score += 5

    elif last["pct"] > 0:

        score += 2



    if lhb.get("net", 0) > 0:

        score += 4

    if lhb.get("net", 0) > 1000:

        score += 3



    sig = get_signals(k)

    score += min(8, len(sig["buy"]) * 2)

    score += min(5, len(sig["rules"]))



    # 有明确卖出信号时适当扣分

    score -= min(10, len(sig["sell"]) * 3)



    return max(0, min(100, int(score)))





# =========================

# 单票计算

# =========================



def analyze_one(stock: Dict[str, Any], target_date: str, lhb_map: Dict[str, Dict[str, float]]):

    code = stock["code"]

    try:

        end = datetime.strptime(target_date, "%Y-%m-%d")

        beg = (end - timedelta(days=75)).strftime("%Y-%m-%d")

        k = get_kline(code, beg, target_date)

        if len(k) < 35:

            return None



        # 只使用截至目标日期的数据

        k = [x for x in k if x["date"] <= target_date]

        if len(k) < 35:

            return None



        hard = calc_hard_conditions(k)

        count = sum(hard)



        # 0~2 不进入展示

        if count < 3:

            return None



        lhb = lhb_map.get(code, {

            "net": 0.0,

            "buy": 0.0,

            "sell": 0.0,

        })



        sig = get_signals(k)

        score = score_stock(hard, k, lhb)



        missing = [

            HARD_NAMES[i]

            for i, ok in enumerate(hard)

            if not ok

        ]



        if count == 5:

            category = "strong"

            category_text = "5/5 强势"

        elif count == 4:

            category = "near"

            category_text = "4/5 高度接近"

        else:

            category = "observe"

            category_text = "3/5 观察"



        last = k[-1]

        ma5 = sum(x["close"] for x in k[-5:]) / 5



        return {

            "code": code,

            "name": stock["name"],

            "market_cap": stock["market_cap"],

            "pct": round(last["pct"], 2),

            "close": round(last["close"], 2),

            "turnover": round(last["turnover"], 2),

            "ma5": round(ma5, 2),

            "hard": hard,

            "hard_count": count,

            "category": category,

            "category_text": category_text,

            "missing_conditions": missing,

            "missing_text": "、".join(missing) if missing else "无",

            "zt30": any(is_limit_up(x) for x in k[-30:]),

            "lhb": bool(code in lhb_map),

            "lhb_net": round(lhb.get("net", 0), 2),

            "lhb_buy": round(lhb.get("buy", 0), 2),

            "lhb_sell": round(lhb.get("sell", 0), 2),

            "score": score,

            "signals": sig,

            "data_note": "⑤底部筹码不动为公开K线代理判断",

        }

    except Exception:

        return None





def build_scan(target_date: Optional[str] = None):

    target = actual_target_date(target_date)

    lhb_map = get_lhb(target)

    stocks = get_stock_list(1000)



    results = []



    # 控制东方财富请求压力

    with ThreadPoolExecutor(max_workers=8) as pool:

        futures = [

            pool.submit(analyze_one, s, target, lhb_map)

            for s in stocks

        ]

        for f in as_completed(futures):

            try:

                item = f.result()

                if item:

                    results.append(item)

            except Exception:

                pass



    def rank_key(x):

        return (

            x["score"],

            x["hard_count"],

            x["lhb_net"],

            x["zt30"],

            x["pct"],

        )



    strong = sorted(

        [x for x in results if x["category"] == "strong"],

        key=rank_key, reverse=True

    )

    near = sorted(

        [x for x in results if x["category"] == "near"],

        key=rank_key, reverse=True

    )

    observe = sorted(

        [x for x in results if x["category"] == "observe"],

        key=rank_key, reverse=True

    )



    if strong:

        top3 = strong[:3]

        top3_type = "5/5 强势"

        top3_note = "TOP 3来自5/5硬条件全部满足的强势池"

    elif near:

        top3 = near[:3]

        top3_type = "4/5 替补"

        top3_note = "今日暂无5/5，以下为4/5高度接近标的"

    else:

        top3 = observe[:3]

        top3_type = "3/5 观察"

        top3_note = "今日暂无5/5或4/5，以下仅供观察，不视为强势票"



    ranking = sorted(

        results,

        key=lambda x: (x["hard_count"], x["score"], x["lhb_net"]),

        reverse=True,

    )



    return {

        "date": target,

        "top3": top3,

        "top3_type": top3_type,

        "top3_note": top3_note,

        "strong": strong[:30],

        "near": near[:30],

        "observe": observe[:30],

        "ranking": ranking[:100],

        "strong_count": len(strong),

        "near_count": len(near),

        "observe_count": len(observe),

        "rules": HARD_NAMES,

        "notice": "只有①②③④⑤是选股硬条件；买点、持有、卖点属于操作提示，不计入硬条件。",

    }





# =========================

# API

# =========================



@app.get("/api/scanner")

def scanner(date: Optional[str] = Query(default=None)):

    try:

        return build_scan(date)

    except Exception as e:

        return {

            "date": normalize_date(date),

            "top3": [],

            "top3_type": "暂无",

            "top3_note": "数据接口暂时不可用",

            "strong": [],

            "near": [],

            "observe": [],

            "ranking": [],

            "error": str(e),

        }





# =========================

# 手机端网页

# =========================



HTML = r"""

<!doctype html>

<html lang="zh-CN">

<head>

<meta charset="utf-8">

<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">

<title>妖股雷达</title>

<style>

*{box-sizing:border-box}

body{

 margin:0;background:#07090d;color:#eee;

 font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Arial,sans-serif;

}

.wrap{max-width:900px;margin:auto;padding:14px}

h1{font-size:24px;margin:4px 0}

.sub{color:#8f98a8;font-size:13px;margin-bottom:14px}

.toolbar{display:flex;gap:8px;margin-bottom:12px}

input,button{

 border:1px solid #252b36;background:#11151c;color:#fff;

 border-radius:8px;padding:10px 12px;font-size:14px

}

button{cursor:pointer}

button:hover{background:#181e27}

.card{

 background:#0d1117;border:1px solid #202733;border-radius:12px;

 padding:13px;margin:10px 0

}

.rules{display:grid;grid-template-columns:1fr 1fr;gap:8px}

.rule{padding:10px;background:#11161e;border-radius:8px;font-size:13px}

.notice{color:#9aa5b5;font-size:12px;line-height:1.6}

.title{font-size:18px;font-weight:700;margin-bottom:10px}

.topnote{font-size:12px;color:#ffb15c;margin-bottom:10px}

.grid{display:grid;grid-template-columns:1fr;gap:10px}

.stock{

 background:#11161e;border:1px solid #252d39;border-radius:10px;padding:12px

}

.stockhead{display:flex;justify-content:space-between;gap:10px}

.name{font-size:17px;font-weight:700}

.code{color:#7e899a;font-size:12px}

.score{font-size:21px;font-weight:800}

.badge{display:inline-block;padding:3px 7px;border-radius:5px;font-size:11px;margin-top:5px}

.red{background:#4a1418;color:#ff6974}

.orange{background:#493117;color:#ffb35e}

.yellow{background:#49410e;color:#f4dc64}

.row{font-size:13px;line-height:1.75;color:#c6ccd6}

.good{color:#ff4d5c}

.ok{color:#55d6a2}

.warn{color:#ffbd55}

.gray{color:#818b9b}

.missing{color:#ff8f68;font-size:12px;margin-top:4px}

.signal{

 margin-top:8px;padding:8px;background:#0b0f15;border-radius:7px;

 font-size:12px;line-height:1.7

}

.signal b{color:#fff}

table{width:100%;border-collapse:collapse;font-size:12px}

th,td{padding:8px 5px;border-bottom:1px solid #202733;text-align:left}

th{color:#8994a4}

.green{color:#4ee39b}

.redtxt{color:#ff5965}

.footer{color:#667180;font-size:11px;margin:18px 0;text-align:center}

@media(min-width:700px){.grid{grid-template-columns:repeat(3,1fr)}}

</style>

</head>

<body>

<div class="wrap">

 <h1>🔥 妖股雷达</h1>

 <div class="sub">5条硬条件 + 买点/持有/卖点规则 · 东方财富公开行情数据</div>



 <div class="toolbar">

   <input id="date" type="date">

   <button onclick="loadData()">开始扫描</button>

 </div>



 <div class="card">

   <div class="title">一、选股条件</div>

   <div class="rules">

     <div class="rule">① 连续5日上涨</div>

     <div class="rule">② 30日内有过涨停</div>

     <div class="rule">③ 收盘价不破5日线</div>

     <div class="rule">④ 成交量堆量</div>

     <div class="rule">⑤ 底部筹码不动</div>

   </div>

   <div class="notice" style="margin-top:9px">

     只有以上5条属于硬条件。⑤使用公开K线做代理判断，不能等同于券商真实筹码分布。

   </div>

 </div>



 <div class="card">

   <div class="title">🔥 TOP 3 强势票</div>

   <div id="topNote" class="topnote">正在扫描……</div>

   <div id="top3" class="grid"></div>

 </div>



 <div class="card">

   <div class="title">🔴 5/5 强势池</div>

   <div id="strong" class="grid"></div>

 </div>



 <div class="card">

   <div class="title">🟠 4/5 高度接近</div>

   <div id="near" class="grid"></div>

 </div>



 <div class="card">

   <div class="title">🟡 3/5 观察池</div>

   <div id="observe" class="grid"></div>

 </div>



 <div class="card">

   <div class="title">📊 综合评分排行</div>

   <div style="overflow:auto">

   <table>

    <thead>

     <tr><th>股票</th><th>条件</th><th>市值</th><th>涨停</th><th>龙虎榜</th><th>评分</th></tr>

    </thead>

    <tbody id="ranking"></tbody>

   </table>

   </div>

 </div>



 <div class="footer">仅用于量化研究，不构成投资建议。</div>

</div>



<script>

function esc(s){

 return String(s ?? '').replace(/[&<>"']/g,m=>({

  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'

 }[m]));

}



function badge(r){

 let cls = r.hard_count===5?'red':(r.hard_count===4?'orange':'yellow');

 return `<span class="badge ${cls}">${esc(r.category_text)}</span>`;

}



function checkHard(r){

 return r.hard.map((v,i)=>

   `<div class="row">${v?'<span class="ok">✓</span>':'<span class="gray">×</span>'} ${esc(['连续5日上涨','30日内有过涨停','收盘价不破5日线','成交量堆量','底部筹码不动'][i])}</div>`

 ).join('');

}



function signalBlock(r){

 const s=r.signals||{};

 const buy=(s.buy||[]).length?s.buy.join('、'):'暂无明确买点';

 const hold=(s.hold||[]).length?s.hold.join('、'):'暂无持有确认';

 const sell=(s.sell||[]).length?s.sell.join('、'):'暂无卖点';

 const rules=(s.rules||[]).length?s.rules.join('、'):'暂无形态规则';



 return `

 <div class="signal">

   <div><b>🟢 买点：</b><span class="${(s.buy||[]).length?'ok':'gray'}">${esc(buy)}</span></div>

   <div><b>🟡 持有：</b><span class="${(s.hold||[]).length?'ok':'gray'}">${esc(hold)}</span></div>

   <div><b>🔴 卖点：</b><span class="${(s.sell||[]).length?'redtxt':'gray'}">${esc(sell)}</span></div>

   <div><b>⚡ 触发规则：</b><span class="warn">${esc(rules)}</span></div>

 </div>`;

}



function stockCard(r){

 return `

 <div class="stock">

  <div class="stockhead">

   <div>

    <div class="name">${esc(r.name)}</div>

    <div class="code">${esc(r.code)} · 市值 ${esc(r.market_cap)}亿</div>

    ${badge(r)}

   </div>

   <div class="score">${esc(r.score)}<small style="font-size:11px">分</small></div>

  </div>



  <div class="row" style="margin-top:7px">

   收盘 ${esc(r.close)}　

   <span class="${r.pct>=0?'good':'redtxt'}">${r.pct>=0?'+':''}${esc(r.pct)}%</span>　

   5日线 ${esc(r.ma5)}

  </div>



  <div style="margin-top:7px">${checkHard(r)}</div>



  ${r.missing_text!=='无'

    ? `<div class="missing">未满足：${esc(r.missing_text)}</div>`

    : `<div class="row ok">✓ 5条硬条件全部满足</div>`}



  ${signalBlock(r)}

 </div>`;

}



function renderList(id, rows, empty){

 const el=document.getElementById(id);

 el.innerHTML=rows.length?rows.map(stockCard).join(''):

 `<div class="notice">${esc(empty)}</div>`;

}



function renderTop3(rows,type,note){

 document.getElementById('topNote').innerHTML =

   `${esc(type)}　${esc(note)}`;

 renderList('top3',rows,'今天没有满足展示条件的股票');

}



function renderRanking(rows){

 const el=document.getElementById('ranking');

 if(!rows.length){

   el.innerHTML='<tr><td colspan="6">暂无数据</td></tr>';

   return;

 }

 el.innerHTML=rows.map(r=>`

  <tr>

   <td><b>${esc(r.name)}</b><br><span class="gray">${esc(r.code)}</span></td>

   <td>${esc(r.hard_count)}/5</td>

   <td>${esc(r.market_cap)}亿</td>

   <td>${r.zt30?'有':'无'}</td>

   <td class="${r.lhb_net>=0?'green':'redtxt'}">${r.lhb?'有':'无'} ${esc(r.lhb_net)}万</td>

   <td><b>${esc(r.score)}</b></td>

  </tr>

 `).join('');

}



async function loadData(){

 const d=document.getElementById('date').value;

 document.getElementById('topNote').textContent='正在扫描东方财富公开数据……';

 try{

   const res=await fetch('/api/scanner?date='+encodeURIComponent(d));

   const data=await res.json();



   if(data.error){

     document.getElementById('topNote').textContent='数据接口错误：'+data.error;

     return;

   }



   document.getElementById('date').value=data.date;

   renderTop3(data.top3||[],data.top3_type||'暂无',data.top3_note||'');

   renderList('strong',data.strong||[],'暂无5/5强势票');

   renderList('near',data.near||[],'暂无4/5高度接近票');

   renderList('observe',data.observe||[],'暂无3/5观察票');

   renderRanking(data.ranking||[]);

 }catch(e){

   document.getElementById('topNote').textContent='加载失败：'+e;

 }

}



document.getElementById('date').value=new Date().toISOString().slice(0,10);

loadData();

</script>

</body>

</html>

"""



@app.get("/", response_class=HTMLResponse)

def home():

    return HTML
