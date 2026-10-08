#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
妖股雷达 v3 —— 行情口径：妙想(MX) + 东方财富公开数据（不使用任何第三方行情源）

硬条件：近3交易日涨停 + 上龙虎榜 + 总市值≤300亿
评分：封板强弱20 + 连板梯队20 + 龙虎榜资金20 + 涨幅15 + 换手率15 + 机构席位10

数据源（全部实测通过）：
  妙想选股   mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen   候选池主入口
  ① 涨停池   push2ex.eastmoney.com/getTopicZTPool                  连板/首封/炸板/封单/行业/总市值
  ② 龙虎榜   datacenter RPT_DAILYBILLBOARD_DETAILSNEW              净额/成交占比/流通市值/席位摘要/上榜原因
  ③ 席位明细 datacenter RPT_BILLBOARD_DAILYDETAILSBUY|SELL         机构专用·沪深股通·游资营业部
  ④ 全板块市值 datacenter RPT_VALUEANALYSIS_DET                    补齐 30/68 开头在涨停池取不到市值的缺口
  ⑤ 交易日历 由①推导（date ≤ qdate 且 tc>0 即为真实交易日）

关键坑（已在实现中规避，勿回退）：
  · push2/push2his 系列在云端被服务端直接断连，不能用于取市值，否则市值恒为 0。
  · 涨停池对非交易日不会返回空，而是回退到最近真实交易日的同一份数据；
    直接按"今天往前数几天"取会得到多份重复池子，近3日涨停次数被虚高。
  · data.qdate 恒等于最近真实交易日，不能用来逐日判定是否交易日。
  · 涨停池 tshare 对 30/68 开头返回 0，必须由 ④ 补总市值。
  · 龙虎榜同一只票一天可能多条（多个上榜原因），按 |净额| 取最大并合并原因，不能覆盖也不能累加。
  · 龙虎榜当日约 18:00 后才披露，盘中取到空榜不能判成"没上榜"，要标"待披露"。
  · Render 服务器是 UTC，日期一律显式用 Asia/Shanghai。
"""

import os
import re
import json
import hashlib
import time
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# ============================== 配置 ==============================

MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"
EM_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_ZT = "https://push2ex.eastmoney.com/getTopicZTPool"
ZT_UT = "7eea3edcaed734bea9cbfc24409ed989"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
}

CN_TZ = timezone(timedelta(hours=8))

MAX_CAP_YI = 300.0        # 硬条件：总市值上限（亿元）
LOOKBACK = 3              # 硬条件：近 N 个交易日涨停
SEAT_LOOKBACK = 3         # 席位明细取近 N 个交易日（已确认）
LHB_PUBLISH_HOUR = 18     # 龙虎榜约当日 18:00 后披露

CACHE_SECONDS = 60        # 最新交易日数据缓存
HIST_TTL = 6 * 3600       # 历史交易日数据缓存
MX_CACHE_SECONDS = 15 * 60
DATE_TTL = 3600

BASE_DIR = Path(__file__).resolve().parent
SNAP_DIR = BASE_DIR / "snapshots"
SNAP_FILE = SNAP_DIR / "妖股雷达快照.json"

app = FastAPI(title="妖股雷达")

# ============================== 基础设施 ==============================

_lock = threading.RLock()
_STORE: dict = {}
_STATE = {
    "ts": 0.0,
    "data": [],
    "tiers": {},
    "meta": {},
    "source": "未连接",
    "error": None,
    "refreshing": False,
    "last_try": 0.0,
}


def _cache_get(key, now=None):
    now = now if now is not None else time.time()
    with _lock:
        hit = _STORE.get(key)
        if hit and hit[0] > now:
            return True, hit[1]
    return False, None


def _cache_put(key, val, ttl, now=None):
    now = now if now is not None else time.time()
    with _lock:
        _STORE[key] = (now + ttl, val)


def cached(key, ttl, producer):
    ok, val = _cache_get(key)
    if ok:
        return val
    val = producer()
    _cache_put(key, val, ttl)
    return val


def http_json(url, params=None, timeout=15, tries=3, headers=None, method="GET", payload=None):
    """带退避重试。三次都失败才抛异常。"""
    last = None
    for i in range(tries):
        try:
            if method == "POST":
                r = requests.post(url, params=params, json=payload,
                                  headers=headers or HEADERS, timeout=timeout)
            else:
                r = requests.get(url, params=params, headers=headers or HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:                      # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep(0.5 * (i + 1))
    raise last


def dc(params, tries=3):
    """datacenter 报表统一入口：success=False 会明确报错，避免列名写错时静默返回空结果。"""
    j = http_json(EM_DC, params, timeout=15, tries=tries)
    if j.get("success") is False:
        raise RuntimeError("东财报表错误: %s | %s" % (j.get("message"), params.get("reportName")))
    return j.get("result") or {}


def num(v, default=0.0):
    """支持东财/妙想的中文单位字符串："111.85亿"、"8997.11万"、"1,234.5"、"10.04%"。"""
    try:
        if v is None:
            return default
        s = str(v).strip().replace(",", "").replace("%", "")
        if s in ("", "-", "--", "None", "null"):
            return default
        for suf, mul in (("亿", 1e8), ("万", 1e4)):
            if s.endswith(suf):
                return float(s[:-len(suf)]) * mul
        return float(s)
    except Exception:                               # noqa: BLE001
        return default


def yi(v, nd=2):
    """-> 亿元。已带"亿/万"单位的字符串会自动换算；取不到返回 None（区别于 0，
    避免把取数失败当成"市值 0"从而把硬条件永久判死）。"""
    f = num(v, default=0.0)
    if f <= 0:
        return None
    return round(f / 1e8, nd)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def d8_to_dashed(d):
    return "%s-%s-%s" % (d[:4], d[4:6], d[6:])


def limit_pct(code):
    """主板 10%，创业板/科创板 20%，北交所 30%"""
    if code.startswith(("30", "68")):
        return 20.0
    if code.startswith(("43", "83", "87", "92")):
        return 30.0
    return 10.0


# ============================== 1. 妙想选股（候选池主入口） ==============================

MX_QUERY = """
近三个交易日涨停且当日换手率较高的A股，排除ST股，返回100只，
包含股票代码、股票简称、涨跌幅、换手率、总市值、概念题材。
"""

MX_HEADERS = {"Content-Type": "application/json"}
MX_LIMITED = threading.Event()          # 触发日限额后本进程不再打妙想


def build_mx_column_map(columns):
    m = {}
    for c in columns or []:
        if not isinstance(c, dict):
            continue
        en = c.get("field") or c.get("name") or c.get("key") or ""
        cn = c.get("displayName") or c.get("title") or c.get("label") or ""
        if c.get("dateMsg"):
            cn = (cn + " " + c["dateMsg"]).strip()
        if en:
            m[str(en)] = str(cn)
    return m


def mx_raw():
    key = os.getenv("MX_APIKEY")
    if not key:
        raise RuntimeError("MX_APIKEY 未设置")
    obj = http_json(MX_URL, payload={"keyword": MX_QUERY}, timeout=40, tries=2,
                    headers={**MX_HEADERS, "apikey": key}, method="POST")
    if obj.get("status") != 0:
        raise RuntimeError("妙想顶层错误 status=%s %s" % (obj.get("status"), obj.get("message")))
    biz = (obj.get("data") or {}).get("code")
    if biz in (113, "113", 429, "429"):
        MX_LIMITED.set()
        raise RuntimeError("妙想日调用次数已达上限(code=%s)，本进程后续自动降级" % biz)
    return obj


def mx_rows_of(obj):
    """优先全量 dataList，其次解析 partialResults 的 Markdown 表格"""
    inner = ((obj.get("data") or {}).get("data") or {})
    res = (inner.get("allResults") or {}).get("result") or inner.get("result") or {}
    rows, cols = res.get("dataList") or [], res.get("columns") or []
    if rows:
        cn = build_mx_column_map(cols)
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            dual = dict(r)                          # 英文键原样保留
            for k, v in r.items():
                alias = cn.get(k)
                if alias and alias != k:
                    dual.setdefault(alias, v)       # 中文标题作为别名并列
            out.append(dual)
        return out
    lines = [x.strip() for x in str(inner.get("partialResults") or "").splitlines()
             if x.strip().startswith("|")]
    if len(lines) < 3:
        return []

    def cells(x):
        return [v.strip() for v in x.strip("|").split("|")]

    head = cells(lines[0])
    out = []
    for ln in lines[2:]:
        v = cells(ln)
        if len(v) == len(head):
            out.append(dict(zip(head, v)))
    return out


def mx_pick(row, names):
    """按候选列名取值：精确命中 > 前缀命中 > 包含命中，同级别取最短列名。
    前缀优先很关键：否则搜"涨跌幅"可能命中"近5日涨跌幅"。"""
    for n in names:
        if row.get(n) not in (None, ""):
            return row[n]
    for mode in (lambda k, n: k.startswith(n), lambda k, n: n in k):
        cand = []
        for k, v in row.items():
            if v in (None, ""):
                continue
            ks = str(k)
            for n in names:
                if mode(ks, n):
                    cand.append((len(ks), v))
                    break
        if cand:
            return sorted(cand, key=lambda x: x[0])[0][1]
    return ""


def mx_candidates():
    """返回 (候选列表, 状态说明)。失败不抛异常，交由调用方降级。"""
    if MX_LIMITED.is_set():
        return [], "妙想已达日限额，本次运行内不再请求"

    def build():
        try:
            rows = mx_rows_of(mx_raw())
        except Exception as e:                          # noqa: BLE001
            # 失败不入缓存，否则一次网络抖动会把妙想锁死 15 分钟
            print("MX_FAIL", repr(e)[:150])
            return (None, "妙想不可用：%s" % str(e)[:120])
        out = []
        for r in rows:
            code = "".join(c for c in str(mx_pick(r, ["SECURITY_CODE", "股票代码", "证券代码", "代码"]))
                           if c.isdigit())
            name = str(mx_pick(r, ["SECURITY_SHORT_NAME", "SECURITY_NAME_ABBR",
                                   "股票简称", "证券简称", "名称"])).strip()
            if len(code) != 6 or not name:
                continue
            out.append({"code": code, "name": name,
                        "price": num(mx_pick(r, ["NEWEST_PRICE", "最新价", "现价", "收盘价"])),
                        "pct": num(mx_pick(r, ["CHG", "涨跌幅", "涨幅"])),
                        "turnover": num(mx_pick(r, ["TURNOVER_RATE", "换手率", "HSL"])),
                        "cap": yi(mx_pick(r, ["TOTAL_MARKET_CAP", "TOAL_MARKET_VALUE",
                                               "TOTAL_MARKET_VALUE", "总市值", "市值"])),
                        "industry": str(mx_pick(r, ["东财行业总分类", "RPT_F10_ORG_BASICINFO_BOARD_NAME",
                                                    "所属行业", "行业"])) or "",
                        "concepts": str(mx_pick(r, ["STYLE_CONCEPT", "概念", "题材"])) or "",
                        "liangbi": num(mx_pick(r, ["LIANGBI", "量比"])) or None})
        return (out, "妙想选股正常，返回 %d 只候选" % len(out))

    key = "mx_" + datetime.now(CN_TZ).strftime("%Y%m%d%H")
    ok, val = _cache_get(key)
    if ok:
        return val
    out, msg = build()
    if out is None:                                   # 失败不写缓存，下次刷新即重试
        return [], msg
    _cache_put(key, (out, msg), MX_CACHE_SECONDS)
    return out, msg


# ============================== 2. 交易日历（涨停池推导） ==============================

def _zt_probe(date):
    """轻量探测：返回 (tc, qdate)"""
    j = http_json(EM_ZT, {"ut": ZT_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 1,
                          "sort": "fbt:asc", "date": date}, timeout=10, tries=2)
    got = j.get("data") or {}
    return int(num(got.get("tc"))), str(got.get("qdate") or "")


def get_trading_dates(count=LOOKBACK):
    def build():
        today = datetime.now(CN_TZ)
        _, latest = _zt_probe(today.strftime("%Y%m%d"))
        if not latest or len(latest) != 8:
            raise RuntimeError("未取到最近交易日")
        dates, day = [], datetime.strptime(latest, "%Y%m%d").replace(tzinfo=CN_TZ)
        for _ in range(45):
            d = day.strftime("%Y%m%d")
            if day.weekday() >= 5:                      # 周末直接跳过，省请求
                day -= timedelta(days=1)
                continue
            try:
                tc, _ = _zt_probe(d)
                if tc > 0:
                    dates.append(d)
                    if len(dates) >= count:
                        break
            except Exception as e:                      # noqa: BLE001
                print("DATE_PROBE_ERR", d, repr(e)[:80])
            day -= timedelta(days=1)
        if not dates:
            raise RuntimeError("交易日历为空")
        print("TRADING_DATES", dates, "latest=%s" % latest)
        return dates, latest

    return cached("dates_" + datetime.now(CN_TZ).strftime("%Y%m%d"), DATE_TTL, build)


# ============================== 3. 涨停池 ==============================

def get_zt_pool(date, latest):
    def build():
        pool, page, tc = [], 0, None
        while True:
            got = (http_json(EM_ZT, {"ut": ZT_UT, "dpt": "wz.ztzt", "Pageindex": page,
                                     "pagesize": 200, "sort": "fbt:asc", "date": date},
                             timeout=15).get("data") or {})
            chunk = got.get("pool") or []
            pool.extend(chunk)
            tc = int(num(got.get("tc")))
            if not chunk or len(pool) >= tc or page > 25:
                break
            page += 1
        out = {}
        for it in pool:
            code = str(it.get("c", "")).zfill(6)
            zt = it.get("zttj") or {}
            out[code] = {
                "name": str(it.get("n", "")),
                "pct": round(num(it.get("zdp")), 2),
                "turnover": round(num(it.get("hs")), 2),
                "price": round(num(it.get("p")) / 1000, 2),          # p 是 元×1000
                "amount": yi(it.get("amount")),            # 成交额字段单位是元（实测 002058=51136320 即 0.51 亿）
                "cap_pool": yi(it.get("tshare")),                    # 总市值（30/68 开头常为 0）
                "float_cap": yi(it.get("ltsz")),
                "lianban": int(num(it.get("lbc"))),
                "days": int(num(zt.get("days"))),
                "boards": int(num(zt.get("ct"))),
                "seal_fund": yi(it.get("fund")),
                "first_seal": int(num(it.get("fbt"))),
                "last_seal": int(num(it.get("lbt"))),
                "open_times": int(num(it.get("zbc"))),
                "industry": str(it.get("hybk", "")),
            }
        print("ZT_POOL", date, "->", len(out))
        return out

    ttl = CACHE_SECONDS if date == latest else HIST_TTL
    return cached("zt_" + date, ttl, build)


# ============================== 4. 龙虎榜 ==============================

def get_lhb(date, latest):
    def build():
        rows, page = [], 1
        flt = "(TRADE_DATE='%s')" % d8_to_dashed(date)
        while page <= 8:
            res = dc({"reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
                      "columns": "ALL", "pageNumber": page, "pageSize": 500,
                      "sortColumns": "BILLBOARD_NET_AMT", "sortTypes": -1,
                      "source": "WEB", "client": "WEB", "filter": flt})
            chunk = res.get("data") or []
            rows.extend(chunk)
            if not chunk or page >= int(num(res.get("pages"), 1)):
                break
            page += 1
        out = {}
        for r in rows:
            code = str(r.get("SECURITY_CODE", "")).zfill(6)
            net = num(r.get("BILLBOARD_NET_AMT"))
            it = out.get(code)
            if it is None or abs(net) > abs(it["net_raw"]):
                it = {
                    "net_raw": net,
                    "buy_raw": num(r.get("BILLBOARD_BUY_AMT")),
                    "sell_raw": num(r.get("BILLBOARD_SELL_AMT")),
                    "deal_raw": num(r.get("BILLBOARD_DEAL_AMT")),
                    "deal_ratio": round(num(r.get("DEAL_AMOUNT_RATIO")), 2),
                    "float_cap": yi(r.get("FREE_MARKET_CAP")),
                    "close": round(num(r.get("CLOSE_PRICE")), 2),
                    "chg": round(num(r.get("CHANGE_RATE")), 2),
                    "turnover": round(num(r.get("TURNOVERRATE")), 2),
                    "market": str(r.get("TRADE_MARKET", "") or r.get("MARKET", "")),
                    "explain": str(r.get("EXPLAIN", "") or ""),
                    "reasons": set(),
                    "times": 0,
                }
                out[code] = it
            why = str(r.get("EXPLANATION", "") or "").strip()
            if why:
                it["reasons"].add(why)
            it["times"] += 1
        for it in out.values():
            it["net"] = round(it["net_raw"] / 1e8, 2)
            it["buy"] = round(it["buy_raw"] / 1e8, 2)
            it["sell"] = round(it["sell_raw"] / 1e8, 2)
            it["deal"] = round(it["deal_raw"] / 1e8, 2)
            m = re.search(r"(\d+)家机构(买入|卖出)", it["explain"])
            it["org_cnt"] = int(m.group(1)) if m else 0
            it["org_dir"] = m.group(2) if m else ""
            w = re.search(r"成功率([\d.]+)%", it["explain"])
            it["org_win"] = float(w.group(1)) if w else None
            it["reason"] = " / ".join(sorted(it["reasons"]))
            del it["reasons"]
        print("LHB", date, "->", len(out), "只 /", len(rows), "条")
        return out

    ttl = CACHE_SECONDS if date == latest else HIST_TTL
    return cached("lhb_" + date, ttl, build)


# ============================== 5. 席位明细（近 SEAT_LOOKBACK 日） ==============================

def seat_kind(name):
    n = str(name or "")
    if "机构专用" in n:
        return "org"
    if "股通" in n:                       # 沪股通专用 / 深股通专用
        return "north"
    return "hot"                          # 游资营业部


def get_seats(date, latest):
    def build():
        agg = {}
        flt = "(TRADE_DATE='%s')" % d8_to_dashed(date)
        for side, rep in (("buy", "RPT_BILLBOARD_DAILYDETAILSBUY"),
                          ("sell", "RPT_BILLBOARD_DAILYDETAILSSELL")):
            page = 1
            while page <= 12:
                res = dc({"reportName": rep, "columns": "ALL", "pageNumber": page,
                          "pageSize": 500, "source": "WEB", "client": "WEB", "filter": flt})
                chunk = res.get("data") or []
                for r in chunk:
                    code = str(r.get("SECURITY_CODE", "")).zfill(6)
                    name = str(r.get("OPERATEDEPT_NAME", "") or "")
                    kind = seat_kind(name)
                    it = agg.setdefault(code, {
                        "org": 0.0, "north": 0.0, "hot": 0.0,
                        "buy_total": 0.0, "sell_total": 0.0,
                        "top_buy": [], "top_sell": [], "org_win": [],
                        "top_buy_map": {}, "top_sell_map": {},
                    })
                    b, s = num(r.get("BUY")), num(r.get("SELL"))
                    it[kind] += b - s
                    it["buy_total"] += b
                    it["sell_total"] += s
                    if kind == "org" and r.get("RISE_PROBABILITY_3DAY") is not None:
                        it["org_win"].append(num(r.get("RISE_PROBABILITY_3DAY")))
                    tgt = "top_buy" if side == "buy" else "top_sell"
                    slot = it.setdefault(tgt + "_map", {})
                    cur = slot.setdefault(name, {"seat": name, "kind": kind, "amt": 0.0, "net": 0.0})
                    cur["amt"] += (b if side == "buy" else s) / 1e8
                    cur["net"] += (b - s) / 1e8
                if not chunk or page >= int(num(res.get("pages"), 1)):
                    break
                page += 1
        for code, it in agg.items():
            for side_key in ("top_buy", "top_sell"):
                lst = sorted(it.pop(side_key + "_map", {}).values(), key=lambda x: -x["amt"])
                for x in lst:
                    x["amt"] = round(x["amt"], 3)
                    x["net"] = round(x["net"], 3)
                it[side_key] = lst
            for k in ("org", "north", "hot", "buy_total", "sell_total"):
                it[k] = round(it[k] / 1e8, 3)
            it["net_total"] = round(it["buy_total"] - it["sell_total"], 3)
            it["top_buy"] = it["top_buy"][:5]
            it["top_sell"] = it["top_sell"][:5]
            it["org_win"] = round(sum(it["org_win"]) / len(it["org_win"]), 2) if it["org_win"] else None
        print("SEATS", date, "->", len(agg), "只")
        return agg

    ttl = CACHE_SECONDS if date == latest else HIST_TTL
    return cached("seats_" + date, ttl, build)


# ============================== 6. 全板块总市值 ==============================

def get_caps(codes, date):
    """④ RPT_VALUEANALYSIS_DET：含 30/68 开头的总市值，涨停池 tshare 对这些是 0"""
    cl = sorted(set(codes))
    if not cl:
        return {}
    tag = hashlib.md5((",".join(cl) + date).encode()).hexdigest()[:10]

    def build():
        out, chunk = {}, 60
        for i in range(0, len(cl), chunk):
            part = cl[i:i + chunk]
            inlist = ",".join('"%s"' % c for c in part)
            flts = ["(TRADE_DATE='%s')(SECURITY_CODE in (%s))" % (d8_to_dashed(date), inlist),
                    "(SECURITY_CODE in (%s))" % inlist]      # 兜底：不限日期，取每票最新一条
            for k, flt in enumerate(flts):
                page = 1
                got_any = False
                while page <= 8:
                    res = dc({"reportName": "RPT_VALUEANALYSIS_DET",
                              "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,TOTAL_MARKET_CAP,"
                                         "NOTLIMITED_MARKETCAP_A,CLOSE_PRICE,CHANGE_RATE,"
                                         "PE_TTM,PB_MRQ,BOARD_NAME,TRADE_DATE",
                              "pageNumber": page, "pageSize": 500,
                              "sortColumns": "TRADE_DATE" if k else "SECURITY_CODE",
                              "sortTypes": -1 if k else 1,
                              "source": "WEB", "client": "WEB", "filter": flt})
                    rows = res.get("data") or []
                    got_any = got_any or bool(rows)
                    for r in rows:
                        code = str(r.get("SECURITY_CODE", "")).zfill(6)
                        if k and code in out:          # 兜底模式：已取到目标日则不覆盖
                            continue
                        out[code] = {"cap": yi(r.get("TOTAL_MARKET_CAP")),
                                     "float_cap": yi(r.get("NOTLIMITED_MARKETCAP_A")),
                                     "close": round(num(r.get("CLOSE_PRICE")), 2),
                                     "chg": round(num(r.get("CHANGE_RATE")), 2),
                                     "pe": round(num(r.get("PE_TTM")), 2) or None,
                                     "pb": round(num(r.get("PB_MRQ")), 2) or None,
                                     "board": str(r.get("BOARD_NAME", "") or "")}
                    if not rows or page >= int(num(res.get("pages"), 1)):
                        break
                    page += 1
                if got_any:
                    break
        print("CAPS", date, "->", len(out))
        return out

    # 只按最新交易日取市值（盘中会变），故统一用短缓存
    return cached("caps_" + tag, CACHE_SECONDS, build)


# ============================== 7. 指标与评分 ==============================

def seal_strength(p):
    """东财代理指标：首封时间越早 + 炸板越少 + 封单/流通市值比越高 -> 越强 (0~1)"""
    fbt = p.get("first_seal") or 0
    if fbt <= 0:
        t_score = 0.30
    else:
        h, m = divmod(fbt // 100, 100)
        mins = (h * 60 + m) - (9 * 60 + 25)
        t_score = clamp(1.0 - clamp(mins, 0, 215) / 215, 0, 1)
    zbc = p.get("open_times") or 0
    z_score = 1.0 if zbc == 0 else clamp(1.0 - zbc * 0.28, 0, 1)
    fc = p.get("float_cap") or 0
    sf = p.get("seal_fund") or 0
    f_score = clamp((sf / fc) / 0.02, 0, 1) if fc > 0 else 0.0
    return round(0.4 * t_score + 0.3 * z_score + 0.3 * f_score, 4)


def tier_of(n):
    if n >= 4:
        return "4板及以上"
    if n == 3:
        return "3板"
    if n == 2:
        return "2板"
    return "首板"


def score_row(p):
    """总分 0~100，各维度见文件头注释"""
    lim = limit_pct(p["code"])
    s_pct = clamp(p["pct"], 0, lim) / lim * 15

    t = p["turnover"]
    s_turn = clamp(t, 0, 20) / 20 * 13.5 if t > 0 else 0.0
    if t > 25:
        s_turn -= min(4.0, (t - 25) * 0.3)
    s_turn = clamp(s_turn, 0, 15)

    ladder = max(p["zt_days"], p["lianban"], p["boards"])
    s_ladder = min(ladder, 4) / 4 * 20

    net = p["lhb_net"]
    if net > 0:
        s_net = min(net / 3.0, 1.0) * 20
    elif net < 0:
        s_net = -min(abs(net) / 3.0, 1.0) * 5 + 4       # 净卖出：给基础分但倒扣
        s_net = clamp(s_net, 0, 20)
    else:
        s_net = 0.0
    s_seal = seal_strength(p) * 20

    org = p.get("org_net") or 0
    s_org = min(max(org, 0) / 1.0, 1.0) * 8
    win = p.get("seat_org_win")
    if win is not None:
        s_org += clamp((win - 35.0) / 15.0, -1, 1) * 2
    s_org = clamp(s_org, 0, 10)

    detail = {"涨幅": round(s_pct, 1), "换手": round(s_turn, 1), "连板梯队": round(s_ladder, 1),
              "龙虎榜资金": round(s_net, 1), "封板强弱": round(s_seal, 1), "机构席位": round(s_org, 1)}
    return round(clamp(sum(detail.values()), 0, 100), 1), detail


# ============================== 8. 组装 ==============================

def _board_kind(fbt, zbc, turnover):
    """板型：一字板(9:25即封且几乎无换手) / T字板(盘中回封) / 换手板。纯本地推断，不引入新数据源。"""
    if fbt and fbt <= 92600 and (turnover or 0) < 2:
        return "一字板"
    if (zbc or 0) > 0:
        return "T字板"
    return "换手板"


def build_radar():
    dates, latest = get_trading_dates(LOOKBACK)
    pools = {d: get_zt_pool(d, latest) for d in dates}
    lhbs = {d: get_lhb(d, latest) for d in dates}
    seats = {d: get_seats(d, latest) for d in dates[:SEAT_LOOKBACK]}

    mx_list, mx_msg = mx_candidates()
    mx_ok = bool(mx_list)
    cands = {}
    for c in mx_list:
        cands.setdefault(c["code"], {"code": c["code"], "name": c["name"],
                                     "price": c["price"], "pct": c["pct"],
                                     "turnover": c["turnover"], "cap_mx": c["cap"],
                                     "industry_mx": c.get("industry", ""),
                                     "concepts": c.get("concepts", "")})
    for d in dates:                                  # 涨停池兜底（同时保证硬条件可判）
        for code, it in pools[d].items():
            e = cands.setdefault(code, {"code": code})
            e.setdefault("name", it["name"])
            e.setdefault("price", it["price"])
            e.setdefault("pct", it["pct"])
            e.setdefault("turnover", it["turnover"])

    caps = get_caps(list(cands), latest)
    quotes = get_quotes(list(cands), latest)          # 最新交易日统一行情（妙想）
    quote_cover = round(len(quotes) / len(cands) * 100, 1) if cands else 0.0

    # 晋级率：上一交易日涨停的票里，本日继续涨停的比例
    promo = {}
    for i in range(len(dates) - 1):
        cur, prev = pools[dates[i]], pools[dates[i + 1]]
        if prev:
            promo["%s→%s" % (dates[i + 1][4:], dates[i][4:])] = round(
                len(set(cur) & set(prev)) / len(prev) * 100, 1)

    rows = []
    for code, c in cands.items():
        name = str(c.get("name") or "").strip()
        if len(code) != 6 or not name:
            continue
        if "ST" in name.upper() or name.startswith("退"):
            continue

        zt_days, best, best_date = 0, None, None
        for d in dates:
            it = pools[d].get(code)
            if it:
                zt_days += 1
                if best is None:
                    best, best_date = it, d
        src = best or {}

        lhb = None
        for d in dates:
            if code in lhbs[d]:
                cur = lhbs[d][code]
                if lhb is None or abs(cur["net"]) > abs(lhb["net"]):
                    lhb = cur
                    lhb["_d"] = d
        seat = None
        for d in dates[:SEAT_LOOKBACK]:
            cur = seats[d].get(code)
            if cur and (seat is None or abs(cur["net_total"]) > abs(seat["net_total"])):
                seat = cur
                seat["_d"] = d

        capinfo = caps.get(code, {})
        q = quotes.get(code, {})
        cap = q.get("cap") or capinfo.get("cap") or src.get("cap_pool") or c.get("cap_mx")
        float_cap = (q.get("float_cap") or capinfo.get("float_cap") or src.get("float_cap")
                     or (lhb or {}).get("float_cap"))
        # 行情口径：优先最新交易日（妙想报价 > 估值表），涨停池值只在取不到时兜底并标注日期
        px = q.get("price") or capinfo.get("close") or src.get("price") or c.get("price") or 0
        pct = q.get("pct") if q.get("pct") else (capinfo.get("chg") or src.get("pct", c.get("pct", 0)))
        if not q.get("pct") and not capinfo.get("chg"):
            pct = src.get("pct", c.get("pct", 0))
        # 换手率：只认妙想最新交易日值；取不到才退回涨停池当日值，并记下来自哪天
        if q.get("turnover"):
            to, to_date = q["turnover"], latest
        else:
            to, to_date = src.get("turnover", c.get("turnover", 0)), best_date

        p = {
            "code": code, "name": name,
            "price": px, "pct": pct, "turnover": to,
            "cap": cap, "float_cap": float_cap,
            "quote_date": latest, "turnover_date": to_date,
            "limit_up_date": best_date,
            "pe": capinfo.get("pe"), "pb": capinfo.get("pb"),
            "industry": src.get("industry") or c.get("industry_mx") or capinfo.get("board", ""),
            "concepts": (c.get("concepts") or "")[:120],
            "lianban": src.get("lianban", 0), "days": src.get("days", 0),
            "boards": src.get("boards", 0), "seal_fund": src.get("seal_fund"),
            "first_seal": src.get("first_seal", 0), "open_times": src.get("open_times", 0),
            "zt_days": zt_days, "seal_date": dates[0],
            "amount": src.get("amount"),
            "board_kind": _board_kind(src.get("first_seal", 0), src.get("open_times", 0),
                                     src.get("turnover", c.get("turnover", 0))),
            "limit_up_today": (best_date == latest),
            "on_board": bool(lhb), "lhb_net": (lhb or {}).get("net", 0.0),
            "lhb_buy": (lhb or {}).get("buy"), "lhb_sell": (lhb or {}).get("sell"),
            "lhb_deal_ratio": (lhb or {}).get("deal_ratio"),
            "lhb_reason": (lhb or {}).get("reason", ""),
            "lhb_explain": (lhb or {}).get("explain", ""),
            "lhb_date": (lhb or {}).get("_d"),
            "org_cnt": (lhb or {}).get("org_cnt", 0),
            "org_dir": (lhb or {}).get("org_dir", ""),
            "org_net": (seat or {}).get("org"), "north_net": (seat or {}).get("north"),
            "hot_net": (seat or {}).get("hot"),
            "top_buy": (seat or {}).get("top_buy", []), "top_sell": (seat or {}).get("top_sell", []),
            "seat_org_win": (seat or {}).get("org_win"),
            "seat_date": (seat or {}).get("_d"),
        }
        p["tier"] = tier_of(max(p["zt_days"], p["lianban"], p["boards"]))

        hard = {"zt": p["zt_days"] > 0, "lhb": p["on_board"],
                "cap": None if not p["cap"] else p["cap"] <= MAX_CAP_YI}
        p["hard_met"] = sum(1 for v in hard.values() if v is True)
        p["hard_total"] = sum(1 for v in hard.values() if v is not None)
        p["hard_full"] = p["hard_met"] == 3
        p["score"], p["score_detail"] = score_row(p)
        p["seal_score"] = round(seal_strength(p) * 100)
        rows.append(p)

    rows.sort(key=lambda x: (x["hard_met"], x["score"]), reverse=True)

    tiers = {}
    for r in rows:
        if r["zt_days"] <= 0:
            continue
        t = tiers.setdefault(r["tier"], {"n": 0, "sum": 0.0, "best": None})
        t["n"] += 1
        t["sum"] += r["score"]
        mb = max(r["lianban"], r["boards"], 1)
        if not t["best"] or mb > t["best"]["mb"]:
            t["best"] = {"code": r["code"], "name": r["name"], "mb": mb}
    for t in tiers.values():
        t["avg"] = round(t["sum"] / t["n"], 1) if t["n"] else 0
        t.pop("sum", None)

    today = datetime.now(CN_TZ).strftime("%Y%m%d")
    lhb_pending = (today in dates) and (datetime.now(CN_TZ).hour < LHB_PUBLISH_HOUR) \
        and not lhbs.get(today)

    meta = {
        "dates": dates, "latest": latest, "total": len(rows),
        "passed": sum(1 for r in rows if r["hard_full"]),
        "generated": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "mx_ok": mx_ok, "mx_msg": mx_msg, "promo": promo,
        "lhb_pending": lhb_pending,
        "industries": _industry_stat(rows),
        "today_limit_up": sum(1 for r in rows if r["limit_up_today"]),
        "quote_cover": quote_cover,
    }
    return rows, tiers, meta


def _industry_stat(rows, top=8):
    agg = {}
    for r in rows:
        if r["zt_days"] <= 0:
            continue
        k = r["industry"] or "其他"
        a = agg.setdefault(k, {"n": 0, "score": 0.0})
        a["n"] += 1
        a["score"] += r["score"]
    out = [{"industry": k, "n": v["n"], "avg": round(v["score"] / v["n"], 1)}
           for k, v in agg.items()]
    out.sort(key=lambda x: (-x["n"], -x["avg"]))
    return out[:top]


# ============================== 9. 快照兜底 + 后台刷新 ==============================

def save_snapshot(rows, tiers, meta):
    try:
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SNAP_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"rows": rows, "tiers": tiers, "meta": meta,
                       "saved_at": datetime.now(CN_TZ).isoformat()}, f, ensure_ascii=False)
        os.replace(tmp, SNAP_FILE)
    except Exception as e:                          # noqa: BLE001
        print("SNAP_SAVE_ERR", repr(e)[:120])


def load_snapshot():
    try:
        with open(SNAP_FILE, encoding="utf-8") as f:
            d = json.load(f)
        print("SNAP_LOADED", d["meta"].get("latest"), len(d["rows"]))
        return d
    except Exception:                               # noqa: BLE001
        return None


def do_refresh(force=False):
    now = time.time()
    with _lock:
        fresh = _STATE["ts"] and (now - _STATE["ts"] < CACHE_SECONDS) and not force
        # 失败后 15 秒内不再重试，避免接口全挂时每个页面请求都猛打一遍上游
        throttled = now - _STATE["last_try"] < 15
        if fresh or throttled or _STATE["refreshing"]:
            return False
        _STATE["refreshing"] = True
        _STATE["last_try"] = now
    try:
        rows, tiers, meta = build_radar()
        with _lock:
            _STATE.update({"ts": time.time(), "data": rows, "tiers": tiers,
                           "meta": meta, "source": "妙想选股 + 东方财富公开数据",
                           "error": None})
        save_snapshot(rows, tiers, meta)
        print("REFRESH_OK", meta["latest"], meta["total"], "只")
    except Exception as e:                          # noqa: BLE001
        err = "%s: %s" % (type(e).__name__, str(e)[:200])
        print("REFRESH_ERR", err)
        with _lock:
            _STATE["error"] = err
            if not _STATE["ts"]:
                snap = load_snapshot()
                if snap:
                    _STATE.update({"ts": 0.0, "data": snap["rows"], "tiers": snap["tiers"],
                                   "meta": snap["meta"], "source": "内置快照（上次成功收盘数据）"})
    finally:
        with _lock:
            _STATE["refreshing"] = False


def refresher():
    while True:
        do_refresh(force=True)
        time.sleep(CACHE_SECONDS)


threading.Thread(target=refresher, daemon=True).start()


def view():
    with _lock:
        st = dict(_STATE)
    if not st["data"]:
        do_refresh()                        # 冷启动：后台线程没跑起来时同步补一次
        with _lock:
            st = dict(_STATE)
    if not st["data"]:
        snap = load_snapshot()              # 仍为空：落到内置快照，保证页面不空白
        if snap:
            st.update({"data": snap["rows"], "tiers": snap["tiers"], "meta": snap["meta"],
                       "source": "内置快照（上次成功收盘数据）"})
    return st


# ============================== 10. 路由 ==============================

@app.get("/api/radar")
def api_radar():
    st = view()
    keep = ("code", "name", "price", "pct", "turnover", "cap", "float_cap", "pe", "pb",
            "industry", "tier", "lianban", "days", "boards", "zt_days", "seal_fund",
            "first_seal", "open_times", "seal_score", "amount", "board_kind", "on_board", "lhb_net", "lhb_buy",
            "quote_date", "limit_up_date", "turnover_date", "limit_up_today",
            "lhb_sell", "lhb_deal_ratio", "lhb_reason", "lhb_explain", "lhb_date",
            "org_cnt", "org_dir", "org_net", "north_net", "hot_net", "seat_org_win",
            "concepts",
            "top_buy", "top_sell", "seat_date", "score", "score_detail",
            "hard_met", "hard_total", "hard_full")
    out = [{k: r[k] for k in keep if k in r} for r in st["data"][:80]]
    return JSONResponse({"generated": st["meta"].get("generated"),
                         "dates": st["meta"].get("dates"), "latest": st["meta"].get("latest"),
                         "source": st["source"], "error": st["error"],
                         "refreshing": st["refreshing"], "total": st["meta"].get("total"),
                         "passed": st["meta"].get("passed"), "quote_cover": st["meta"].get("quote_cover"),
                         "today_limit_up": st["meta"].get("today_limit_up"), "mx_ok": st["meta"].get("mx_ok"),
                         "mx_msg": st["meta"].get("mx_msg"), "promo": st["meta"].get("promo"),
                         "lhb_pending": st["meta"].get("lhb_pending"),
                         "industries": st["meta"].get("industries"),
                         "tiers": st["tiers"], "list": out})


@app.get("/api/health")
def api_health():
    with _lock:
        return {"ts": _STATE["ts"], "rows": len(_STATE["data"]),
               "latest": _STATE["meta"].get("latest"), "error": _STATE["error"],
               "refreshing": _STATE["refreshing"], "snapshot": SNAP_FILE.exists()}


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


# ============================== 12. 策略选股（善水模型） ==============================
#
# 模板来自用户提供的善水科技(301190) 2026-09-30 截图实测值：
#   涨幅20.01%(20cm涨停) / 换手7.95%(实换手16.71%) / 量比1.25 / 总市值77.12亿
#   流通市值65.34亿 / 5日涨幅75.61% / 最高价35.93创52周新高 / MA5 26.60>MA10 22.92>MA20 20.65
#   主力净流入3210万、5日净流入1.21亿
# 实现思路：6 条全满足查一次得"严格命中"；每次去掉一条各查一次，
#           该结果减去严格命中即"只差这一条"的接近信号，天然给出"差的那一条"。
# 依赖：均线/MACD/资金流/创52周新高这些字段只有妙想能按条件过滤
#      （东方财富的 K 线接口 push2his 在本运行环境被服务端断连），故此模块必须有 MX_APIKEY。

def mx_field(row, names):
    """按候选列名从妙想一行里取值（内部走 mx_pick：精确 > 前缀 > 包含）"""
    return mx_pick(row, names)


def _pick_main(row, want_sum=True):
    """主力净额有两列：当日「主力净额(不含暗盘)(元)」与区间「主力净额合计{...|...}」，
    标题只差「合计」两字，必须显式区分，否则会把当日值当成 5 日合计。"""
    best = ''
    for k, v in row.items():
        ks = str(k)
        if '主力净额' not in ks or v in (None, ''):
            continue
        if (('合计' in ks) or ('|' in ks)) == want_sum:
            if not best or len(ks) < len(best):
                best = ks
    return row.get(best, '') if best else ''


def norm_row(row):
    """把妙想一行压成策略页要用的字段；num/yi 已能解析「3.24亿」「1446.61万」这类中文单位"""
    code = ''.join(c for c in str(mx_field(row, ['SECURITY_CODE', '股票代码', '代码', '证券代码']))
                   if c.isdigit())
    name = str(mx_field(row, ['SECURITY_SHORT_NAME', 'SECURITY_NAME_ABBR', '股票简称', '名称',
                             'CUSTOM_FORMAT_STOCK_NAME'])).strip()
    if len(code) != 6 or not name:
        return None
    return {
        'code': code, 'name': name,
        'price': round(num(mx_field(row, ['NEWEST_PRICE', '最新价', '收盘价', '现价'])), 2),
        'pct': round(num(mx_field(row, ['CHG', '涨跌幅', '涨幅'])), 2),
        'turnover': round(num(mx_field(row, ['TURNOVER_RATE', '换手率', 'HSL'])), 2),
        'liangbi': round(num(mx_field(row, ['LIANGBI', '量比'])), 2),
        'cap': yi(mx_field(row, ['TOAL_MARKET_VALUE', 'TOTAL_MARKET_CAP', '总市值', '市值'])),
        'float_cap': yi(mx_field(row, ['CIRCULATION_MARKET_VALUE', '流通市值'])),
        'ma5': round(num(mx_field(row, ['5日均线', 'MA5'])), 2) or None,
        'ma10': round(num(mx_field(row, ['10日均线', 'MA10'])), 2) or None,
        'ma20': round(num(mx_field(row, ['20日均线', 'MA20'])), 2) or None,
        'main_net': yi(_pick_main(row, want_sum=False)),
        'main_net_5d': yi(_pick_main(row, want_sum=True)),
        'chg5d': round(num(mx_field(row, ['5日区间涨跌幅', '区间涨跌幅', 'INTERVAL_CHG', '5日涨幅'])), 2),
        'peak': round(num(mx_field(row, ['最高价', '52周最高价', '区间最高价'])), 2) or None,
        'concepts': str(mx_field(row, ['STYLE_CONCEPT', '概念', '题材', '个股题材']))[:80],
    }


STRATEGY_NAME = "善水策略"
STRAT_MAX = 200                        # 单次问句要求返回的只数上限
COND_KEYS = ["C1涨停确认", "C2均线多头", "C3抛压衰竭", "C4量能与换手", "C5小市值", "C6阶段强势创新高"]

COND_DESC = {
    "C1涨停确认": "最近一个交易日涨停（主板10%／创业科创20%，由妙想按涨停过滤）",
    "C2均线多头": "5日均线 > 10日均线 > 20日均线（善水科技当日 26.60 > 22.92 > 20.65）",
    "C3抛压衰竭": "当日主力净额 > 0 且 近5个交易日主力净额合计 > 0（对应参考图的「D-1还有资金流出」）",
    "C4量能与换手": "换手率 6%~25% 且 量比 1~3（善水科技 7.95% / 1.25）",
    "C5小市值": "总市值 <= 100 亿 且 流通市值 <= 80 亿（善水科技 77.12 / 65.34）",
    "C6阶段强势创新高": "5日区间涨幅 > 50% 且 最高价创52周新高（善水科技 75.61%）",
}

MISS_WHY = {
    "C1涨停确认": lambda r: "未涨停",
    "C2均线多头": lambda r: "均线未多头（MA5 %s / MA10 %s / MA20 %s）" % (
        _sv(r["ma5"]), _sv(r["ma10"]), _sv(r["ma20"])),
    "C3抛压衰竭": lambda r: "D-1还有资金流出（主力净额 %s，5日合计 %s）" % (
        _sv(r["main_net"], "亿"), _sv(r["main_net_5d"], "亿")),
    "C4量能与换手": lambda r: "量能不符（换手 %s%%，量比 %s）" % (_sv(r["turnover"]), _sv(r["liangbi"])),
    "C5小市值": lambda r: "市值超区间（总市值 %s亿，流通 %s亿）" % (_sv(r["cap"]), _sv(r["float_cap"])),
    "C6阶段强势创新高": lambda r: "强度或新高不够（5日涨幅 %s%%，最高价 %s，52周新高 %s）" % (
        _sv(r["chg5d"]), _sv(r["peak"]), "是" if r.get("new_high_52w") else "否"),
}


def mx_post(q, tries=3):
    """带频控退避的妙想查询：status=112(频率过高) 按 8s/16s 退避重试，113(日限额) 直接降级。"""
    key = os.getenv("MX_APIKEY")
    if not key:
        raise RuntimeError("策略选股依赖妙想的均线／资金流／新高字段，请先配置 MX_APIKEY")
    last = None
    for i in range(tries):
        obj = http_json(MX_URL, payload={"keyword": q}, timeout=60, tries=1,
                        headers={**MX_HEADERS, "apikey": key}, method="POST")
        st = obj.get("status")
        if st == 0:
            biz = (obj.get("data") or {}).get("code")
            if biz in (113, "113"):
                MX_LIMITED.set()
                raise RuntimeError("妙想日调用次数已达上限(code=113)")
            return mx_rows_of(obj)
        msg = str(obj.get("message") or "")
        last = RuntimeError("妙想 status=%s %s" % (st, msg[:80]))
        if "频率" in msg or st in (112, "112"):
            wait = 8 * (i + 1)
            print("MX_RATE_LIMIT 第%d次，退避 %ds" % (i + 1, wait))
            time.sleep(wait)
            continue
        raise last
    raise last


def _sv(v, suf=""):
    return "--" if v in (None, "", 0) else "%s%s" % (v, suf)


def _rows_of(q):
    out = []
    for r in mx_post(q):
        n = norm_row(r)
        if n:
            out.append(n)
    return out


# 阈值全部可调：DEFAULT_TH 同时驱动「妙想问句」与「本地判定」，两边口径永远一致
DEFAULT_TH = {
    "turn_lo": 6.0, "turn_hi": 25.0,        # C4 换手率区间 (%)
    "lb_lo": 1.0, "lb_hi": 3.0,             # C4 量比区间
    "cap_max": 100.0, "float_max": 80.0,    # C5 总市值/流通市值上限 (亿)
    "chg5d_min": 50.0,                      # C6 5日区间涨幅下限 (%)
    "use_ma": True,                         # C2 均线多头开关
    "use_money": True,                      # C3 主力净额>0 开关
    "use_newhigh": True,                    # C6 创52周新高开关
}
TH_LABELS = {
    "turn_lo": "换手率下限(%)", "turn_hi": "换手率上限(%)",
    "lb_lo": "量比下限", "lb_hi": "量比上限",
    "cap_max": "总市值上限(亿)", "float_max": "流通市值上限(亿)",
    "chg5d_min": "5日涨幅下限(%)",
}


def clean_th(raw):
    """把请求里传来的阈值清洗成合法字典；越界或非法值回落到默认值"""
    th = dict(DEFAULT_TH)
    for k in ("turn_lo", "turn_hi", "lb_lo", "lb_hi", "cap_max", "float_max", "chg5d_min"):
        v = raw.get(k)
        if v in (None, ""):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f >= 0:
            th[k] = f
    for k in ("use_ma", "use_money", "use_newhigh"):
        v = raw.get(k)
        if v is not None:
            th[k] = str(v) in ("1", "true", "True", "on", "yes")
    if th["turn_lo"] > th["turn_hi"]:
        th["turn_lo"], th["turn_hi"] = th["turn_hi"], th["turn_lo"]
    if th["lb_lo"] > th["lb_hi"]:
        th["lb_lo"], th["lb_hi"] = th["lb_hi"], th["lb_lo"]
    return th


def th_key(th):
    return "|".join("%s=%s" % (k, th[k]) for k in sorted(th))


def q_pool(th):
    """结构池问句：只把便宜且当日不变的条件交给妙想过滤，指标列一并取回"""
    extra = []
    if th["cap_max"] < 5000:
        extra.append("总市值小于等于%s亿元" % _n(th["cap_max"]))
    if th["float_max"] < 5000:
        extra.append("流通市值小于等于%s亿元" % _n(th["float_max"]))
    if th["turn_hi"] < 100:
        extra.append("换手率大于等于%s%%且小于等于%s%%" % (_n(th["turn_lo"]), _n(th["turn_hi"])))
    if th["lb_hi"] < 50:
        extra.append("量比大于等于%s且小于等于%s" % (_n(th["lb_lo"]), _n(th["lb_hi"])))
    tail = ("，同时满足" + "、".join(extra)) if extra else ""
    return ("筛选最近一个交易日涨停的A股%s，排除ST股票、*ST股票和退市股票，返回不超过%d只，"
            "包含股票代码、股票简称、最新价、涨跌幅、换手率、量比、总市值、流通市值、"
            "5日均线、10日均线、20日均线、主力净额、5日主力净额合计、5日区间涨跌幅、最高价、概念题材"
            % (tail, STRAT_MAX))


def q_newhigh():
    """52周新高只能当过滤条件、取不回字段值，所以单独确认一次拿集合"""
    return ("筛选最近一个交易日涨停且最高价创52周新高的A股，排除ST股票、*ST股票和退市股票，"
            "返回不超过%d只，包含股票代码、股票简称、涨跌幅、最高价" % STRAT_MAX)


def _n(v):
    return str(int(v)) if float(v) == int(float(v)) else str(v)


def get_quotes(codes, date):
    """统一取【最新交易日】行情：妙想按代码清单取 最新价/涨跌幅/换手率/市值。
    涨停池记录的是各自涨停当天的值，跨日会串口径，所以行情一律以此处为准。"""
    cl = sorted(set(codes))
    if not cl:
        return {}
    tag = hashlib.md5((",".join(cl) + date).encode()).hexdigest()[:10]

    def build():
        out = {}
        for i in range(0, len(cl), 40):
            part = cl[i:i + 40]
            q = ("筛选股票代码在(%s)范围内的A股，返回这些股票，"
                 "包含股票代码、股票简称、最新价、涨跌幅、换手率、总市值、流通市值"
                 % ",".join('"%s"' % x for x in part))
            got = 0
            for attempt in (1, 2):                  # 偶发空响应：整片重试一次
                try:
                    rows = mx_post(q)
                except Exception as e:              # noqa: BLE001
                    print("QUOTES_PART_FAIL", str(e)[:100])
                    time.sleep(6)
                    continue
                for row in rows:
                    n = norm_row(row)
                    if n:
                        out[n["code"]] = {"price": n["price"], "pct": n["pct"],
                                          "turnover": n["turnover"], "cap": n["cap"],
                                          "float_cap": n["float_cap"]}
                        got += 1
                if got:
                    break
                print("QUOTES_EMPTY 第%d次，重试本片(%d只)" % (attempt, len(part)))
                time.sleep(8)
            time.sleep(3)
        print("QUOTES", date, "->", len(out), "/", len(cl))
        return out

    return cached("q_" + tag, CACHE_SECONDS, build)


def evaluate(r, nh_codes, th):
    """本地逐条判定 6 个条件，阈值来自 th（与问句同源）"""
    ma5, ma10, ma20 = r["ma5"], r["ma10"], r["ma20"]
    return {
        "C1涨停确认": True,                       # 结构池本身就是按涨停筛出来的
        "C2均线多头": (not th["use_ma"]) or bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20),
        "C3抛压衰竭": (not th["use_money"]) or bool((r["main_net"] or 0) > 0 and (r["main_net_5d"] or 0) > 0),
        "C4量能与换手": th["turn_lo"] <= (r["turnover"] or 0) <= th["turn_hi"]
                        and th["lb_lo"] <= (r["liangbi"] or 0) <= th["lb_hi"],
        "C5小市值": bool(r["cap"] and r["cap"] <= th["cap_max"]
                         and (r["float_cap"] or 9e9) <= th["float_max"]),
        "C6阶段强势创新高": (r["chg5d"] or 0) > th["chg5d_min"]
                             and ((not th["use_newhigh"]) or r["code"] in nh_codes),
    }


def judge(pool, nh_codes, th):
    """对结构池逐条判定，产出严格命中 / 只差1条 / 各条件通过数"""
    strict, near, counts = [], [], {k: 0 for k in COND_KEYS}
    for r in pool:
        res = evaluate(r, nh_codes, th)
        miss = [k for k, v in res.items() if not v]
        for k, v in res.items():
            if v:
                counts[k] += 1
        if not miss:
            strict.append(r)
        elif len(miss) == 1:
            item = dict(r)
            item["miss"] = miss[0]
            item["miss_reason"] = MISS_WHY[miss[0]](r)
            near.append(item)
    strict.sort(key=lambda x: (-x["pct"], x["code"]))
    near.sort(key=lambda x: (-x["pct"], x["code"]))
    return strict, near, counts


def build_strategy(th):
    pool = _rows_of(q_pool(th))
    time.sleep(6)                                  # 频控友好：两次查询之间留间隔
    nh_codes = {r["code"] for r in _rows_of(q_newhigh())}
    for r in pool:
        r["new_high_52w"] = r["code"] in nh_codes
    strict, near, counts = judge(pool, nh_codes, th)
    return strict, near, counts, pool, nh_codes


def strategy_view(force=False, raw_th=None):
    th = clean_th(raw_th or {})
    now = time.time()
    key = "strategy_" + datetime.now(CN_TZ).strftime("%Y%m%d%H") + "_" + hashlib.md5(
        th_key(th).encode()).hexdigest()[:8]
    if not force:
        ok, val = _cache_get(key)
        if ok:
            return val
    strict, near, counts, pool, nh_codes = build_strategy(th)
    val = {
        "name": STRATEGY_NAME,
        "generated": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "conds": COND_KEYS, "cond_desc": COND_DESC,
        "strict": strict, "near": near, "cond_count": counts,
        "pool": len(pool), "rows": pool, "new_high": len(nh_codes),
        "th": th, "th_labels": TH_LABELS, "th_default": DEFAULT_TH,
        "note": ("严格命中=6条全满足；最接近信号=只差1条。判定全部本地完成，"
                 "拖动滑块即时重算、不重新请求妙想；但放宽阈值只会让名单变多、不会补回被"
                 "妙想过滤掉的票，需要放宽时点「按当前阈值重新取数」（会重跑 2 次妙想查询）。"
                 "胜率需逐日历史回测，东方财富 K 线接口在本环境不可用，故未计算。"),
    }
    _cache_put(key, val, CACHE_SECONDS * 10, now)
    return val


@app.get("/api/strategy")
def api_strategy(refresh: int = 0, turn_lo: float = None, turn_hi: float = None,
                 lb_lo: float = None, lb_hi: float = None, cap_max: float = None,
                 float_max: float = None, chg5d_min: float = None,
                 use_ma: str = None, use_money: str = None, use_newhigh: str = None):
    raw = {k: v for k, v in dict(turn_lo=turn_lo, turn_hi=turn_hi, lb_lo=lb_lo, lb_hi=lb_hi,
                                 cap_max=cap_max, float_max=float_max, chg5d_min=chg5d_min,
                                 use_ma=use_ma, use_money=use_money,
                                 use_newhigh=use_newhigh).items() if v is not None}
    try:
        return JSONResponse(strategy_view(force=bool(refresh), raw_th=raw))
    except Exception as e:                          # noqa: BLE001
        return JSONResponse({"error": "%s: %s" % (type(e).__name__, str(e)[:200]),
                             "strict": [], "near": [], "rows": []}, status_code=200)


@app.get("/strategy", response_class=HTMLResponse)
def page_strategy():
    return STRAT_PAGE


# ============================== 11. 前端 ==============================

PAGE = r"""
<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>妖股雷达 · A股短线打板工作台</title>
<style>
:root{
  --bg:#06070a;--bg2:#0b0d12;--card:#11141b;--card2:#151923;--line:#212736;--line2:#2b3346;
  --tx:#e8edf6;--tx2:#aab4c6;--sub:#76839a;--up:#f2554f;--dn:#2fa46a;--gold:#dcae5d;--gold2:#8a6a31;
  --blue:#5aa2e0;--radius:12px;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{background:var(--bg);color:var(--tx);font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  padding-bottom:56px;background-image:radial-gradient(1200px 400px at 50% -180px,rgba(220,174,93,.07),transparent)}
.wrap{max-width:1440px;margin:0 auto;padding:0 14px}
a{color:var(--blue);text-decoration:none}
button,select,input,textarea{font-family:inherit;font-size:12.5px;color:var(--tx);background:var(--card2);
  border:1px solid var(--line);border-radius:8px;padding:6px 10px;outline:none}
button{cursor:pointer;transition:.14s}
button:hover{border-color:var(--line2);background:#1a2030}
button.on{background:rgba(220,174,93,.16);border-color:rgba(220,174,93,.5);color:var(--gold)}
input:focus,select:focus,textarea:focus{border-color:var(--gold2)}
header{position:sticky;top:0;z-index:40;background:rgba(6,7,10,.88);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
.hd{display:flex;align-items:center;gap:10px;padding:10px 0;flex-wrap:wrap}
.logo{display:flex;align-items:center;gap:8px;font-size:16px;font-weight:700;letter-spacing:.3px}
.logo .mk{width:22px;height:22px;border-radius:6px;background:linear-gradient(140deg,var(--gold),#7a5a24);
  display:grid;place-items:center;color:#161006;font-size:12px;font-weight:900}
.logo small{font-weight:400;color:var(--sub);font-size:11px;margin-left:2px}
.spacer{flex:1 1 auto}
.meta{font-size:11.5px;color:var(--sub);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.dotk{width:6px;height:6px;border-radius:50%;background:var(--dn);display:inline-block;margin-right:5px}
.dotk.warn{background:var(--gold);animation:pl 1.1s infinite}
@keyframes pl{50%{opacity:.35}}
.tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--line);border-radius:9px;padding:3px}
.tabs button{border:0;background:transparent;padding:4px 11px;border-radius:6px;color:var(--tx2)}
.tabs button.on{background:rgba(220,174,93,.18);color:var(--gold)}
nav.links{display:flex;gap:14px;font-size:12px;padding:0 0 9px}
nav.links a{color:var(--tx2)}nav.links a.on{color:var(--gold)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(124px,1fr));gap:8px;margin:12px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:9px 11px}
.kpi b{display:block;font-size:19px;font-weight:800;color:var(--gold);font-variant-numeric:tabular-nums;line-height:1.3}
.kpi span{font-size:11px;color:var(--sub)}
.filters{position:sticky;top:104px;z-index:30;background:rgba(11,13,18,.94);backdrop-filter:blur(10px);
  border:1px solid var(--line);border-radius:var(--radius);padding:10px;margin-bottom:12px}
.frow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.frow+.frow{margin-top:8px}
.search{flex:1 1 230px;min-width:170px;position:relative}
.search input{width:100%;padding-left:27px}
.search:before{content:"⌕";position:absolute;left:8px;top:3px;color:var(--sub);font-size:15px}
.grp{display:flex;gap:5px;align-items:center;flex-wrap:wrap}
.grp>label{font-size:11px;color:var(--sub)}
.rng{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--tx2)}
.rng input[type=range]{width:100px;accent-color:var(--gold);padding:0;height:18px}
.rng b{color:var(--gold);font-weight:700;min-width:42px;font-variant-numeric:tabular-nums}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:11px;padding:3px 9px;border-radius:20px;background:var(--card2);border:1px solid var(--line);color:var(--tx2);cursor:pointer}
.chip:hover{border-color:var(--gold2);color:var(--gold)}
.ladder{display:flex;gap:7px;overflow-x:auto;padding-bottom:8px;margin-bottom:10px}
.lay{flex:0 0 auto;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;min-width:128px;cursor:pointer}
.lay:hover{border-color:var(--gold2)}
.lay em{font-style:normal;display:block;font-size:17px;font-weight:800;color:var(--gold)}
.lay span{font-size:11.5px;color:var(--tx2)}
.lay i{font-style:normal;font-size:10.5px;color:var(--sub);display:block}
.banner{border-radius:10px;padding:9px 12px;font-size:12px;margin-bottom:10px;line-height:1.65;display:flex;gap:8px}
.banner.e{background:rgba(242,85,79,.1);border:1px solid rgba(242,85,79,.35);color:#f6a19d}
.banner.n{background:rgba(220,174,93,.08);border:1px solid rgba(220,174,93,.3);color:#e6c98a}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(342px,1fr));gap:11px}
.grid.com{grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:8px}
.cd{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:11px 12px;position:relative}
.cd.full{border-color:rgba(220,174,93,.42)}
.r1{display:flex;align-items:flex-start;gap:9px}
.rk{font-size:10.5px;color:var(--sub);width:24px;flex:0 0 auto;text-align:center;line-height:1.3}
.rk b{display:block;font-size:14px;color:var(--gold);font-weight:800}
.nm{font-size:15.5px;font-weight:700;line-height:1.25}
.nm .bd{font-size:10.5px;font-weight:500;color:var(--sub);margin-left:5px}
.cd1{font-size:11px;color:var(--sub)}
.pchg{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:1px}
.u{color:var(--up)}.d{color:var(--dn)}
.score{margin-left:auto;text-align:right}
.score em{font-style:normal;font-size:23px;font-weight:800;color:var(--gold);font-variant-numeric:tabular-nums}
.score span{font-size:10.5px;color:var(--sub);display:block;margin-top:-3px}
.tagrow{display:flex;gap:5px;flex-wrap:wrap;margin:8px 0 2px}
.tg{font-size:10.5px;padding:2px 7px;border-radius:6px;background:#1b2130;color:#9aa6ba}
.tg.gold{background:rgba(220,174,93,.14);color:var(--gold)}
.tg.ok{background:rgba(47,164,106,.14);color:#5fc98d}
.tg.hot{background:rgba(242,85,79,.13);color:#f08a86}
.tg.blue{background:rgba(90,162,224,.13);color:#7fb6e8}
.mg{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;margin:9px 0 2px;
  background:var(--line);border:1px solid var(--line);border-radius:9px;overflow:hidden}
.mg div{background:var(--card2);padding:6px 7px;min-width:0}
.mg span{font-size:10px;color:var(--sub);display:block}
.mg b{font-size:12.5px;font-weight:650;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.bar2{height:3px;border-radius:3px;background:var(--line2);margin-top:4px;overflow:hidden}
.bar2 i{display:block;height:100%;background:linear-gradient(90deg,var(--gold2),var(--gold))}
.money{display:flex;gap:10px;flex-wrap:wrap;font-size:11.5px;padding:7px 0 2px;border-top:1px dashed var(--line);margin-top:7px}
.money b{font-weight:650;font-variant-numeric:tabular-nums}
.seats{font-size:11px;color:#93a0b5;line-height:1.75;margin-top:5px}
.seats .s{display:flex;gap:6px;align-items:baseline}
.seats .s em{font-style:normal;color:var(--sub);flex:0 0 15px}
.seats .s span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.seats .s b{color:#c8d2e2;font-variant-numeric:tabular-nums}
.seats .watch{color:var(--gold)}
.detail{margin-top:8px;border-top:1px dashed var(--line);padding-top:7px;font-size:11px;color:var(--sub);line-height:1.75}
.detail .why{color:#c99a58}
.bd3{display:flex;gap:3px;margin-top:5px;flex-wrap:wrap}
.bd3 i{font-style:normal;background:#181e2b;border-radius:5px;padding:2px 6px;font-size:10px;color:#8f9cb1}
.act{display:flex;gap:9px;margin-top:8px;align-items:center;font-size:11.5px}
.fav{position:absolute;right:9px;top:9px;font-size:16px;line-height:1;color:#39435a;cursor:pointer}
.fav.on{color:var(--gold)}
table{width:100%;border-collapse:collapse;font-size:12px;background:var(--card)}
th{background:#0e1219;color:var(--sub);font-weight:500;text-align:right;padding:7px 8px;font-size:11px;white-space:nowrap;
  position:sticky;top:0;z-index:2}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2),th:nth-child(3),td:nth-child(3){text-align:left}
td{padding:7px 8px;border-top:1px solid #1a1f2a;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr:hover td{background:#161b25}
.tb{overflow:auto;max-height:78vh;border:1px solid var(--line);border-radius:var(--radius)}
.empty{padding:64px 12px;text-align:center;color:var(--sub);line-height:2}
footer{margin-top:16px;padding:14px 0;border-top:1px solid var(--line);color:var(--sub);font-size:11px;line-height:1.9}
.drawer{position:fixed;right:0;bottom:0;left:0;background:var(--bg2);border-top:1px solid var(--line);
  padding:12px 14px;transform:translateY(103%);transition:.22s;z-index:50;max-height:72vh;overflow:auto}
.drawer.open{transform:none}
.drawer h4{font-size:13px;margin-bottom:6px}
.drawer textarea{width:100%;min-height:88px;background:#0d1017;font-size:12px;resize:vertical}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:#1b2130;border:1px solid var(--gold2);
  color:var(--gold);padding:8px 15px;border-radius:22px;font-size:12px;z-index:60;opacity:0;transition:.2s;pointer-events:none}
.toast.show{opacity:1}
@media(max-width:640px){.mg{grid-template-columns:repeat(2,1fr)}.filters{top:132px}.grid{grid-template-columns:1fr}
  .score em{font-size:20px}}
</style></head><body>
<header><div class="wrap">
  <div class="hd">
    <div class="logo"><i class="mk">妖</i>妖股雷达<small>短线打板工作台</small></div>
    <div class="tabs"><button id="vCard" onclick="setView('card')">卡片</button><button id="vTable" onclick="setView('table')">表格</button></div>
    <button id="denseBtn" onclick="toggleDense()">紧凑</button>
    <button onclick="openDrawer()">席位清单</button>
    <div class="spacer"></div>
    <div class="meta"><i class="dotk" id="liveDot"></i><span id="src">加载中…</span></div>
    <button class="on" onclick="load(1)">刷新数据</button>
  </div>
  <nav class="links"><a href="/" class="on">妖股雷达</a><a href="/strategy">善水策略选股</a></nav>
</div></header>
<div class="wrap">
  <div class="kpis" id="kpis"></div>
  <div class="filters">
    <div class="frow">
      <div class="search"><input id="q" placeholder="搜索 名称/代码/题材/行业/席位　（按 / 聚焦）" oninput="render()"></div>
      <div class="grp"><label>涨停日</label>
        <button id="tpAll" class="on" onclick="setTp('all')">全部</button>
        <button id="tpToday" onclick="setTp('today')">仅当日涨停</button>
        <button id="tpHist" onclick="setTp('hist')">仅非当日</button></div>
      <div class="grp">
        <button id="onlyFull" onclick="tog('onlyFull','onlyFull')">只看三项全中</button>
        <button id="onlyFav" onclick="tog('onlyFav','favOnly')">只看收藏</button>
        <button id="onlySeat" onclick="tog('onlySeat','seatOnly')">只看命中关注席位</button></div>
    </div>
    <div class="frow">
      <div class="grp"><label>板型</label><select id="board" onchange="render()">
        <option value="">全部</option><option>一字板</option><option>T字板</option><option>换手板</option></select></div>
      <div class="grp"><label>梯队</label><select id="tier" onchange="render()"><option value="">全部</option></select></div>
      <div class="grp"><label>排序</label><select id="sortSel" onchange="render()">
        <option value="score">评分</option><option value="lhb">龙虎榜净额</option><option value="org">机构净额</option>
        <option value="hot">游资净额</option><option value="pct">涨幅</option><option value="boards">连板数</option>
        <option value="seal">封板强弱</option><option value="turnover">换手率</option><option value="cap">总市值</option>
        <option value="amount">成交额</option></select></div>
      <div class="rng">评分≥<input type="range" id="minScore" min="0" max="90" step="5" value="0" oninput="render()"><b id="v_minScore">0</b></div>
      <div class="rng">涨幅≥<input type="range" id="minPct" min="-10" max="20" step="1" value="-10" oninput="render()"><b id="v_minPct">-10%</b></div>
      <div class="rng">市值≤<input type="range" id="maxCap" min="20" max="300" step="10" value="300" oninput="render()"><b id="v_maxCap">300亿</b></div>
      <div class="grp"><button id="dirBtn" onclick="flipDir()">从大到小</button>
        <button onclick="exportCsv()">导出CSV</button><button onclick="copyList()">复制列表</button>
        <button onclick="resetF()">清空筛选</button></div>
    </div>
    <div class="frow"><div class="chips" id="themes"></div></div>
  </div>
  <div id="msg"></div>
  <div class="ladder" id="tiers"></div>
  <div id="out"></div>
  <footer id="foot"></footer>
</div>
<div class="drawer" id="drawer"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
  <h4>游资席位关注清单</h4><button onclick="closeDrawer()">收起</button></div>
  <div style="font-size:11.5px;color:var(--sub);margin-bottom:7px;line-height:1.7">
    每行一个关键词（营业部名称片段即可）。命中后卡片席位行标金，并可用「只看命中关注席位」过滤。仅保存在你本机浏览器 localStorage，不会上传。</div>
  <textarea id="seatText" oninput="saveSeats()"></textarea>
  <div style="display:flex;gap:8px;margin-top:9px;flex-wrap:wrap;align-items:center">
    <button onclick="load(1)">重新取数</button><button onclick="clearFav()">清空收藏</button>
    <span style="font-size:11px;color:var(--sub)" id="favCount"></span></div></div>
<div class="toast" id="toast"></div>
<script>
let D={list:[]},FAV={},SEATS=[],VIEW='card',DENSE=false,LIST=[],DIR=-1;
let F={tp:'all',onlyFull:false,onlyFav:false,onlySeat:false};
const K='yaogu.v3.';
const LS=(k,v)=>{try{if(v===undefined)localStorage.removeItem(K+k);
  else localStorage.setItem(K+k,typeof v==='string'?v:JSON.stringify(v))}catch(e){}};
const LG=(k,d)=>{try{const x=localStorage.getItem(K+k);return x===null?d:x}catch(e){return d}};
function toast(t){const e=document.getElementById('toast');e.textContent=t;e.classList.add('show');
  setTimeout(()=>e.classList.remove('show'),1700)}
const V=v=>(v===null||v===undefined||v==='')?'--':v;
const N1=v=>(v===null||v===undefined||v==='')?'--':Number(v).toFixed(1);
const D8=x=>x?x.slice(4,6)+'-'+x.slice(6,8):'--';
const HM=x=>{if(!x)return '--';const s=String(x).padStart(6,'0');return s.slice(0,2)+':'+s.slice(2,4)};
const YI=v=>(v===null||v===undefined||v===0)?'--':(v>=100?Math.round(v):v.toFixed(2))+'亿';
const M2=v=>(v===null||v===undefined||v===0)?'--':(v>0?'+':'−')+Math.abs(v).toFixed(2)+'亿';
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const emurl=c=>'https://quote.eastmoney.com/'+(c[0]==='6'?'sh':'sz')+c+'.html';
const gburl=c=>'https://guba.eastmoney.com/list,'+c+'.html';
function watchHit(r){
  if(!SEATS.length)return [];
  const h=[];
  (r.top_buy||[]).concat(r.top_sell||[]).forEach(x=>{
    if(SEATS.some(k=>k&&String(x.seat).indexOf(k)>=0))h.push(x.seat);});
  return h;
}
async function load(refresh){
  document.getElementById('liveDot').className='dotk warn';
  document.getElementById('src').textContent=refresh?'重新取数中…（约 10–30 秒）':'加载中…';
  try{
    const d=await (await fetch('/api/radar?refresh='+(refresh||0)+'&t='+Date.now())).json();
    D=d;
    document.getElementById('liveDot').className='dotk';
    document.getElementById('src').textContent=
      (D.generated||'')+' · 行情 '+D8(D.latest)+' 收盘 · 候选 '+(D.total||0)+' · 当日涨停 '+(D.today_limit_up||0)+
      ' · 三项全中 '+(D.passed||0);
    let w='';
    if(D.error)w+='<div class="banner e"><span>!</span><div>刷新失败：'+esc(D.error)+
      '<br>当前展示的是上次成功的数据，可能不是最新。</div></div>';
    if(!D.mx_ok)w+='<div class="banner n"><span>i</span><div>未使用妙想选股（'+esc(D.mx_msg||'')+
      '），已自动降级为东方财富涨停池模式，榜单仍可用。</div></div>';
    if((D.quote_cover||0)<95)w+='<div class="banner n"><span>i</span><div>最新交易日行情覆盖 '+
      (D.quote_cover||0)+'%，未覆盖标的的换手率沿用其涨停日口径（卡片已标注）。</div></div>';
    if(D.lhb_pending)w+='<div class="banner n"><span>i</span><div>今日龙虎榜约 18:00 后披露，'+
      '当前龙虎榜维度取最近已披露日。</div></div>';
    document.getElementById('msg').innerHTML=w;
    buildKpis();buildTiers();buildThemes();render();
    document.getElementById('foot').innerHTML=
      '数据源：妙想选股（候选池）＋东方财富公开数据（涨停池／龙虎榜／席位明细／估值分析／最新报价）。'+
      '评分＝封板强弱20＋连板梯队20＋龙虎榜资金20＋涨幅15＋换手率15＋机构席位10；硬条件＝近3日涨停＋上龙虎榜＋总市值≤300亿。'+
      '<br>席位分类依据东方财富龙虎榜席位明细的营业部名称（机构专用／沪深股通／游资营业部）。「关注席位」关键词只保存在你本机浏览器。'+
      '本页面为公开数据整理与量化打分，不构成投资建议；龙虎榜为盘后披露数据，仅供复盘。';
  }catch(e){document.getElementById('src').textContent='加载失败：'+e}
}
function buildKpis(){
  const rows=D.list||[],hi=Math.max.apply(null,rows.map(r=>Math.max(r.boards||0,r.lianban||0)).concat([0]));
  const zx=rows.filter(r=>r.limit_up_today).length;
  const pr=D.promo||{},pk=Object.keys(pr);
  const up=rows.filter(r=>(r.pct||0)>0).length,dn=rows.filter(r=>(r.pct||0)<0).length;
  const cards=[
    ['候选标的',D.total||0,'三项全中 '+(D.passed||0)+' 只'],
    ['当日涨停',zx,'最高连板 '+hi+' 板'],
    ['红盘 / 绿盘',up+' / '+dn,'按最新交易日涨幅'],
    ['晋级率',pk.length?(pr[pk[pk.length-1]]+'%'):'--',pk.length?pk[pk.length-1]:'暂无数据'],
    ['行情口径',D8(D.latest),'覆盖 '+(D.quote_cover||0)+'% 候选'],
    ['收藏',Object.keys(FAV).length,'点卡片右上星标']];
  document.getElementById('kpis').innerHTML=cards.map(c=>
    '<div class="kpi"><span>'+c[0]+'</span><b>'+c[1]+'</b><i style="font-style:normal;font-size:10.5px;color:var(--sub);display:block">'+c[2]+'</i></div>').join('');
}
function buildTiers(){
  const t=D.tiers||{},order=['首板','2板','3板','4板及以上'],sel=document.getElementById('tier'),cur=sel.value;
  sel.innerHTML='<option value="">全部</option>'+order.filter(k=>t[k]).map(k=>'<option>'+k+'</option>').join('');
  sel.value=cur;
  const pr=D.promo||{},pk=Object.keys(pr);
  document.getElementById('tiers').innerHTML=order.filter(k=>t[k]).map(k=>{
    const x=t[k];
    return '<div class="lay" onclick="pickTier(\''+k+'\')">'+k+'<em>'+x.n+'只</em><span>均分 '+N1(x.avg)+
      '</span><i>'+(x.best?('最高标 '+esc(x.best.name)+' '+x.best.mb+'板'):'')+'</i></div>'}).join('')
    +'<div class="lay" style="cursor:default;border-style:dashed">晋级率<i>'+
      (pk.length?pk.map(x=>x+' '+pr[x]+'%').join('　'):'暂无')+'</i></div>';
}
function buildThemes(){
  const t=(D.industries||[]).slice(0,10);
  document.getElementById('themes').innerHTML=t.length?t.map(x=>
    '<span class="chip" onclick="setTheme(\''+esc(x.industry)+'\')">'+esc(x.industry)+' '+x.n+'只</span>').join('')
    :'<span style="font-size:11px;color:var(--sub)">题材主线：暂无</span>';
}
function val(id){return document.getElementById(id).value}
function filtered(){
  const q=val('q').trim().toLowerCase(),bd=val('board'),ti=val('tier');
  const numOr=(id,d)=>{const v=val(id);const n=v===''?d:Number(v);return isFinite(n)?n:d};
  const ms=numOr('minScore',0),mp=numOr('minPct',-10),mc=numOr('maxCap',300);
  document.getElementById('v_minScore').textContent=ms;
  document.getElementById('v_minPct').textContent=mp+'%';
  document.getElementById('v_maxCap').textContent=mc+'亿';
  return (D.list||[]).filter(r=>{
    if(F.tp==='today'&&!r.limit_up_today)return false;
    if(F.tp==='hist'&&r.limit_up_today)return false;
    if(F.onlyFull&&!r.hard_full)return false;
    if(F.onlyFav&&!FAV[r.code])return false;
    if(F.onlySeat&&!watchHit(r).length)return false;
    if(bd&&r.board_kind!==bd)return false;
    if(ti&&r.tier!==ti)return false;
    if((r.score||0)<ms)return false;
    if((r.pct||-99)<mp)return false;
    if(r.cap&&r.cap>mc)return false;
    if(!r.cap&&mc<300)return false;
    if(q){const hay=(r.name+r.code+(r.concepts||'')+(r.industry||'')+(r.lhb_reason||'')
      +(r.top_buy||[]).map(x=>x.seat).join('')+(r.top_sell||[]).map(x=>x.seat).join('')).toLowerCase();
      if(hay.indexOf(q)<0)return false}
    return true});
}
function sorted(rows){
  const k=val('sortSel');
  const g=r=>({score:r.score||0,lhb:r.lhb_net||0,org:r.org_net||0,hot:r.hot_net||0,pct:r.pct||0,
    boards:Math.max(r.boards||0,r.lianban||0),seal:r.seal_score||0,turnover:r.turnover||0,
    cap:(r.cap||0),amount:(r.amount||0)})[k];
  return rows.slice().sort((a,b)=>((g(a)-g(b))*DIR)||(a.code<b.code?-1:1));
}
function setTp(m){F.tp=m;
  document.getElementById('tpAll').className=m==='all'?'on':'';
  document.getElementById('tpToday').className=m==='today'?'on':'';
  document.getElementById('tpHist').className=m==='hist'?'on':'';
  LS('tp',m);render()}
function tog(key,store){F[key]=!F[key];document.getElementById(key).className=F[key]?'on':'';
  LS(store,F?'1':'0');render()}
function pickTier(k){const s=document.getElementById('tier');s.value=(s.value===k?'':k);render()}
function flipDir(){DIR=-DIR;document.getElementById('dirBtn').textContent=DIR<0?'从大到小':'从小到大';
  LS('dir',DIR<0?'-1':'1');render()}
function setTheme(t){document.getElementById('q').value=t;render()}
function resetF(){F={tp:'all',onlyFull:false,onlyFav:false,onlySeat:false};
  ['q','board','tier'].forEach(i=>document.getElementById(i).value='');
  document.getElementById('minScore').value=0;document.getElementById('minPct').value=-10;
  document.getElementById('maxCap').value=300;document.getElementById('sortSel').value='score';
  ['onlyFull','onlyFav','onlySeat'].forEach(i=>document.getElementById(i).className='');
  setTp('all');toast('筛选已清空')}
function seatLine(x,side){
  const w=SEATS.some(k=>k&&String(x.seat).indexOf(k)>=0);
  return '<div class="s"><em>'+side+'</em><span class="'+(w?'watch':'')+'">'+(w?'★ ':'')+
    esc(String(x.seat).slice(0,20))+'</span><b>'+x.amt.toFixed(2)+'亿</b>'+
    (x.net?'<i style="font-style:normal;color:'+(x.net>0?'#f08a86':'#5fc98d')+'">'+
      (x.net>0?'+':'−')+Math.abs(x.net).toFixed(2)+'</i>':'')+'</div>';
}
function card(r,i){
  const p=(r.pct||0)>=0?'u':'d',sign=(r.pct||0)>=0?'+':'';
  const hit=watchHit(r);
  const bars=Object.entries(r.score_detail||{}).map(x=>'<i>'+x[0]+' '+x[1]+'</i>').join('');
  const ct=(r.concepts||'').split('、').filter(Boolean);
  return '<div class="cd'+(r.hard_full?' full':'')+'">'+
   '<span class="fav'+(FAV[r.code]?' on':'')+'" onclick="tf(\''+r.code+'\')" title="收藏">'+(FAV[r.code]?'★':'☆')+'</span>'+
   '<div class="r1"><div class="rk">#<b>'+(i+1)+'</b></div><div style="min-width:0">'+
     '<div class="nm">'+esc(r.name)+'<span class="bd">'+esc(r.board_kind||'')+'</span></div>'+
     '<div class="cd1">'+r.code+' · '+esc(r.industry||'—')+(r.lhb_date?(' · 榜'+D8(r.lhb_date)):'')+'</div>'+
     '<div class="pchg '+p+'">'+sign+(r.pct||0).toFixed(2)+'%'+
       '<span style="font-size:11px;font-weight:400;color:var(--sub)"> / '+V(r.price)+'元</span></div></div>'+
     '<div class="score"><em>'+N1(r.score)+'</em><span>综合分</span></div></div>'+
   '<div class="tagrow">'+
     '<span class="tg '+(r.hard_full?'ok':'')+'">'+(r.hard_full?'✓ 三项全中':('硬条件 '+r.hard_met+'/'+r.hard_total))+'</span>'+
     '<span class="tg '+(r.limit_up_today?'gold':'')+'">'+(r.limit_up_today?'当日涨停':('非当日 · '+D8(r.limit_up_date)))+'</span>'+
     '<span class="tg blue">'+esc(r.tier||'')+'</span>'+
     (r.on_board?('<span class="tg hot">榜 '+M2(r.lhb_net)+'</span>'):'<span class="tg">未上榜</span>')+
     (r.org_cnt?('<span class="tg">'+r.org_cnt+'家机构'+esc(r.org_dir||'')+'</span>'):'')+
     (hit.length?('<span class="tg gold">★ 关注席位 '+hit.length+'</span>'):'')+'</div>'+
   '<div class="mg">'+
     '<div><span>总市值</span><b>'+YI(r.cap)+'</b></div>'+
     '<div><span>流通市值</span><b>'+YI(r.float_cap)+'</b></div>'+
     '<div><span>换手率</span><b>'+V((r.turnover||0).toFixed(2))+'%</b></div>'+
     '<div><span>成交额</span><b>'+YI(r.amount)+'</b></div>'+
     '<div><span>封板强弱</span><b>'+N1(r.seal_score)+'</b><div class="bar2"><i style="width:'+
       Math.min(100,r.seal_score||0)+'%"></i></div></div>'+
     '<div><span>首封 / 炸板</span><b>'+HM(r.first_seal)+' / '+(r.open_times||0)+'次</b></div>'+
     '<div><span>封单</span><b>'+YI(r.seal_fund)+'</b></div>'+
     '<div><span>近3日涨停</span><b>'+r.zt_days+'次'+(r.days&&r.boards?(' '+r.days+'天'+r.boards+'板'):'')+'</b></div></div>'+
   ((r.org_net||r.north_net||r.hot_net)?('<div class="money">'+
     '<span>机构 <b style="color:'+((r.org_net||0)>=0?'#f08a86':'#5fc98d')+'">'+M2(r.org_net)+'</b></span>'+
     '<span>北向 <b>'+M2(r.north_net)+'</b></span>'+
     '<span>游资 <b style="color:'+((r.hot_net||0)>=0?'#f08a86':'#5fc98d')+'">'+M2(r.hot_net)+'</b></span>'+
     (r.lhb_deal_ratio?('<span>榜内成交占比 <b>'+r.lhb_deal_ratio+'%</b></span>'):'')+
     (r.seat_org_win?('<span>机构3日胜率 <b>'+r.seat_org_win+'%</b></span>'):'')+'</div>'+
     '<div class="seats">'+(r.top_buy||[]).slice(0,3).map(x=>seatLine(x,'买')).join('')+
       (r.top_sell||[]).slice(0,2).map(x=>seatLine(x,'卖')).join('')+'</div>'):'')+
   (ct.length?'<div class="bd3">'+ct.slice(0,5).map(x=>'<i>'+esc(x)+'</i>').join('')+'</div>':'')+
   (r.lhb_reason?('<div class="detail"><span class="why">上榜原因</span> '+esc(r.lhb_reason)+
     '（'+D8(r.lhb_date)+'）</div>'):'')+
   (bars?'<div class="detail" style="border-top:0;padding-top:2px"><div class="bd3">'+bars+'</div></div>':'')+
   '<div class="act"><a href="'+emurl(r.code)+'" target="_blank" rel="noopener">东财行情</a>'+
     '<a href="'+gburl(r.code)+'" target="_blank" rel="noopener">股吧</a></div></div>';
}
function table(rows){
  const h=['#','代码','名称','板型','涨幅','最新价','换手','总市值','成交额','连板','首封','炸板','封单','封板分',
    '龙虎榜','机构','北向','游资','评分'];
  return '<div class="tb"><table><thead><tr>'+h.map(x=>'<th>'+x+'</th>').join('')+'</tr></thead><tbody>'+
   rows.map((r,i)=>'<tr onclick="window.open(emurl(\''+r.code+'\'))" style="cursor:pointer">'+
     '<td>'+(i+1)+'</td><td>'+r.code+'</td><td style="font-weight:600">'+(FAV[r.code]?'★ ':'')+esc(r.name)+'</td>'+
     '<td>'+esc(r.board_kind||'')+'</td><td class="'+((r.pct||0)>=0?'u':'d')+'">'+(r.pct||0).toFixed(2)+'%</td>'+
     '<td>'+V(r.price)+'</td><td>'+(r.turnover||0).toFixed(2)+'%</td><td>'+YI(r.cap)+'</td><td>'+YI(r.amount)+'</td>'+
     '<td>'+Math.max(r.boards||0,r.lianban||0)+'</td><td>'+HM(r.first_seal)+'</td><td>'+(r.open_times||0)+'</td>'+
     '<td>'+YI(r.seal_fund)+'</td><td>'+N1(r.seal_score)+'</td><td>'+M2(r.lhb_net)+'</td><td>'+M2(r.org_net)+'</td>'+
     '<td>'+M2(r.north_net)+'</td><td>'+M2(r.hot_net)+'</td>'+
     '<td style="color:var(--gold);font-weight:700">'+N1(r.score)+'</td></tr>').join('')+'</tbody></table></div>';
}
function render(){
  LIST=sorted(filtered());
  const out=document.getElementById('out');
  if(!LIST.length){out.innerHTML='<div class="empty">当前筛选没有标的<br>试试「清空筛选」，或放宽评分 / 市值 / 涨幅</div>';return}
  out.innerHTML=VIEW==='table'?table(LIST)
    :'<div class="grid'+(DENSE?' com':'')+'">'+LIST.map(card).join('')+'</div>';
  const fc=document.getElementById('favCount');
  if(fc)fc.textContent='已收藏 '+Object.keys(FAV).length+' 只';
}
function tf(c){FAV[c]=!FAV[c];if(!FAV[c])delete FAV[c];LS('fav',JSON.stringify(FAV));render();
  toast(FAV[c]?'已收藏 '+c:'已取消收藏')}
function setView(v){VIEW=v;
  document.getElementById('vCard').className=v==='card'?'on':'';
  document.getElementById('vTable').className=v==='table'?'on':'';
  LS('view',v);render()}
function toggleDense(){DENSE=!DENSE;const b=document.getElementById('denseBtn');
  b.textContent=DENSE?'舒适':'紧凑';b.className=DENSE?'on':'';LS('dense',DENSE?'1':'0');render()}
function openDrawer(){document.getElementById('drawer').classList.add('open')}
function closeDrawer(){document.getElementById('drawer').classList.remove('open')}
function saveSeats(){SEATS=document.getElementById('seatText').value.split('\n').map(x=>x.trim()).filter(Boolean);
  LS('seats',document.getElementById('seatText').value);render()}
function clearFav(){FAV={};LS('fav','{}');render();toast('收藏已清空')}
function exportCsv(){
  if(!LIST.length)return toast('没有可导出的数据');
  const cols=['code','name','score','pct','price','turnover','cap','float_cap','amount','tier','boards','lianban',
    'zt_days','limit_up_today','board_kind','first_seal','open_times','seal_fund','seal_score','on_board','lhb_net',
    'lhb_buy','lhb_sell','org_net','north_net','hot_net','lhb_deal_ratio','seat_org_win','industry','lhb_reason','concepts'];
  const cn=['代码','名称','评分','涨幅%','最新价','换手%','总市值亿','流通市值亿','成交额亿','梯队','连板','最高连板',
    '近3日涨停次数','是否当日涨停','板型','首封时间','炸板次数','封单亿','封板强弱','是否上榜','龙虎榜净额亿',
    '龙虎榜买入亿','龙虎榜卖出亿','机构净额亿','北向净额亿','游资净额亿','榜内成交占比%','机构3日胜率%','行业','上榜原因','题材'];
  const q=v=>'"'+String(v==null?'':v).replace(/"/g,'""')+'"';
  const csv=[cn.map(q).join(',')].concat(LIST.map(r=>cols.map(c=>q(r[c])).join(','))).join('\r\n');
  const blob=new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='妖股雷达_'+(D.latest||'')+new Date().toISOString().slice(11,16).replace(':','')+'.csv';
  document.body.appendChild(a);a.click();a.remove();
  toast('已导出 '+LIST.length+' 行 CSV');
}
function copyList(){
  if(!LIST.length)return toast('没有可复制的数据');
  const t=LIST.map((r,i)=>(i+1)+'. '+r.name+' '+r.code+'　'+N1(r.score)+'分　'+(r.pct||0).toFixed(2)+'%　'+
    r.tier+'　市值'+YI(r.cap)+'　龙虎'+M2(r.lhb_net)+'　'+(r.limit_up_today?'当日涨停':'非当日')).join('\n');
  const head='妖股雷达 '+(D.latest||'')+' · 共 '+LIST.length+' 只\n';
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(head+t).then(()=>toast('已复制 '+LIST.length+' 条'),()=>toast('复制失败，请用导出CSV'));
  }else toast('该浏览器不支持一键复制，请用导出CSV');
}
document.addEventListener('keydown',e=>{
  const tag=document.activeElement?document.activeElement.tagName:'';
  if(e.key==='/'&&tag!=='INPUT'&&tag!=='TEXTAREA'){e.preventDefault();document.getElementById('q').focus()}
  else if(e.key==='Escape')closeDrawer();
  else if(e.key==='ArrowDown'&&tag!=='INPUT'){e.preventDefault();nav(1)}
  else if(e.key==='ArrowUp'&&tag!=='INPUT'){e.preventDefault();nav(-1)}
});
let fi=0;
function nav(d){
  if(!LIST.length)return;
  fi=(fi+d+LIST.length)%LIST.length;
  const els=document.querySelectorAll('.cd');
  const el=els[fi];if(!el)return;
  el.scrollIntoView({behavior:'smooth',block:'center'});
  el.style.outline='1px solid var(--gold)';setTimeout(()=>el.style.outline='',900);
  toast(LIST[fi].name+' '+LIST[fi].code+'　'+N1(LIST[fi].score)+'分');
}
(function init(){
  try{FAV=JSON.parse(LG('fav','{}')||'{}')||{}}catch(e){FAV={}}
  const sw=LG('seats','')||'';
  document.getElementById('seatText').value=sw;
  SEATS=sw.split('\n').map(x=>x.trim()).filter(Boolean);
  F.tp=LG('tp','all');
  F.onlyFull=LG('onlyFull','0')==='1';F.onlyFav=LG('favOnly','0')==='1';F.onlySeat=LG('seatOnly','0')==='1';
  ['onlyFull','onlyFav','onlySeat'].forEach(k=>document.getElementById(k).className=F[k]?'on':'');
  DENSE=LG('dense','0')==='1';
  const b=document.getElementById('denseBtn');b.textContent=DENSE?'舒适':'紧凑';b.className=DENSE?'on':'';
  DIR=LG('dir','-1')==='1'?1:-1;
  setView(LG('view','card'));setTp(F.tp);
  load(0);setInterval(()=>load(0),60000);
})();
</script></body></html>"""


STRAT_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>善水策略 · 选股结果</title>
<style>
:root{--bg:#07080b;--card:#12151b;--line:#1f242d;--tx:#e9edf3;--sub:#8790a0;--up:#f0524f;--gold:#d4a24a;--green:#3fb97a}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:14px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:14px;max-width:1120px;margin:0 auto}
a{color:#7aa7d8;text-decoration:none;font-size:12px}
h2{font-size:18px;margin-bottom:2px}
.sub{color:var(--sub);font-size:12px;margin-bottom:12px;line-height:1.7}
.sec{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:12px}
.sec h3{font-size:14px;margin-bottom:9px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.badge{font-size:11px;padding:2px 9px;border-radius:20px;background:rgba(63,185,122,.15);color:var(--green);font-weight:600}
.badge.n{background:rgba(212,162,74,.15);color:#e3bd76}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--sub);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid #171c24;vertical-align:top}
tr:last-child td{border-bottom:0}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;color:#aeb7c6;font-size:12px}
.nm{font-weight:600}.u{color:var(--up);font-weight:600}
.miss{color:#e39a5b;font-size:11.5px;line-height:1.5}
.pnl{display:flex;gap:7px;flex-wrap:wrap;font-size:11.5px;color:var(--sub)}
.pnl span{background:#171d26;border:1px solid var(--line);border-radius:7px;padding:4px 9px}
.pnl b{color:var(--gold);font-weight:700}
.tpl{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px;font-size:12px}
.tpl div{background:#171d26;border-radius:7px;padding:5px 9px;color:#aeb7c6}
.tpl b{color:#dfe5ee;font-weight:600;float:right}
.note{font-size:11.5px;color:#8f98a8;line-height:1.75}
.warn{background:rgba(227,114,74,.1);border:1px solid rgba(227,114,74,.35);color:#f0a878;border-radius:9px;padding:10px 12px;font-size:12px;margin-bottom:12px;line-height:1.7}
button{background:#1b2029;color:var(--tx);border:1px solid var(--line);border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer}
button.p{background:rgba(212,162,74,.16);border-color:rgba(212,162,74,.45);color:#e3bd76}
.conds{font-size:11.5px;color:#9aa4b4;line-height:1.9}.conds b{color:#cfd6e2;font-weight:600}
.sl{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px 16px}
.sl label{display:block;font-size:12px;color:#aeb7c6;margin-bottom:9px}
.sl label span{float:right;color:var(--gold);font-weight:700;font-variant-numeric:tabular-nums}
input[type=range]{width:100%;accent-color:#d4a24a;height:20px;margin-top:3px}
.sw{display:flex;gap:14px;flex-wrap:wrap;margin-top:4px;font-size:12px;color:#aeb7c6}
.sw b{font-weight:400}
.sw input{accent-color:#d4a24a;margin-right:4px}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:2px 0 10px}
</style></head><body>
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
<a href="/">← 返回妖股雷达</a><div><button onclick="reset()">恢复默认阈值</button>
<button class="p" onclick="load(1)">按当前阈值重新取数</button></div></div>
<h2>📦 <span id="tt">善水策略</span> 选股结果</h2>
<div class="sub" id="src">加载中…</div>
<div id="msg"></div>
<div class="sec"><h3>⚙ 阈值调节 <span style="font-size:11px;color:#71809a;font-weight:400">拖动即时重算，不请求妙想</span></h3>
<div class="sl" id="sliders"></div>
<div class="sw" id="switches"></div></div>
<div id="box"></div>
<div class="sec"><h3>策略条件定义</h3><div class="conds" id="defs"></div></div>
<div class="sec"><h3>模板票基准值 · 善水科技 301190（2026-09-30 收盘）</h3>
<div class="tpl">
<div>涨幅<b>20.01%</b></div><div>换手/实换手<b>7.95%/16.71%</b></div><div>量比<b>1.25</b></div>
<div>总市值<b>77.12亿</b></div><div>流通市值<b>65.34亿</b></div><div>5日涨幅<b>75.61%</b></div>
<div>MA5/10/20<b>26.60/22.92/20.65</b></div><div>最高价<b>35.93(创52周新高)</b></div>
<div>主力净额<b>+3210万</b></div><div>5日主力净额<b>+1.21亿</b></div><div>市净率<b>3.86</b></div>
<div>成交额<b>5.10亿</b></div></div>
<div class="note" style="margin-top:8px">基准值取自你提供的两张东方财富截图；默认阈值按该票当日取值反推。6 条判定全部本地完成（妙想每次只查 2 次：结构池 + 52周新高确认）。</div></div>
<script>
const CFG=[['turn_lo','换手率下限',0,40,0.5,'%'],['turn_hi','换手率上限',0,60,0.5,'%'],
           ['lb_lo','量比下限',0,5,0.1,''],['lb_hi','量比上限',0,8,0.1,''],
           ['cap_max','总市值上限',10,500,10,'亿'],['float_max','流通市值上限',10,400,10,'亿'],
           ['chg5d_min','5日涨幅下限',0,150,5,'%']];
const SWI=[['use_ma','C2 均线多头'],['use_money','C3 主力净额为正'],['use_newhigh','C6 须创52周新高']];
let D=null, TH={};
const E=v=>(v===null||v===undefined||v===0||v==='--')?'--':Math.abs(v).toFixed(2)+'亿';
const P=v=>(v===null||v===undefined||v==='')?'--':v;
const g=(r,k,d)=>(r[k]===null||r[k]===undefined||r[k]==='')?d:r[k];
function cl(r,nh){
  const t=TH;
  return {'C1涨停确认':true,
   'C2均线多头':!t.use_ma||(g(r,'ma5',0)>g(r,'ma10',0)&&g(r,'ma10',0)>g(r,'ma20',0)),
   'C3抛压衰竭':!t.use_money||(g(r,'main_net',0)>0&&g(r,'main_net_5d',0)>0),
   'C4量能与换手':t.turn_lo<=g(r,'turnover',-1)&&g(r,'turnover',-1)<=t.turn_hi&&t.lb_lo<=g(r,'liangbi',-1)&&g(r,'liangbi',-1)<=t.lb_hi,
   'C5小市值':g(r,'cap',9e9)<=t.cap_max&&(r.float_cap==null||r.float_cap===0||r.float_cap<=t.float_max),
   'C6阶段强势创新高':g(r,'chg5d',0)>t.chg5d_min&&(!t.use_newhigh||r.new_high_52w)};
}
function why(k,r){
  if(k==='C2均线多头')return '均线未多头（MA5 '+P(r.ma5)+' / MA10 '+P(r.ma10)+' / MA20 '+P(r.ma20)+'）';
  if(k==='C3抛压衰竭')return 'D-1还有资金流出（主力净额 '+P(r.main_net)+'亿，5日合计 '+P(r.main_net_5d)+'亿）';
  if(k==='C4量能与换手')return '量能不符（换手 '+P(r.turnover)+'%，量比 '+P(r.liangbi)+'）';
  if(k==='C5小市值')return '市值超区间（总市值 '+P(r.cap)+'亿，流通 '+P(r.float_cap)+'亿）';
  if(k==='C6阶段强势创新高')return '强度或新高不够（5日涨幅 '+P(r.chg5d)+'%，最高价 '+P(r.peak)+'，52周新高 '+(r.new_high_52w?'是':'否')+'）';
  return '未涨停';
}
function compute(){
  const rows=(D&&D.rows)||[],nh=new Set(rows.filter(r=>r.new_high_52w).map(r=>r.code));
  const strict=[],near=[],counts={};
  (D?D.conds:[]).forEach(k=>counts[k]=0);
  rows.forEach(r=>{const res=cl(r,nh);const miss=[];
    Object.keys(res).forEach(k=>{if(res[k])counts[k]++;else miss.push(k)});
    if(miss.length===0)strict.push(r);
    else if(miss.length===1){const o=Object.assign({},r);o.miss=miss[0];o.miss_reason=why(miss[0],r);near.push(o)}
  });
  const by=(a,b)=>(b.pct-a.pct)||(a.code<b.code?-1:1);
  return {strict:strict.sort(by),near:near.sort(by),counts:counts};
}
function tbl(rows,mm){
  if(!rows.length)return '<div class="note">当前阈值下没有标的；可放宽条件或点「按当前阈值重新取数」。</div>';
  let h=mm?'<table><tr><th>代码</th><th>名称</th><th>最新价</th><th>D0涨</th><th>总市值</th><th>5日涨</th><th>差的那一条</th></tr>'
          :'<table><tr><th>代码</th><th>名称</th><th>最新价</th><th>D0涨</th><th>换手</th><th>量比</th><th>总市值</th><th>流通</th><th>5日涨</th><th>题材</th></tr>';
  rows.forEach(r=>{h+= mm
    ? `<tr><td class="mono">${r.code}</td><td class="nm">${r.name}</td><td>${P(r.price)}</td><td class="u">+${P(r.pct)}%</td><td>${E(r.cap)}</td><td>${P(r.chg5d)}%</td><td class="miss"><b>${r.miss}</b> · ${r.miss_reason}</td></tr>`
    : `<tr><td class="mono">${r.code}</td><td class="nm">${r.name}</td><td>${P(r.price)}</td><td class="u">+${P(r.pct)}%</td><td>${P(r.turnover)}%</td><td>${P(r.liangbi)}</td><td>${E(r.cap)}</td><td>${E(r.float_cap)}</td><td>${P(r.chg5d)}%</td><td class="miss">${(r.concepts||'').split('、').slice(0,4).join('、')}</td></tr>`});
  return h+'</table>';
}
function renderCtl(){
  document.getElementById('sliders').innerHTML=CFG.map(c=>
    `<label>${c[1]}<span id="v_${c[0]}">${TH[c[0]]}${c[5]}</span>
     <input type="range" min="${c[2]}" max="${c[3]}" step="${c[4]}" value="${TH[c[0]]}"
       oninput="onSlide('${c[0]}',this.value,'${c[5]}')"></label>`).join('');
  document.getElementById('switches').innerHTML=SWI.map(x=>
    `<b><input type="checkbox" ${TH[x[0]]?'checked':''} onchange="onSw('${x[0]}',this.checked)">${x[1]}</b>`).join('');
}
function render(){
  if(!D)return;
  const c=compute();
  document.getElementById('box').innerHTML=
   `<div class="sec"><h3>✅ 严格命中（6/6 全满足）<span class="badge">${c.strict.length} 只</span></h3>${tbl(c.strict,false)}</div>
    <div class="sec"><h3>🎯 最接近信号（5/6，只差1条）<span class="badge n">${c.near.length} 只</span></h3>${tbl(c.near,true)}</div>
    <div class="sec"><h3>条件通过统计（结构池内满足该条的家数，越少越卡）</h3>
      <div class="pnl">${Object.entries(c.counts).map(([k,v])=>`<span>${k} <b>${v}</b></span>`).join('')}</div>
      <div class="note" style="margin-top:8px">${D.note}</div></div>`;
}
function onSlide(k,v,u){TH[k]=parseFloat(v);document.getElementById('v_'+k).textContent=v+u;render();}
function onSw(k,v){TH[k]=v;render();}
function reset(){TH=Object.assign({},D.th_default);renderCtl();render();}
function qs(){let q='';Object.keys(TH).forEach(k=>{q+='&'+k+'='+encodeURIComponent(TH[k])});return q}
async function load(refresh){
  document.getElementById('src').textContent=refresh?'重新取数中…（2 次妙想查询，约 10–20 秒）':'加载中…';
  try{
    const d=await (await fetch('/api/strategy?refresh='+(refresh?1:0)+qs())).json();
    if(d.error){document.getElementById('src').textContent='';
      document.getElementById('msg').innerHTML=`<div class="warn">策略选股失败：${d.error}</div>`;return;}
    D=d;TH=Object.assign({},d.th);
    document.getElementById('tt').textContent=d.name;
    document.getElementById('src').textContent=`${d.generated} · 结构池 ${d.pool} 只（52周新高 ${d.new_high} 只）· 当前阈值下严格命中 ${d.strict.length} 只`;
    document.getElementById('defs').innerHTML=Object.entries(d.cond_desc).map(([k,v])=>`<div><b>${k}</b> — ${v}</div>`).join('');
    renderCtl();render();
  }catch(e){document.getElementById('src').textContent='加载失败：'+e}
}
load(0);
</script></body></html>"""
