import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

import requests
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
# 基础
# =========================================================

def get_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def clean_code(code: str) -> str:
    code = str(code or "").strip()

    if "." in code:
        code = code.split(".")[-1]

    return code.zfill(6)


def normalize_date(date: str) -> str:
    if not date:
        return TODAY

    date = date.replace("-", "").replace("/", "").strip()

    if len(date) != 8 or not date.isdigit():
        return TODAY

    return date


# =========================================================
# 交易日
# =========================================================

def get_trading_dates(
    end_date: str,
    count: int = 45
) -> List[str]:

    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    params = {
        "secid": "1.000001",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2":
            "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
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
            dates.append(
                row.split(",")[0].replace("-", "")
            )
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
        "fs":
            "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields":
            "f2,f3,f8,f12,f14,f20,f21"
    }

    data = get_json(url, params)

    diff = data.get("data", {}).get("diff", [])

    result = []

    for x in diff:

        code = clean_code(x.get("f12", ""))

        if not code:
            continue

        # 排除明显非A股代码
        if not (
            code.startswith(("000", "001", "002",
                              "003", "300", "301",
                              "600", "601", "603",
                              "605", "688", "689"))
        ):
            continue

        result.append({
            "code": code,
            "name": x.get("f14", ""),
            "price": float(x.get("f2") or 0),
            "pct": float(x.get("f3") or 0),
            "turnover": float(x.get("f8") or 0),
            "market_cap":
                float(x.get("f20") or 0) / 1e8
        })

    return result


# =========================================================
# K线
# =========================================================

def get_kline(
    code: str,
    end_date: str,
    limit: int = 45
) -> List[Dict[str, Any]]:

    market = "1" if code.startswith(
        ("6", "68")
    ) else "0"

    secid = f"{market}.{code}"

    url = (
        "https://push2his.eastmoney.com/"
        "api/qt/stock/kline/get"
    )

    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2":
            "f51,f52,f53,f54,f55,f56,"
            "f57,f58,f59,f60,f61",
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

    url = (
        "https://datacenter-web.eastmoney.com/"
        "api/data/v1/get"
    )

    date_fmt = (
        f"{target_date[:4]}-"
        f"{target_date[4:6]}-"
        f"{target_date[6:]}"
    )

    params = {
        "reportName":
            "RPT_DAILYBILLBOARD_DETAILSNEW",

        "columns":
            "SECURITY_CODE,SECURITY_NAME_ABBR,"
            "BILLBOARD_NET_AMT,"
            "BILLBOARD_BUY_AMT,"
            "BILLBOARD_SELL_AMT,"
            "TRADE_DATE",

        "filter":
            f"(TRADE_DATE='{date_fmt}')",

        "pageNumber": "1",
        "pageSize": "500",
        "sortColumns": "BILLBOARD_NET_AMT",
        "sortTypes": "-1"
    }

    data = get_json(url, params)

    rows = (
        data.get("result", {})
        .get("data", [])
        or []
    )

    result = {}

    for x in rows:

        code = clean_code(
            x.get("SECURITY_CODE", "")
        )

        if not code:
            continue

        result[code] = {
            "name":
                x.get("SECURITY_NAME_ABBR", ""),

            "net":
                float(x.get("BILLBOARD_NET_AMT") or 0),

            "buy":
                float(x.get("BILLBOARD_BUY_AMT") or 0),

            "sell":
                float(x.get("BILLBOARD_SELL_AMT") or 0)
        }

    return result


# =========================================================
# 涨停判断
# =========================================================

def is_limit_up(
    row: Dict[str, Any],
    code: str
) -> bool:

    pct = row.get("pct", 0)

    # 创业板 / 科创板
    if code.startswith(("300", "301", "688", "689")):
        return pct >= 19.5

    # 北交所
    if code.startswith(("8", "4")):
        return pct >= 29

    # 主板
    return pct >= 9.5


# =========================================================
# 五项硬条件
# =========================================================

def calculate_hard_conditions(
    code: str,
    kline: List[Dict[str, Any]]
) -> Dict[str, Any]:

    if len(kline) < 30:

        return {
            "valid": False,
            "reason": "K线不足30日"
        }

    last = kline[-1]

    closes = [
        x["close"]
        for x in kline
    ]

    volumes = [
        x["volume"]
        for x in kline
    ]

    # -----------------------------------------------------
    # 硬条件1：连续5日上涨
    # -----------------------------------------------------

    last5 = kline[-5:]

    five_up = all(
        last5[i]["close"]
        > last5[i - 1]["close"]
        for i in range(1, 5)
    )

    # -----------------------------------------------------
    # 硬条件2：30日内有过涨停
    # -----------------------------------------------------

    last30 = kline[-30:]

    zt30_count = sum(
        1
        for x in last30
        if is_limit_up(x, code)
    )

    zt30 = zt30_count > 0

    # -----------------------------------------------------
    # 硬条件3：收盘不破5日线
    # -----------------------------------------------------

    ma5 = sum(closes[-5:]) / 5

    above_ma5 = (
        last["close"] >= ma5
    )

    # -----------------------------------------------------
    # 硬条件4：堆量成交量
    #
    # 定义：
    # 当日成交量 >= 前20日平均成交量 × 1.5
    # -----------------------------------------------------

    avg20_volume = (
        sum(volumes[-21:-1]) / 20
    )

    if avg20_volume > 0:

        volume_ratio = (
            last["volume"]
            / avg20_volume
        )

    else:

        volume_ratio = 0

    volume_stack = (
        volume_ratio >= 1.5
    )

    # -----------------------------------------------------
    # 硬条件5：底部筹码不动
    #
    # 使用公开K线进行可验证代理：
    #
    # 1. 最近10日最低价没有明显跌破
    #    前20日低点
    #
    # 2. 最新收盘距离近期低点至少5%
    #
    # 3. 最近10日没有出现异常破位
    # -----------------------------------------------------

    previous20 = kline[-30:-10]

    old_low = min(
        x["low"]
        for x in previous20
    )

    recent10 = kline[-10:]

    recent10_low = min(
        x["low"]
        for x in recent10
    )

    no_break_bottom = (
        recent10_low
        >= old_low * 0.95
    )

    close_away_bottom = (
        last["close"]
        >= recent10_low * 1.05
    )

    bottom_stable = (
        no_break_bottom
        and close_away_bottom
    )

    hard = [
        five_up,
        zt30,
        above_ma5,
        volume_stack,
        bottom_stable
    ]

    return {

        "valid": True,

        # 五项硬条件
        "five_up": five_up,
        "zt30": zt30,
        "above_ma5": above_ma5,
        "volume_stack": volume_stack,
        "bottom_stable": bottom_stable,

        "hard_count":
            sum(1 for x in hard if x),

        "hard": hard,

        "zt30_count":
            zt30_count,

        "ma5":
            ma5,

        "volume_ratio":
            volume_ratio,

        "close":
            last["close"],

        "pct":
            last["pct"],

        "turnover":
            last["turnover"]
    }


# =========================================================
# 买卖时机提示
# =========================================================

def get_buy_sell_signals(
    kline: List[Dict[str, Any]]
) -> Dict[str, Any]:

    if len(kline) < 10:

        return {
            "buy_signals": [],
            "risk_signals": [],
            "hold_signal": "",
            "sell_signal": ""
        }

    last = kline[-1]
    prev = kline[-2]

    recent3 = kline[-3:]

    buy_signals = []
    risk_signals = []

    # =====================================================
    # 1. 援军战法
    #
    # 股价重挫后企稳：
    # 前一日明显下跌
    # 当前不再创新低
    # 当前收盘重新走强
    # =====================================================

    rescue = (
        prev["pct"] <= -3
        and last["low"] >= prev["low"]
        and last["close"] > prev["close"]
    )

    if rescue:
        buy_signals.append(
            "援军战法：重挫后企稳"
        )

    # =====================================================
    # 2. 破位阴线
    # =====================================================

    ma5 = sum(
        x["close"]
        for x in kline[-5:]
    ) / 5

    breakdown = (
        last["close"] < last["open"]
        and last["close"] < ma5
        and last["close"] < prev["low"]
    )

    if breakdown:
        risk_signals.append(
            "破位阴线：注意风险"
        )

    # =====================================================
    # 3. 加速阴线
    # =====================================================

    acceleration = (
        last["close"] < last["open"]
        and last["pct"] <= -3
        and last["volume"]
        >= prev["volume"] * 1.3
    )

    if acceleration:
        risk_signals.append(
            "加速阴线：注意减仓"
        )

    # =====================================================
    # 4. 反转阴线
    #
    # 当日收阴但下影明显，
    # 且低点没有继续明显破位
    # =====================================================

    body = abs(
        last["close"] - last["open"]
    )

    lower_shadow = (
        min(last["open"], last["close"])
        - last["low"]
    )

    reversal_yin = (
        last["close"] < last["open"]
        and lower_shadow > body * 1.5
        and last["low"] >= prev["low"] * 0.98
    )

    if reversal_yin:
        buy_signals.append(
            "反转阴线：低位观察买点"
        )

    # =====================================================
    # 5. 仙人指路
    # =====================================================

    upper_shadow = (
        last["high"]
        - max(last["open"], last["close"])
    )

    xianren = (
        upper_shadow
        >= body * 2
        and last["close"] >= last["open"] * 0.98
        and last["volume"] >= prev["volume"]
    )

    if xianren:
        buy_signals.append(
            "仙人指路：观察突破"
        )

    # =====================================================
    # 6. 双剑合璧
    #
    # 这里采用量价确认：
    # 收盘走强 + 成交量放大
    # =====================================================

    double_sword = (
        last["close"] > prev["close"]
        and last["volume"]
        >= prev["volume"] * 1.2
        and last["close"] >= last["open"]
    )

    if double_sword:
        buy_signals.append(
            "双剑合璧：量价配合"
        )

    # =====================================================
    # 7. 倚天剑
    #
    # 强势突破近期高点
    # =====================================================

    recent_high = max(
        x["high"]
        for x in kline[-20:-1]
    )

    yitian = (
        last["close"] > recent_high
        and last["volume"] >=
        sum(x["volume"] for x in kline[-5:-1]) / 4
    )

    if yitian:
        buy_signals.append(
            "倚天剑：突破近期高点"
        )

    # =====================================================
    # 8. 屠龙刀
    #
    # 放量突破
    # =====================================================

    recent20_high = max(
        x["high"]
        for x in kline[-20:-1]
    )

    avg10_volume = (
        sum(x["volume"] for x in kline[-11:-1])
        / 10
    )

    tulong = (
        last["close"] > recent20_high
        and last["volume"] >= avg10_volume * 1.5
    )

    if tulong:
        buy_signals.append(
            "屠龙刀：放量突破"
        )

    # =====================================================
    # 9. 九阴九阳
    #
    # 作为趋势提示，不作为硬条件
    # =====================================================

    last9 = kline[-9:]

    up_days = sum(
        1 for x in last9
        if x["close"] > x["open"]
    )

    down_days = sum(
        1 for x in last9
        if x["close"] < x["open"]
    )

    if up_days >= 7:
        buy_signals.append(
            "九阴九阳：多方占优"
        )

    elif down_days >= 7:
        risk_signals.append(
            "九阴九阳：空方占优"
        )

    # =====================================================
    # 持股 / 卖出规则
    # =====================================================

    if (
        last["high"] > prev["high"]
        and last["low"] > prev["low"]
        and last["close"] > prev["close"]
    ):
        hold_signal = (
            "持股：高点高、低点高、收盘高"
        )
    else:
        hold_signal = ""

    sell_reasons = []

    if last["high"] <= prev["high"]:
        sell_reasons.append(
            "高点不创新高"
        )

    if last["close"] <= prev["close"]:
        sell_reasons.append(
            "收盘价不高于昨日"
        )

    if last["low"] <= prev["low"]:
        sell_reasons.append(
            "最低点不高于昨日最低点"
        )

    if sell_reasons:
        sell_signal = (
            "卖出观察：" +
            "、".join(sell_reasons)
        )
    else:
        sell_signal = ""

    return {
        "buy_signals": buy_signals,
        "risk_signals": risk_signals,
        "hold_signal": hold_signal,
        "sell_signal": sell_signal
    }


# =========================================================
# 综合评分
#
# 注意：
# 评分只针对已经通过5项硬条件的股票
# =========================================================

def calculate_score(
    indicators: Dict[str, Any],
    lhb: Dict[str, Any]
) -> float:

    score = 0.0

    # 3日涨停强度
    score += min(
        indicators.get("zt3_count", 0) * 15,
        30
    )

    # 30日涨停次数
    score += min(
        indicators.get("zt30_count", 0) * 3,
        15
    )

    # 堆量
    score += min(
        indicators.get("volume_ratio", 0) * 8,
        20
    )

    # 龙虎榜净买
    net = float(
        lhb.get("net", 0)
    )

    if net > 0:
        score += 15

    # 涨幅
    score += min(
        max(indicators.get("pct", 0), 0) * 2,
        10
    )

    # 换手
    turnover = indicators.get(
        "turnover", 0
    )

    if turnover >= 10:
        score += 5

    if turnover >= 20:
        score += 5

    return round(
        min(score, 100),
        1
    )


# =========================================================
# 主扫描
# =========================================================

def build_scan(
    requested_date: str
) -> Dict[str, Any]:

    target_date = actual_target_date(
        requested_date
    )

    lhb = get_lhb(target_date)

    stocks = get_stock_list()

    results = []

    for stock in stocks:

        code = stock["code"]

        # 市值作为评分/筛选范围
        # 这里保留你之前要求的 <=300亿
        market_cap = stock["market_cap"]

        if market_cap <= 0 or market_cap > 300:
            continue

        kline = get_kline(
            code,
            target_date,
            45
        )

        if len(kline) < 30:
            continue

        indicators = calculate_hard_conditions(
            code,
            kline
        )

        if not indicators["valid"]:
            continue

        # -------------------------------------------------
        # 近3日涨停次数
        # -------------------------------------------------

        last3 = kline[-3:]

        zt3_count = sum(
            1
            for x in last3
            if is_limit_up(x, code)
        )

        indicators["zt3_count"] = zt3_count

        # -------------------------------------------------
        # 5项硬条件
        # -------------------------------------------------

        hard = [
            indicators["five_up"],
            indicators["zt30"],
            indicators["above_ma5"],
            indicators["volume_stack"],
            indicators["bottom_stable"]
        ]

        hard_count = sum(
            1 for x in hard if x
        )

        # -------------------------------------------------
        # 必须5项全部满足
        # -------------------------------------------------

        if hard_count != 5:
            continue

        # -------------------------------------------------
        # 龙虎榜只是评分依据
        # -------------------------------------------------

        lhb_item = lhb.get(
            code,
            {
                "name": stock["name"],
                "net": 0,
                "buy": 0,
                "sell": 0
            }
        )

        score = calculate_score(
            indicators,
            lhb_item
        )

        # -------------------------------------------------
        # 买卖提示
        # -------------------------------------------------

        signals = get_buy_sell_signals(
            kline
        )

        results.append({

            "code":
                code,

            "name":
                stock["name"],

            "market_cap":
                round(market_cap, 2),

            "price":
                round(indicators["close"], 2),

            "pct":
                round(indicators["pct"], 2),

            "turnover":
                round(indicators["turnover"], 2),

            # 五项硬条件
            "five_up":
                indicators["five_up"],

            "zt30":
                indicators["zt30_count"],

            "above_ma5":
                indicators["above_ma5"],

            "volume_stack":
                indicators["volume_stack"],

            "bottom_stable":
                indicators["bottom_stable"],

            "hard_count":
                hard_count,

            # 其他评分指标
            "zt3":
                zt3_count,

            "volume_ratio":
                round(
                    indicators["volume_ratio"],
                    2
                ),

            "lhb_net":
                round(
                    lhb_item["net"] / 1e8,
                    2
                ),

            "lhb_buy":
                round(
                    lhb_item["buy"] / 1e8,
                    2
                ),

            "lhb_sell":
                round(
                    lhb_item["sell"] / 1e8,
                    2
                ),

            "lhb":
                code in lhb,

            "score":
                score,

            # 买卖提示
            "buy_signals":
                signals["buy_signals"],

            "risk_signals":
                signals["risk_signals"],

            "hold_signal":
                signals["hold_signal"],

            "sell_signal":
                signals["sell_signal"]
        })

        time.sleep(0.05)

    # -----------------------------------------------------
    # 综合评分排名
    # -----------------------------------------------------

    results.sort(
        key=lambda x: (
            x["score"],
            x["lhb_net"],
            x["zt3"],
            x["pct"]
        ),
        reverse=True
    )

    return {

        "requested_date":
            requested_date,

        "date":
            target_date,

        "hard_count":
            len(results),

        "top3":
            results[:3],

        "ranking":
            results,

        "source":
            "东方财富公开行情接口",

        "note":
            "5项为硬条件；买卖时机、持股和卖出仅作为提示。"
    }


# =========================================================
# API
# =========================================================

@app.get("/api/scanner")
def scanner(
    date: Optional[str] = Query(default="")
):

    requested_date = normalize_date(date)

    if requested_date > TODAY:
        requested_date = TODAY

    try:

        return build_scan(
            requested_date
        )

    except Exception as e:

        return {
            "requested_date":
                requested_date,

            "date":
                requested_date,

            "hard_count":
                0,

            "top3":
                [],

            "ranking":
                [],

            "error":
                str(e)
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
content="width=device-width,
initial-scale=1,
maximum-scale=1,
user-scalable=no">

<title>妖股雷达</title>

<style>

*{
    box-sizing:border-box;
}

body{
    margin:0;
    background:#07090f;
    color:#f2f4f8;
    font-family:
    -apple-system,
    BlinkMacSystemFont,
    "PingFang SC",
    "Microsoft YaHei",
    Arial;
}

.container{
    max-width:900px;
    margin:auto;
    padding:16px;
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
    color:#ff3030;
}

.source{
    color:#8e97a8;
    font-size:12px;
}

.card{
    background:#11151e;
    border:1px solid #252d3d;
    border-radius:18px;
    padding:16px;
    margin-bottom:16px;
}

input{
    width:100%;
    padding:15px;
    border-radius:14px;
    border:1px solid #3a4356;
    background:#080a10;
    color:white;
    font-size:18px;
    text-align:center;
}

button{
    width:100%;
    padding:14px;
    margin-top:10px;
    border:0;
    border-radius:14px;
    font-size:17px;
    font-weight:800;
}

.search{
    background:#ed3030;
    color:white;
}

.today{
    background:#273144;
    color:white;
}

.section-title{
    font-size:23px;
    font-weight:900;
    margin:24px 0 12px;
}

.hard{
    line-height:1.9;
    color:#c4cbd8;
}

.hard b{
    color:white;
}

.warning{
    background:#141923;
    border-left:4px solid #ff3b3b;
    padding:13px;
    border-radius:10px;
    color:#aab2c0;
    font-size:13px;
    line-height:1.7;
}

.stock{
    background:#121722;
    border:1px solid #293243;
    border-radius:17px;
    padding:16px;
    margin-bottom:13px;
}

.stock-head{
    display:flex;
    justify-content:space-between;
}

.name{
    font-size:21px;
    font-weight:900;
}

.code{
    color:#8e97a7;
    font-size:12px;
    margin-top:4px;
}

.score{
    color:#ff4545;
    font-size:26px;
    font-weight:900;
}

.meta{
    display:grid;
    grid-template-columns:
    repeat(2,1fr);
    gap:7px;
    margin-top:12px;
}

.meta div{
    background:#1b2130;
    padding:8px;
    border-radius:8px;
    font-size:12px;
    color:#b7bfce;
}

.signal{
    margin-top:12px;
    padding:11px;
    border-radius:10px;
    background:#171d2a;
    line-height:1.8;
    font-size:13px;
}

.signal-title{
    font-weight:900;
    margin-bottom:4px;
}

.buy{
    color:#37dc98;
}

.risk{
    color:#ff5555;
}

.hold{
    color:#45c7ff;
}

.sell{
    color:#ff9d42;
}

.empty{
    text-align:center;
    color:#818a9b;
    padding:35px 10px;
}

table{
    width:100%;
    border-collapse:collapse;
    font-size:12px;
}

th,td{
    padding:10px 4px;
    border-bottom:1px solid #272e3b;
    text-align:center;
}

th{
    color:#8992a3;
}

.red{
    color:#ff5151;
}

.green{
    color:#36d996;
}

.footer{
    color:#616a7a;
    text-align:center;
    font-size:12px;
    line-height:1.8;
    margin:30px 0;
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


<div class="card hard">

<b>🔴 5项核心硬条件</b><br>

① 连续5日上涨<br>

② 30日内有过涨停<br>

③ 收盘不破5日线<br>

④ 堆量成交量<br>

⑤ 底部筹码不动

<br><br>

<b>说明：</b>

以上5项必须全部满足，
才进入强势候选池。

</div>


<div class="warning">

⚠️ 买卖时机、持股和卖出规则
只作为操作提示，不作为选股硬条件。

<br>

历史日期不伪造历史集合竞价数据。

</div>


<div class="section-title">
🔥 强势 TOP3
</div>

<div id="top3"></div>


<div class="section-title">
📊 综合评分排行
</div>

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

5项硬条件 + 综合评分 + 买卖操作提示<br>

本工具仅用于信息整理与量化筛选，不构成投资建议

</div>

</div>


<script>

function money(v){

    v = Number(v || 0);

    if(Math.abs(v) >= 1){

        return v.toFixed(2) + "亿";

    }

    return Math.round(v * 10000) + "万";
}


function signalsHtml(r){

    let html = "";


    if(r.buy_signals &&
       r.buy_signals.length){

        html += `
        <div class="signal buy">
        <div class="signal-title">
        🟢 买卖时机提示
        </div>
        ${r.buy_signals.join("<br>")}
        </div>
        `;

    }


    if(r.risk_signals &&
       r.risk_signals.length){

        html += `
        <div class="signal risk">
        <div class="signal-title">
        🔴 风险提示
        </div>
        ${r.risk_signals.join("<br>")}
        </div>
        `;

    }


    if(r.hold_signal){

        html += `
        <div class="signal hold">
        <div class="signal-title">
        🔵 持股提示
        </div>
        ${r.hold_signal}
        </div>
        `;

    }


    if(r.sell_signal){

        html += `
        <div class="signal sell">
        <div class="signal-title">
        🟠 卖出规则提示
        </div>
        ${r.sell_signal}
        </div>
        `;

    }


    if(!html){

        html = `
        <div class="signal">
        当前没有明显的买卖/持股信号
        </div>
        `;

    }


    return html;
}


function renderTop3(rows){

    const box =
        document.getElementById("top3");


    if(!rows ||
       rows.length === 0){

        box.innerHTML = `
        <div class="empty">
        当前日期没有股票同时满足5项硬条件
        </div>
        `;

        return;
    }


    box.innerHTML = rows.map(
        (r,i)=>`

        <div class="stock">

        <div class="stock-head">

        <div>

        <div class="name">
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


        <div class="meta">

        <div>
        连续5日上涨：✅
        </div>

        <div>
        30日涨停：${r.zt30}次
        </div>

        <div>
        不破5日线：✅
        </div>

        <div>
        堆量：${r.volume_ratio}倍
        </div>

        <div>
        底部筹码：稳定
        </div>

        <div>
        近3日涨停：${r.zt3}次
        </div>

        <div>
        龙虎榜：${r.lhb ? "有" : "无"}
        </div>

        <div>
        市值：${r.market_cap}亿
        </div>

        </div>


        ${signalsHtml(r)}

        </div>

        `
    ).join("");
}


function renderRanking(rows){

    const box =
        document.getElementById("ranking");


    if(!rows ||
       rows.length === 0){

        box.innerHTML = `
        <tr>
        <td colspan="5">
        暂无满足5项硬条件的数据
        </td>
        </tr>
        `;

        return;
    }


    box.innerHTML = rows.map(
        r=>`

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

        <td class="${
            r.lhb_net >= 0
            ? "red"
            : "green"
        }">
        ${money(r.lhb_net)}
        </td>

        <td class="red">
        <b>${r.score}</b>
        </td>

        </tr>

        `
    ).join("");
}


async function loadData(){

    const input =
        document.getElementById("date");

    const date =
        input.value.replaceAll("-", "");


    document.getElementById("top3").innerHTML =
        `
        <div class="empty">
        正在扫描东方财富数据……
        </div>
        `;


    try{

        const res =
            await fetch(
                "/api/scanner?date=" + date
            );


        const data =
            await res.json();


        renderTop3(
            data.top3 || []
        );


        renderRanking(
            data.ranking || []
        );


    }catch(e){

        document.getElementById("top3").innerHTML =
            `
            <div class="empty">
            数据加载失败，请稍后刷新
            </div>
            `;

    }

}


function setToday(){

    const d =
        new Date();


    const y =
        d.getFullYear();


    const m =
        String(
            d.getMonth()+1
        ).padStart(2,"0");


    const day =
        String(
            d.getDate()
        ).padStart(2,"0");


    document.getElementById("date").value =
        `${y}-${m}-${day}`;


    loadData();

}


window.onload = function(){

    const d =
        new Date();


    const y =
        d.getFullYear();


    const m =
        String(
            d.getMonth()+1
        ).padStart(2,"0");


    const day =
        String(
            d.getDate()
        ).padStart(2,"0");


    document.getElementById("date").value =
        `${y}-${m}-${day}`;


    loadData();

};

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


# =========================================================
# 启动
# =========================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                10000
            )
        )
    )
