import os
import time
import math
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

app = FastAPI(title="妖股雷达")

TZ = ZoneInfo("Asia/Shanghai")
TODAY = datetime.now(TZ).strftime("%Y%m%d")
TIMEOUT = 12

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/"
})


# =========================================================
# 基础工具
# =========================================================

def normalize_date(date: str) -> str:
    if not date:
        return TODAY

    date = date.replace("-", "").replace("/", "").strip()

    if len(date) != 8 or not date.isdigit():
        return TODAY

    return date


def get_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def clean_code(code: str) -> str:
    code = str(code).strip()

    if "." in code:
        code = code.split(".")[-1]

    return code.zfill(6)


def money(v: float) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0


# =========================================================
# 交易日
# =========================================================

def get_trading_dates(end_date: str, count: int = 35) -> List[str]:
    end_date = normalize_date(end_date)

    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    params = {
        "secid": "1.000001",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": "0",
        "end": end_date,
        "lmt": str(count)
    }

    data = get_json(url, params)

    rows = data.get("data", {}).get("klines", [])

    dates = []

    for row in rows:
        try:
            d = row.split(",")[0].replace("-", "")
            dates.append(d)
        except Exception:
            pass

    return dates


def actual_target_date(date: str) -> str:
    dates = get_trading_dates(date, 10)

    if dates:
        return dates[-1]

    return date


# =========================================================
# 股票列表
# =========================================================

def get_stock_list() -> List[Dict[str, Any]]:
    url = "https://push2.eastmoney.com/api/qt/clist/get"

    params = {
        "pn": "1",
        "pz": "6000",
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f2,f3,f8,f12,f14,f20,f21"
    }

    data = get_json(url, params)

    diff = data.get("data", {}).get("diff", [])

    result = []

    for x in diff:
        code = clean_code(x.get("f12", ""))

        if not code:
            continue

        result.append({
            "code": code,
            "name": x.get("f14", ""),
            "price": money(x.get("f2")),
            "pct": money(x.get("f3")),
            "turnover": money(x.get("f8")),
            "market_cap": money(x.get("f20")) / 1e8
        })

    return result


# =========================================================
# 历史K线
# =========================================================

def get_kline(code: str, end_date: str, limit: int = 45) -> List[Dict[str, Any]]:
    market = "1" if code.startswith(("6", "68")) else "0"

    secid = f"{market}.{code}"

    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": "0",
        "end": end_date,
        "lmt": str(limit)
    }

    data = get_json(url, params)

    rows = data.get("data", {}).get("klines", [])

    result = []

    for row in rows:
        try:
            p = row.split(",")

            result.append({
                "date": p[0].replace("-", ""),
                "open": float(p[1]),
                "close": float(p[2]),
                "high": float(p[3]),
                "low": float(p[4]),
                "volume": float(p[5]),
                "amount": float(p[6]),
                "amplitude": float(p[7]),
                "pct": float(p[8]),
                "change": float(p[9]),
                "turnover": float(p[10])
            })

        except Exception:
            continue

    return result


# =========================================================
# 龙虎榜
# =========================================================

def get_lhb(target_date: str) -> Dict[str, Dict[str, Any]]:
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"

    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": (
            "SECURITY_CODE,SECURITY_NAME_ABBR,"
            "BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,"
            "BILLBOARD_SELL_AMT,TRADE_DATE"
        ),
        "filter": f"(TRADE_DATE='{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}')",
        "pageNumber": "1",
        "pageSize": "500",
        "sortColumns": "BILLBOARD_NET_AMT",
        "sortTypes": "-1"
    }

    data = get_json(url, params)

    result = {}

    rows = data.get("result", {}).get("data", []) or []

    for x in rows:
        code = clean_code(x.get("SECURITY_CODE", ""))

        if not code:
            continue

        result[code] = {
            "name": x.get("SECURITY_NAME_ABBR", ""),
            "net": money(x.get("BILLBOARD_NET_AMT")),
            "buy": money(x.get("BILLBOARD_BUY_AMT")),
            "sell": money(x.get("BILLBOARD_SELL_AMT"))
        }

    return result


# =========================================================
# 涨停判断
# =========================================================

def is_limit_up(row: Dict[str, Any], code: str) -> bool:
    pct = row.get("pct", 0)

    # 主板约10%，创业板/科创板约20%，北交所约30%
    if code.startswith(("300", "301", "688", "689")):
        return pct >= 19.5

    if code.startswith(("8", "4")):
        return pct >= 29.0

    return pct >= 9.5


# =========================================================
# 计算指标
# =========================================================

def calculate_indicators(
    code: str,
    kline: List[Dict[str, Any]],
    target_date: str
) -> Dict[str, Any]:

    if len(kline) < 25:
        return {
            "valid": False,
            "reason": "历史K线不足"
        }

    # 最新交易日
    last = kline[-1]

    closes = [x["close"] for x in kline]
    volumes = [x["volume"] for x in kline]

    # -----------------------------------------------------
    # 硬条件1：连续5日上涨
    # -----------------------------------------------------

    five_up = False

    if len(kline) >= 5:
        last5 = kline[-5:]

        five_up = all(
            last5[i]["close"] > last5[i - 1]["close"]
            for i in range(1, 5)
        )

    # -----------------------------------------------------
    # 硬条件2：30日内出现涨停
    # -----------------------------------------------------

    last30 = kline[-30:]

    zt30_count = sum(
        1 for x in last30
        if is_limit_up(x, code)
    )

    zt30 = zt30_count > 0

    # -----------------------------------------------------
    # 硬条件3：收盘不破5日线
    # -----------------------------------------------------

    ma5 = sum(closes[-5:]) / 5

    above_ma5 = last["close"] >= ma5

    # -----------------------------------------------------
    # 硬条件4：堆量
    # 最新成交量 >= 20日平均成交量1.5倍
    # -----------------------------------------------------

    avg20_volume = sum(volumes[-20:]) / 20

    volume_ratio = (
        last["volume"] / avg20_volume
        if avg20_volume > 0 else 0
    )

    volume_stack = volume_ratio >= 1.5

    # -----------------------------------------------------
    # 硬条件5：底部筹码稳定
    #
    # 用公开K线可验证的代理指标：
    # 最近10日最低价没有明显跌破此前20日低点
    # 同时最新收盘价不能贴近20日最低点
    # -----------------------------------------------------

    previous20 = kline[-30:-10]

    if previous20:
        old_low = min(x["low"] for x in previous20)
    else:
        old_low = min(x["low"] for x in kline[:-10])

    recent10_low = min(x["low"] for x in kline[-10:])

    bottom_stable = (
        recent10_low >= old_low * 0.95
        and last["close"] >= recent10_low * 1.05
    )

    # -----------------------------------------------------
    # 硬条件6：近3交易日出现涨停
    # -----------------------------------------------------

    last3 = kline[-3:]

    zt3_count = sum(
        1 for x in last3
        if is_limit_up(x, code)
    )

    zt3 = zt3_count > 0

    return {
        "valid": True,

        "five_up": five_up,
        "zt30": zt30,
        "zt30_count": zt30_count,

        "above_ma5": above_ma5,
        "ma5": ma5,

        "volume_stack": volume_stack,
        "volume_ratio": volume_ratio,

        "bottom_stable": bottom_stable,

        "zt3": zt3,
        "zt3_count": zt3_count,

        "last_close": last["close"],
        "last_pct": last["pct"],
        "last_turnover": last["turnover"]
    }


# =========================================================
# 扫描
# =========================================================

def build_scan(target_date: str) -> Dict[str, Any]:

    target_date = actual_target_date(target_date)

    trading_dates = get_trading_dates(target_date, 35)

    if not trading_dates:
        return {
            "date": target_date,
            "rows": [],
            "top3": [],
            "ranking": [],
            "hard_count": 0,
            "error": "无法获取交易日"
        }

    # 当日龙虎榜
    lhb = get_lhb(target_date)

    # 股票列表
    stocks = get_stock_list()

    results = []

    for stock in stocks:

        code = stock["code"]

        # -------------------------------------------------
        # 硬条件8：市值 <= 300亿
        # -------------------------------------------------

        market_cap = stock["market_cap"]

        if market_cap <= 0 or market_cap > 300:
            continue

        # -------------------------------------------------
        # 必须进入龙虎榜
        # -------------------------------------------------

        if code not in lhb:
            continue

        # -------------------------------------------------
        # 历史K线
        # -------------------------------------------------

        kline = get_kline(code, target_date, 45)

        if not kline:
            continue

        indicators = calculate_indicators(
            code,
            kline,
            target_date
        )

        if not indicators.get("valid"):
            continue

        # -------------------------------------------------
        # 8项硬条件
        # -------------------------------------------------

        hard = [
            indicators["five_up"],
            indicators["zt30"],
            indicators["above_ma5"],
            indicators["volume_stack"],
            indicators["bottom_stable"],
            indicators["zt3"],
            code in lhb,
            market_cap <= 300
        ]

        hard_count = sum(1 for x in hard if x)

        # -------------------------------------------------
        # 只有8项全部满足，才进入强势TOP3
        # -------------------------------------------------

        if hard_count == 8:

            # 综合评分
            score = 0

            # 近3日涨停
            score += min(indicators["zt3_count"] * 15, 30)

            # 30日涨停强度
            score += min(indicators["zt30_count"] * 3, 15)

            # 堆量
            score += min(
                indicators["volume_ratio"] * 8,
                20
            )

            # 龙虎榜净买额
            lhb_net = lhb[code]["net"]

            if lhb_net > 0:
                score += 15

            # 当日涨幅
            score += min(
                max(indicators["last_pct"], 0) * 2,
                10
            )

            score = round(min(score, 100), 1)

            results.append({
                "code": code,
                "name": stock["name"],
                "market_cap": round(market_cap, 2),

                "zt3": indicators["zt3_count"],
                "zt30": indicators["zt30_count"],

                "five_up": True,
                "above_ma5": True,
                "volume_stack": True,
                "bottom_stable": True,

                "volume_ratio": round(
                    indicators["volume_ratio"], 2
                ),

                "lhb_net": round(
                    lhb[code]["net"] / 1e8,
                    2
                ),

                "lhb_buy": round(
                    lhb[code]["buy"] / 1e8,
                    2
                ),

                "lhb_sell": round(
                    lhb[code]["sell"] / 1e8,
                    2
                ),

                "pct": round(
                    indicators["last_pct"],
                    2
                ),

                "turnover": round(
                    indicators["last_turnover"],
                    2
                ),

                "score": score,

                "hard_count": 8,
                "hard": hard
            })

        time.sleep(0.08)

    # 综合评分排序
    results.sort(
        key=lambda x: (
            x["score"],
            x["lhb_net"],
            x["zt3"]
        ),
        reverse=True
    )

    # TOP3
    top3 = results[:3]

    return {
        "date": target_date,
        "requested_date": target_date,
        "hard_count": len(results),
        "top3": top3,
        "ranking": results,
        "source": "东方财富公开行情接口",
        "note": (
            "8项全部满足才进入强势TOP3；"
            "历史日期不伪造历史集合竞价数据。"
        )
    }


# =========================================================
# API
# =========================================================

@app.get("/api/scanner")
def scanner(date: Optional[str] = Query(default="")):

    target_date = normalize_date(date)

    if target_date > TODAY:
        target_date = TODAY

    try:
        return build_scan(target_date)
    except Exception as e:

        return {
            "date": target_date,
            "hard_count": 0,
            "top3": [],
            "ranking": [],
            "error": str(e)
        }


# =========================================================
# 网页
# =========================================================

HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1,
      maximum-scale=1,user-scalable=no">

<title>妖股雷达</title>

<style>

*{
    box-sizing:border-box;
}

body{
    margin:0;
    background:#07090f;
    color:#f2f4f8;
    font-family:-apple-system,BlinkMacSystemFont,
    "PingFang SC","Microsoft YaHei",Arial;
}

.container{
    width:100%;
    max-width:900px;
    margin:auto;
    padding:18px;
}

.header{
    display:flex;
    justify-content:space-between;
    align-items:center;
    margin-bottom:18px;
}

.logo{
    font-size:28px;
    font-weight:900;
}

.logo span{
    color:#ff3b3b;
}

.source{
    color:#9ca3af;
    font-size:12px;
}

.card{
    background:#11151e;
    border:1px solid #252c3b;
    border-radius:18px;
    padding:18px;
    margin-bottom:16px;
}

input{
    width:100%;
    background:#080a10;
    border:1px solid #394154;
    color:#fff;
    padding:15px;
    border-radius:14px;
    font-size:18px;
    text-align:center;
}

button{
    width:100%;
    border:0;
    border-radius:14px;
    padding:14px;
    margin-top:12px;
    font-size:17px;
    font-weight:800;
}

.search{
    background:#ef3030;
    color:#fff;
}

.today{
    background:#263044;
    color:#fff;
}

.info{
    line-height:1.8;
    color:#c8ceda;
}

.info b{
    color:#fff;
}

.warning{
    border-left:5px solid #ff3b3b;
    background:#131824;
    padding:14px;
    border-radius:10px;
    color:#aeb5c3;
    font-size:13px;
    margin-bottom:22px;
}

h2{
    font-size:24px;
    margin:24px 0 14px;
}

.empty{
    text-align:center;
    padding:40px 10px;
    color:#8c94a5;
}

.stock{
    background:#151a24;
    border:1px solid #293142;
    border-radius:16px;
    padding:16px;
    margin-bottom:12px;
}

.stock-top{
    display:flex;
    justify-content:space-between;
    align-items:center;
}

.stock-name{
    font-size:21px;
    font-weight:900;
}

.score{
    color:#ff4040;
    font-size:25px;
    font-weight:900;
}

.code{
    color:#8e98aa;
    font-size:13px;
    margin-top:4px;
}

.tags{
    display:flex;
    flex-wrap:wrap;
    gap:6px;
    margin-top:12px;
}

.tag{
    background:#202738;
    border-radius:8px;
    padding:5px 8px;
    font-size:12px;
}

.red{
    color:#ff5555;
}

.green{
    color:#38d996;
}

table{
    width:100%;
    border-collapse:collapse;
    font-size:13px;
}

th,td{
    padding:12px 5px;
    border-bottom:1px solid #252b38;
    text-align:center;
}

th{
    color:#929bad;
}

.footer{
    text-align:center;
    color:#626b7c;
    font-size:12px;
    line-height:1.8;
    margin:35px 0 20px;
}

</style>
</head>

<body>

<div class="container">

<div class="header">
    <div class="logo">
        妖股<span>雷达</span>
    </div>

    <div class="source">
        东方财富
    </div>
</div>

<div class="card">

    <input
        id="date"
        type="date"
    >

    <button
        class="search"
        onclick="loadData()">
        查询
    </button>

    <button
        class="today"
        onclick="setToday()">
        今天
    </button>

</div>

<div class="card info">

<b>8项核心硬条件：</b><br>

① 连续5日上涨<br>
② 30日内有过涨停<br>
③ 收盘不破5日线<br>
④ 堆量成交量<br>
⑤ 底部筹码稳定<br>
⑥ 近3日出现涨停<br>
⑦ 进入龙虎榜<br>
⑧ 市值 ≤ 300亿

<br><br>

<b>注意：</b>
8项必须全部满足，才进入强势TOP3。

</div>

<div class="warning">
⚠️ 历史日期不会伪造历史集合竞价数据。
历史日期使用东方财富历史K线及历史龙虎榜数据。
</div>

<h2>🔥 强势 TOP3</h2>

<div id="top3"></div>

<h2>📊 综合评分排行</h2>

<div class="card">

<table>

<thead>
<tr>
<th>股票</th>
<th>市值</th>
<th>3日涨停</th>
<th>龙虎榜</th>
<th>评分</th>
</tr>
</thead>

<tbody id="ranking"></tbody>

</table>

</div>

<div class="footer">
数据来源：东方财富公开行情接口<br>
本工具仅用于信息整理与量化筛选，不构成投资建议
</div>

</div>

<script>

function money(v){

    v = Number(v || 0);

    if(Math.abs(v) >= 1){
        return v.toFixed(2) + "亿";
    }

    return (v * 10000).toFixed(0) + "万";
}


function renderTop3(rows){

    const box =
        document.getElementById("top3");

    if(!rows || rows.length === 0){

        box.innerHTML =
        '<div class="empty">' +
        '当前日期没有股票同时满足8项硬条件' +
        '</div>';

        return;
    }

    box.innerHTML =
        rows.map((r,i)=>`

        <div class="stock">

            <div class="stock-top">

                <div>
                    <div class="stock-name">
                        ${i+1}. ${r.name}
                    </div>

                    <div class="code">
                        ${r.code}
                    </div>
                </div>

                <div class="score">
                    ${r.score}
                </div>

            </div>

            <div class="tags">

                <span class="tag">
                    连涨5日
                </span>

                <span class="tag">
                    30日涨停 ${r.zt30}次
                </span>

                <span class="tag">
                    近3日涨停 ${r.zt3}次
                </span>

                <span class="tag">
                    堆量 ${r.volume_ratio}倍
                </span>

                <span class="tag">
                    龙虎榜净额 ${money(r.lhb_net)}
                </span>

                <span class="tag">
                    市值 ${r.market_cap}亿
                </span>

            </div>

        </div>

        `).join("");
}


function renderRanking(rows){

    const box =
        document.getElementById("ranking");

    if(!rows || rows.length === 0){

        box.innerHTML =
        '<tr><td colspan="5">暂无满足全部硬条件的数据</td></tr>';

        return;
    }

    box.innerHTML =
        rows.map(r=>`

        <tr>

            <td>
                <b>${r.name}</b><br>
                <small>${r.code}</small>
            </td>

            <td>
                ${r.market_cap}亿
            </td>

            <td>
                ${r.zt3}次
            </td>

            <td class="${r.lhb_net >= 0 ? 'red':'green'}">
                ${money(r.lhb_net)}
            </td>

            <td class="red">
                <b>${r.score}</b>
            </td>

        </tr>

        `).join("");
}


async function loadData(){

    const date =
        document.getElementById("date").value
        .replaceAll("-", "");

    document.getElementById("top3").innerHTML =
        '<div class="empty">正在扫描...</div>';

    try{

        const res =
            await fetch(
                "/api/scanner?date=" + date
            );

        const data =
            await res.json();

        renderTop3(data.top3 || []);

        renderRanking(data.ranking || []);

    }catch(e){

        document.getElementById("top3").innerHTML =
            '<div class="empty">数据加载失败，请稍后重试</div>';

    }
}


function setToday(){

    const d = new Date();

    const y =
        d.getFullYear();

    const m =
        String(d.getMonth()+1).padStart(2,"0");

    const day =
        String(d.getDate()).padStart(2,"0");

    document.getElementById("date").value =
        `${y}-${m}-${day}`;

    loadData();
}


window.onload = function(){

    const d = new Date();

    const y =
        d.getFullYear();

    const m =
        String(d.getMonth()+1).padStart(2,"0");

    const day =
        String(d.getDate()).padStart(2,"0");

    document.getElementById("date").value =
        `${y}-${m}-${day}`;

    loadData();
};

</script>

</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTML


# =========================================================
# 启动
# =========================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
