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
    try:
        if v is None:
            return default
        s = str(v).strip().replace(",", "").replace("%", "")
        if s in ("", "-", "--", "None", "null"):
            return default
        return float(s)
    except Exception:                               # noqa: BLE001
        return default


def yi(v, nd=2):
    """元 -> 亿元；无效值返回 None（区别于 0，避免把取数失败当成"市值 0"）"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
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
请筛选最近一个交易日A股市场最强势的股票候选，返回至少40只。
重点关注：1.当日涨幅较大；2.当日换手率较高；3.最近出现涨停；
4.最近出现龙虎榜；5.总市值300亿元以内；6.排除ST、*ST、退市股票。
请返回：股票代码、股票简称、最新价、涨跌幅、换手率、总市值。
不要因为缺少市值、龙虎榜或涨停次数而不返回股票。
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
                        "cap": yi(mx_pick(r, ["TOTAL_MARKET_CAP", "总市值", "市值"]))})
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
                "amount": yi(num(it.get("amount")) * 1e4, 2),        # 成交额 万元 -> 亿
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
    return clamp(sum(detail.values()), 0, 100), detail


# ============================== 8. 组装 ==============================

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
                                     "turnover": c["turnover"], "cap_mx": c["cap"]})
    for d in dates:                                  # 涨停池兜底（同时保证硬条件可判）
        for code, it in pools[d].items():
            e = cands.setdefault(code, {"code": code})
            e.setdefault("name", it["name"])
            e.setdefault("price", it["price"])
            e.setdefault("pct", it["pct"])
            e.setdefault("turnover", it["turnover"])

    caps = get_caps(list(cands), latest)

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

        zt_days, best = 0, None
        for d in dates:
            it = pools[d].get(code)
            if it:
                zt_days += 1
                best = best or it
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
        cap = capinfo.get("cap") or src.get("cap_pool") or c.get("cap_mx")
        float_cap = capinfo.get("float_cap") or src.get("float_cap") or (lhb or {}).get("float_cap")

        p = {
            "code": code, "name": name,
            "price": capinfo.get("close") or c.get("price") or src.get("price") or 0,
            "pct": src.get("pct", c.get("pct", 0)) or (capinfo.get("chg") or 0),
            "turnover": src.get("turnover", c.get("turnover", 0)) or (lhb or {}).get("turnover", 0),
            "cap": cap, "float_cap": float_cap,
            "pe": capinfo.get("pe"), "pb": capinfo.get("pb"),
            "industry": src.get("industry") or capinfo.get("board", ""),
            "lianban": src.get("lianban", 0), "days": src.get("days", 0),
            "boards": src.get("boards", 0), "seal_fund": src.get("seal_fund"),
            "first_seal": src.get("first_seal", 0), "open_times": src.get("open_times", 0),
            "zt_days": zt_days, "seal_date": dates[0],
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
            "first_seal", "open_times", "seal_score", "on_board", "lhb_net", "lhb_buy",
            "lhb_sell", "lhb_deal_ratio", "lhb_reason", "lhb_explain", "lhb_date",
            "org_cnt", "org_dir", "org_net", "north_net", "hot_net", "seat_org_win",
            "top_buy", "top_sell", "seat_date", "score", "score_detail",
            "hard_met", "hard_total", "hard_full")
    out = [{k: r[k] for k in keep if k in r} for r in st["data"][:80]]
    return JSONResponse({"generated": st["meta"].get("generated"),
                         "dates": st["meta"].get("dates"), "latest": st["meta"].get("latest"),
                         "source": st["source"], "error": st["error"],
                         "refreshing": st["refreshing"], "total": st["meta"].get("total"),
                         "passed": st["meta"].get("passed"), "mx_ok": st["meta"].get("mx_ok"),
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


# ============================== 11. 前端 ==============================

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>妖股雷达 · A股短线打板筛选台</title>
<style>
:root{--bg:#07080b;--card:#12151b;--line:#1f242d;--tx:#e9edf3;--sub:#8790a0;--up:#f0524f;--dn:#2ea043;--gold:#d4a24a}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:14px;max-width:1360px;margin:0 auto}
h1{font-size:19px}
.rule{color:var(--sub);font-size:12px;margin:5px 0 12px;line-height:1.75}
.rule b{color:var(--gold);font-weight:600}
.tiers{display:flex;gap:8px;overflow-x:auto;padding-bottom:10px;margin-bottom:6px}
.tier{flex:0 0 auto;background:var(--card);border:1px solid var(--line);border-radius:9px;padding:8px 12px;font-size:12px;min-width:118px}
.tier em{font-style:normal;font-weight:700;font-size:16px;color:var(--gold);display:block;margin-top:3px}
.tier i{font-style:normal;color:var(--sub);font-size:11px}
.bar{display:flex;justify-content:space-between;align-items:center;color:var(--sub);font-size:12px;margin:6px 0;gap:8px;flex-wrap:wrap}
.bar button{background:#1b2029;color:var(--tx);border:1px solid var(--line);border-radius:6px;padding:5px 11px;font-size:12px;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:12px}
.full{border-color:rgba(212,162,74,.5)}
.hd{display:flex;justify-content:space-between;align-items:flex-start}
.nm{font-size:16px;font-weight:700}.cd{font-size:11px;color:var(--sub);letter-spacing:.4px}
.rk{font-size:11px;color:var(--sub);text-align:right}
.rk i{font-style:normal;color:var(--gold);font-weight:700;font-size:17px;display:block}
.ln{display:flex;gap:6px;flex-wrap:wrap;margin:7px 0 3px}
.tag{font-size:10.5px;padding:2px 7px;border-radius:20px;background:#1b2029;color:#a9b2c1}
.tag.g{background:rgba(212,162,74,.15);color:#e3bd76}
.tag.n{background:rgba(46,160,67,.14);color:#68c98a}
.sc{display:flex;align-items:baseline;gap:8px;margin:6px 0}
.sc em{font-style:normal;font-size:24px;font-weight:800;color:var(--gold)}
.sc span{font-size:11px;color:var(--sub)}.pc{font-weight:700;font-size:14px}
.u{color:var(--up)}.d{color:var(--dn)}
.row{display:flex;justify-content:space-between;font-size:12.5px;padding:3px 0;border-top:1px dashed var(--line)}
.row span{color:var(--sub)}
.sum{background:#0e1116;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-top:8px;font-size:11.5px;color:#a9b2c1;line-height:1.7}
.sum b{color:#dfe5ee;font-weight:600}
.seat{color:#95a0b1;font-size:11px}
.why{margin-top:6px;font-size:11px;color:#b58a4a;line-height:1.6}
.warn{background:rgba(227,114,74,.1);border:1px solid rgba(227,114,74,.35);color:#f0a878;border-radius:9px;padding:9px 12px;font-size:12px;margin-bottom:12px;line-height:1.7}
.note{background:rgba(212,162,74,.08);border:1px solid rgba(212,162,74,.28);color:#e3c48a;border-radius:9px;padding:9px 12px;font-size:12px;margin-bottom:12px}
.empty{padding:56px 10px;text-align:center;color:var(--sub);line-height:2}
.split{display:flex;justify-content:space-between;font-size:11px;color:var(--sub);padding-top:5px}
</style></head><body>
<h1>妖股雷达</h1>
<div class="rule">硬条件：<b>近3交易日涨停</b> + <b>龙虎榜</b> + <b>总市值≤300亿</b>　评分：<b>封板强弱20</b> + <b>连板梯队20</b> + <b>龙虎榜资金20</b> + <b>涨幅15</b> + <b>换手率15</b> + <b>机构席位10</b><br>数据源：妙想选股 + 东方财富公开数据（涨停池 / 龙虎榜 / 席位明细 / 估值分析）</div>
<div class="bar"><div id="src">加载中…</div><div>
<button onclick="onlyFull=!onlyFull;render()">只看硬条件全中</button>
<button onclick="sortMode=sortMode==='score'?'lhb':'score';render()">按评分/龙虎榜</button>
<button onclick="load(1)">刷新</button></div></div>
<div id="msg"></div><div id="tiers" class="tiers"></div><div id="out" class="grid"></div>
<script>
let RAW=[],TIERS={},onlyFull=false,sortMode='score';
const V=v=>(v===null||v===undefined||v==='')?'--':v;
const E=v=>(v===null||v===undefined||v===0)?'--':Math.abs(v).toFixed(2)+'亿';
const S=v=>v>0?('+'+v.toFixed(2)+'亿'):(v<0?('-'+Math.abs(v).toFixed(2)+'亿'):'0');
function card(r,i){
  const p=(r.pct||0)>=0?'u':'d', sign=(r.pct||0)>=0?'+':'';
  const hh=r.hard_full?'✓ 三项全中':('⚠ 硬条件 '+r.hard_met+'/'+r.hard_total+(r.cap?'':' · 市值未知'));
  const nb=r.days&&r.boards?(r.days+'天'+r.boards+'板'):(r.lianban?r.lianban+'连板':'--');
  const fs=r.first_seal?String(r.first_seal).padStart(6,'0').replace(/(\d\d)(\d\d)\d\d/,'$1:$2'):'--';
  const seats=(r.top_buy||[]).slice(0,3).map(x=>'<div class="seat">买 '+x.seat.slice(0,16)+' '+x.amt.toFixed(2)+'亿'+(x.net?' 净'+x.net.toFixed(2)+'亿':'')+'</div>').join('')
             +(r.top_sell||[]).slice(0,2).map(x=>'<div class="seat">卖 '+x.seat.slice(0,16)+' '+x.amt.toFixed(2)+'亿</div>').join('');
  return `<div class="card${r.hard_full?' full':''}">
   <div class="hd"><div><div class="nm">${r.name}</div><div class="cd">${r.code}</div></div>
     <div class="rank">#<i>${i+1}</i></div></div>
   <div class="sc"><em>${r.score}</em><span>分</span><span class="pc ${p}">${sign}${(r.pct||0).toFixed(2)}%</span>
     <span class="tag">${r.tier}</span></div>
   <div class="ln"><span class="tag ${r.hard_full?'g':''}">${hh}</span>
     ${r.on_board?`<span class="tag n">龙虎榜 ${S(r.lhb_net)}</span>`:'<span class="tag">未上龙虎榜</span>'}
     ${r.org_cnt?`<span class="tag n">${r.org_cnt}家机构${r.org_dir}</span>`:''}</div>
   <div class="row"><span>总市值 / 流通</span><b>${V(r.cap)}亿 / ${V(r.float_cap)}亿</b></div>
   <div class="row"><span>最新价 / 换手</span><b>${V(r.price)} / ${V((r.turnover||0).toFixed(2))}%</b></div>
   <div class="row"><span>近3日涨停 / 梯队</span><b>${r.zt_days}次 · ${nb}</b></div>
   <div class="row"><span>封板强弱</span><b>${r.seal_score}分 · 首封${fs} · 炸板${r.open_times}次</b></div>
   <div class="row"><span>封单 / 行业</span><b>${E(r.seal_fund)} · ${V(r.industry)}</b></div>
   ${(r.org_net||r.north_net||r.hot_net)?`<div class="sum">席位净额：
     <b>机构 ${S(r.org_net)}</b>　北向 ${S(r.north_net)}　游资 ${S(r.hot_net)}
     ${r.seat_org_win?('<br>机构席位3日胜率 <b>'+r.seat_org_win+'%</b>'):''}
     ${r.lhb_deal_ratio?('<br>龙虎榜成交占比 <b>'+r.lhb_deal_ratio+'%</b>'):''}
     ${seats?'<br>'+seats:''}</div>`:''}
   ${r.lhb_reason?`<div class="why">上榜原因：${r.lhb_reason}${r.lhb_date?('（'+r.lhb_date+'）'):''}</div>`:''}
   <div class="split"><div>评分构成 ${Object.entries(r.score_detail||{}).map(([k,v])=>k+v).join(' · ')}</div>
   <div>${(r.seat_date||r.lhb_date||'').replace(/(\d{4})(\d{2})(\d{2})/,'$2-$3')}</div></div></div>`;
}
function render(){
  let l=RAW.slice();
  if(onlyFull) l=l.filter(r=>r.hard_full);
  l.sort(sortMode==='score'?(a,b=>b.score-a.score):(a,b=>(b.lhb_net||0)-(a.lhb_net||0)));
  document.getElementById('out').innerHTML=l.length?l.map(card).join('')
    :'<div class="empty">暂无符合条件的股票<br>休市日或条件过严，可点右上刷新</div>';
}
function tiersHtml(t){
  const order=['首板','2板','3板','4板及以上'];
  document.getElementById('tiers').innerHTML=order.filter(k=>t[k]).map(k=>{
    const x=t[k],b=x.best?('<i>最高标 '+x.best.name+'('+x.best.mb+'板)</i>'):'';
    return `<div class="tier">${k}<em>${x.n}只</em>均分 ${x.avg}${b?'<br>'+b:''}</div>`;
  }).join('') || '<div class="tier">梯队<i>暂无涨停数据</i></div>';
}
async function load(refresh){
  document.getElementById('src').textContent='加载中…';
  try{
    const d=await (await fetch('/api/radar?'+Date.now())).json();
    RAW=d.list||[];TIERS=d.tiers||{};
    document.getElementById('src').textContent=
      `${d.generated||''} · ${d.latest||''} · 候选${d.total||0}只 · 三项全中${d.passed||0}只 · ${d.source}`;
    let w='';
    if(d.error) w+=`<div class="warn">数据刷新失败：${d.error}<br>当前展示的是上次成功的快照，可能不是最新。</div>`;
    if(d.refreshing && !RAW.length) w+='<div class="note">后台正在拉取数据（约 10–30 秒），请稍候再刷新。</div>';
    if(!d.mx_ok) w+=`<div class="note">未使用妙想选股（${d.mx_msg||''}），已自动降级为东方财富涨停池模式，榜单仍可用。</div>`;
    if(d.lhb_pending) w+='<div class="note">今日龙虎榜约 18:00 后披露，当前龙虎榜维度取最近已披露日。</div>';
    document.getElementById('msg').innerHTML=w;
    tiersHtml(TIERS);render();
  }catch(e){document.getElementById('src').textContent='加载失败：'+e}
}
load(0);setInterval(()=>load(0),60000);
</script></body></html>"""
