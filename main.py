import os
import json
import time
import math
import threading
from datetime import datetime, timedelta, timezone

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="妖股雷达")

MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

CN_TZ = timezone(timedelta(hours=8))

MAX_CAP_YI = 300.0        # 硬条件：总市值上限（亿元）
LOOKBACK_DAYS = 3         # 硬条件：近 N 个交易日涨停
CACHE_SECONDS = 60        # 雷达结果缓存
MX_CACHE_SECONDS = 900    # 妙想候选缓存 15 分钟，省日配额

# ---------------------------------------------------------------- 基础设施

_lock = threading.RLock()
_STORE = {}                       # 通用缓存池：key -> (过期时间戳, 值)
_RADAR = {"ts": 0, "data": [], "meta": {}, "source": "未连接", "error": None}


def _cached(key, ttl, producer, now=None):
    """带锁的 TTL 缓存。ttl 可为按天失效的历史数据。"""
    now = now if now is not None else time.time()
    with _lock:
        hit = _STORE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    val = producer()
    with _lock:
        _STORE[key] = (now + ttl, val)
    return val


def _today_key():
    return datetime.now(CN_TZ).strftime("%Y%m%d")


def http_get(url, params=None, timeout=15, tries=3):
    """带退避重试的 GET；三次都失败才抛异常。"""
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:                      # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep(0.6 * (i + 1))
    raise last


def yd(v):
    """元 -> 亿元"""
    try:
        return round(float(v or 0) / 1e8, 2)
    except Exception:                               # noqa: BLE001
        return None


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


# ---------------------------------------------------------------- 1. 妙想：只负责找候选

QUERY = """
请筛选今天A股市场最强势的股票候选，返回至少30只。
重点关注：1.今日涨幅较大；2.今日换手率较高；3.最近出现涨停；
4.最近出现龙虎榜；5.市值300亿元以内；6.排除ST、*ST、退市股票。
请返回：股票代码、股票简称、最新价、涨跌幅、换手率。
不要因为缺少市值、龙虎榜或涨停次数而不返回股票。
"""


def mx_raw():
    key = os.getenv("MX_APIKEY")
    if not key:
        raise RuntimeError("MX_APIKEY 未设置")
    r = requests.post(MX_URL,
                      headers={"Content-Type": "application/json", "apikey": key},
                      json={"keyword": QUERY}, timeout=40)
    r.raise_for_status()
    obj = r.json()
    if obj.get("status") != 0:
        raise RuntimeError("妙想返回异常: %s" % obj.get("message"))
    return obj


def parse_mx(obj):
    inner = (((obj or {}).get("data") or {}).get("data") or {})
    result = ((inner.get("allResults") or {}).get("result") or {})
    rows = result.get("dataList") or []
    if rows:
        return rows
    lines = [x.strip() for x in str(inner.get("partialResults") or "").splitlines()
             if x.strip().startswith("|")]
    if len(lines) < 3:
        return []

    def cells(x):
        return [v.strip() for v in x.strip("|").split("|")]

    head = cells(lines[0])
    out = []
    for line in lines[2:]:
        vals = cells(line)
        if len(vals) == len(head):
            out.append(dict(zip(head, vals)))
    return out


def pick(row, names, fuzzy=False):
    """先精确命中列名；模糊匹配时只认包含关系，并按列名长度升序取最短的一个，
    避免把"近5日涨跌幅"当成"涨跌幅"。"""
    for n in names:
        if n in row and row[n] not in ("", None):
            return row[n]
    if not fuzzy:
        return ""
    cand = []
    for k, v in row.items():
        if v in ("", None):
            continue
        lk = str(k).lower()
        for n in names:
            if n.lower() in lk:
                cand.append((len(str(k)), v))
                break
    cand.sort(key=lambda x: x[0])
    return cand[0][1] if cand else ""


def mx_candidates():
    def build():
        try:
            rows = parse_mx(mx_raw())
        except Exception as e:                      # noqa: BLE001
            print("MX_SKIP:", repr(e)[:160])
            return []
        out = []
        for row in rows:
            code = "".join(c for c in str(pick(row, ["SECURITY_CODE", "股票代码", "证券代码", "代码"]))
                           if c.isdigit())
            name = str(pick(row, ["SECURITY_SHORT_NAME", "SECURITY_NAME_ABBR",
                                  "股票简称", "证券简称", "名称"])).strip()
            if len(code) != 6 or not name:
                continue
            out.append({"code": code, "name": name,
                        "price": num(pick(row, ["NEWEST_PRICE", "最新价", "现价"])),
                        "pct": num(pick(row, ["CHG", "涨跌幅", "涨幅"])),
                        "turnover": num(pick(row, ["TURNOVER_RATE", "换手率", "HSL"]))})
        print("MX_OK 候选%d只" % len(out))
        return out

    return _cached("mx_cand_" + _today_key(), MX_CACHE_SECONDS, build)


# ---------------------------------------------------------------- 2. 交易日历（借龙虎榜的日期分布反推）
#
# 为什么不能拿"请求某个日期 + 看涨停池是否有数据"来判定交易日：
# 接口对非交易日/未来日期不会返回空，而是回退(clamp)到最近一个真实交易日的池子，
# 于是国庆长假会连着好天返回同一份 9/30 数据 —— 近3日涨停次数被虚高成 3 次。
# data 里的 qdate 也恒等于最近交易日，同样不能用于判定。
# 龙虎榜每条记录自带真实 TRADE_DATE，用一次区间查询就能拿到真实交易日历。

def _lhb_page(page, flt, columns="SECURITY_CODE,SECURITY_NAME_ABBR,BILLBOARD_NET_AMT,"
                                     "BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,TRADE_DATE",
               sort="BILLBOARD_NET_AMT", size=500):
    return http_get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                    {"reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": columns,
                     "pageNumber": page, "pageSize": size,
                     "sortColumns": sort, "sortTypes": -1,
                     "source": "WEB", "client": "WEB", "filter": flt})


def get_trading_dates(count=LOOKBACK_DAYS):
    def build():
        end = datetime.now(CN_TZ)
        for window in (16, 40):                       # 先看半个月，不够再放宽到 40 天
            begin = end - timedelta(days=window)
            flt = "(TRADE_DATE>='%s')(TRADE_DATE<='%s')" % (
                begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            seen = set()
            for page in (1, 2, 3):
                res = (_lhb_page(page, flt, columns="TRADE_DATE", sort="TRADE_DATE")
                       .get("result") or {})
                rows = res.get("data") or []
                seen.update(r["TRADE_DATE"][:10].replace("-", "") for r in rows)
                if not rows or page >= (res.get("pages") or 1):
                    break
            dates = sorted(seen, reverse=True)[:count]
            if len(dates) >= count or (dates and window == 40):
                print("TRADING_DATES:", dates)
                return dates
        raise RuntimeError("未取到任何交易日")

    return _cached("trade_dates_" + _today_key(), 3600, build)


def _is_hist(date, latest):
    """历史日期数据不再变化，可以长缓存。"""
    return date != latest


# ---------------------------------------------------------------- 3. 涨停池（含总市值/封板细节）

def get_zt_pool(date, latest=None):
    def build():
        pool, page, tc = [], 0, None
        while True:
            data = http_get("https://push2ex.eastmoney.com/getTopicZTPool",
                            {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                             "Pageindex": page, "pagesize": 200, "sort": "fbt:asc", "date": date},
                            timeout=15)
            got = data.get("data") or {}
            chunk = got.get("pool") or []
            pool.extend(chunk)
            tc = got.get("tc") or 0
            if not chunk or len(pool) >= tc or page > 20:
                break
            page += 1
        out = {}
        for it in pool:
            code = str(it.get("c", "")).zfill(6)
            zt = it.get("zttj") or {}
            out[code] = {
                "name": it.get("n", ""),
                "pct": round(num(it.get("zdp")), 2),
                "turnover": round(num(it.get("hs")), 2),
                "price": round(num(it.get("p", 0)) / 1000, 2),    # p 是 元×1000（用 tshare/股本反推确认）
                "cap": yd(it.get("tshare")),                      # 总市值（亿）
                "float_cap": yd(it.get("ltsz")),
                "lianban": int(num(it.get("lbc"))),                # 连板次数
                "days": int(num(zt.get("days"))),
                "boards": int(num(zt.get("ct"))),                  # N天M板
                "seal_fund": yd(it.get("fund")),                   # 封单金额（亿）
                "first_seal": int(num(it.get("fbt"))),              # 首次封板 09:25 -> 92500
                "last_seal": int(num(it.get("lbt"))),
                "open_times": int(num(it.get("zbc"))),              # 炸板次数
                "industry": it.get("hybk", ""),
            }
        print("ZT %s -> %d只" % (date, len(out)))
        return out

    # 最新交易日盘中会变 -> 短缓存；更早的历史日期不再变化 -> 长缓存
    ttl = CACHE_SECONDS if not _is_hist(date, latest) else 6 * 3600
    return _cached("zt_" + date, ttl, build)


# ---------------------------------------------------------------- 4. 龙虎榜（同日多条要合并）

def get_lhb(date, latest=None):
    def build():
        rows = []
        for page in range(1, 6):
            data = http_get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                            {"reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
                             "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,BILLBOARD_NET_AMT,"
                                        "BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,TRADE_DATE",
                             "pageNumber": page, "pageSize": 500,
                             "sortColumns": "BILLBOARD_NET_AMT", "sortTypes": -1,
                             "source": "WEB", "client": "WEB",
                             "filter": "(TRADE_DATE='%s-%s-%s')" % (date[:4], date[4:6], date[6:])},
                            timeout=15)
            res = data.get("result") or {}
            chunk = res.get("data") or []
            rows.extend(chunk)
            if not chunk or page >= (res.get("pages") or 1):
                break
        out = {}
        for row in rows:
            code = str(row.get("SECURITY_CODE", "")).zfill(6)
            net = num(row.get("BILLBOARD_NET_AMT"))
            item = out.setdefault(code, {"net": 0.0, "times": 0})
            item["times"] += 1
            # 一只票多个上榜原因时，取绝对值最大的那条，避免重复累加
            if abs(net) > abs(item["net"]):
                item["net"] = net
        print("LHB %s -> %d只 / %d条" % (date, len(out), len(rows)))
        return out

    ttl = CACHE_SECONDS if not _is_hist(date, latest) else 6 * 3600
    return _cached("lhb_" + date, ttl, build)


# ---------------------------------------------------------------- 5. 组装雷达

def hard_hit(p):
    """三个硬条件分别判定；市值未知记 None 而不是 False。"""
    return {
        "zt": p["zt_days"] > 0,
        "lhb": p["on_board"],
        "cap": None if p["cap"] in (None, 0) else p["cap"] <= MAX_CAP_YI,
    }


def seal_strength(p):
    """封板强弱：首封越早、炸板越少、封单占流通市值比越高 -> 分越高（0~1）。"""
    fbt = p["first_seal"]
    if fbt <= 0:
        t_score = 0.35
    else:
        h, m = divmod(fbt // 100, 100)
        minutes = (h * 60 + m) - (9 * 60 + 25)
        t_score = max(0.0, 1.0 - min(max(minutes, 0), 210) / 210)
    zbc = p["open_times"]
    z_score = 1.0 if zbc == 0 else max(0.0, 1.0 - zbc * 0.28)
    fc = p["float_cap"] or 0
    ratio = (p["seal_fund"] or 0) / fc if fc > 0 else 0
    f_score = min(ratio / 0.02, 1.0)
    return round(0.4 * t_score + 0.3 * z_score + 0.3 * f_score, 4)


def score(p):
    """总分 0~100：涨幅20 + 换手20 + 连板20 + 龙虎榜资金25 + 封板强弱15。"""
    s_pct = min(max(p["pct"], 0), 10) / 10 * 20

    t = p["turnover"]
    if t <= 0:
        s_to = 0.0
    elif t <= 20:                       # 20% 换手以内线性给分
        s_to = t / 20 * 18
    else:                               # 过高换手视为分歧，反向扣分
        s_to = max(6.0, 18 - (t - 20) * 0.6)

    lb = max(p["zt_days"], p["lianban"])
    s_lb = min(lb, 4) / 4 * 20

    net = p["lhb_net"]
    if net <= 0:
        s_net = 0.0 if net == 0 else max(0.0, 8 + net * 3)   # 净卖出少量分
    else:
        s_net = min(net / 3.0, 1.0) * 25

    s_seal = seal_strength(p) * 15
    return round(s_pct + s_to + s_lb + s_net + s_seal, 1)


def build_radar():
    dates = get_trading_dates(LOOKBACK_DAYS)
    latest = dates[0]
    pools = {d: get_zt_pool(d, latest) for d in dates}
    lhbs = {d: get_lhb(d, latest) for d in dates}

    cands = {}
    for c in mx_candidates():
        cands.setdefault(c["code"], c)
    for d in dates:                                 # 涨停池兜底，也保证市值一定有
        for code, it in pools[d].items():
            cands.setdefault(code, {"code": code, "name": it["name"],
                                    "price": it["price"], "pct": it["pct"],
                                    "turnover": it["turnover"]})

    rows = []
    for code, c in cands.items():
        name = (c.get("name") or "").strip()
        up = name.upper()
        if "ST" in up or name.startswith("退") or len(code) != 6:
            continue

        best, zt_days = None, 0
        for i, d in enumerate(dates):               # i=0 是最近的一天
            it = pools[d].get(code)
            if it:
                zt_days += 1
                if best is None:
                    best = it
        src = best or {}

        lhb_net, on_board, lhb_times = 0.0, False, 0
        for d in dates:
            hit = lhbs[d].get(code)
            if hit:
                on_board = True
                lhb_times += hit["times"]
                if abs(hit["net"]) > abs(lhb_net):
                    lhb_net = hit["net"]

        cap = src.get("cap")
        p = {
            "code": code, "name": name or src.get("name", ""),
            "price": c.get("price") or src.get("price") or 0,
            "pct": src.get("pct", c.get("pct", 0)),
            "turnover": src.get("turnover", c.get("turnover", 0)),
            "cap": cap if cap else None,
            "float_cap": src.get("float_cap"),
            "industry": src.get("industry", ""),
            "lianban": src.get("lianban", 0),
            "days": src.get("days", 0), "boards": src.get("boards", 0),
            "seal_fund": src.get("seal_fund", 0),
            "first_seal": src.get("first_seal", 0),
            "last_seal": src.get("last_seal", 0),
            "open_times": src.get("open_times", 0),
            "zt_days": zt_days, "on_board": on_board, "lhb_times": lhb_times,
            "lhb_net": yd(lhb_net) or 0.0,
        }
        if zt_days == 0 and not on_board:           # 两个信号都没有，不必上榜
            continue

        p["hard"] = hard_hit(p)
        p["hard_met"] = sum(1 for v in p["hard"].values() if v is True)
        p["hard_total"] = sum(1 for v in p["hard"].values() if v is not None)
        p["score"] = score(p)
        p["seal"] = round(seal_strength(p) * 100)
        rows.append(p)

    rows.sort(key=lambda x: (x["hard_met"], x["score"]), reverse=True)
    return rows, dates


def radar(refresh=False):
    now = time.time()
    with _lock:
        fresh = _RADAR["ts"] and (now - _RADAR["ts"] < CACHE_SECONDS)
        if fresh and not refresh:
            return dict(_RADAR)
    try:
        rows, dates = build_radar()
        with _lock:
            _RADAR.update({"ts": now, "data": rows,
                           "meta": {"dates": dates,
                                    "total": len(rows),
                                    "passed": sum(1 for r in rows if r["hard_met"] == r["hard_total"] == 3),
                                    "generated": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                                    "mx_used": bool(mx_candidates())},
                           "source": "妙想选股 + 东方财富公开数据", "error": None})
    except Exception as e:                          # noqa: BLE001
        with _lock:
            _RADAR["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
            _RADAR["ts"] = now if not _RADAR["ts"] else _RADAR["ts"]
    with _lock:
        return dict(_RADAR)


# ---------------------------------------------------------------- 6. 路由与页面

API = "/api/radar"


@app.get(API)
def api_radar(refresh: int = 0):
    st = radar(refresh=bool(refresh))
    out = []
    for r in st["data"][:60]:
        out.append({k: v for k, v in r.items() if k not in ("hard",)})
    return JSONResponse({"generated": st["meta"].get("generated"),
                         "dates": st["meta"].get("dates"),
                         "source": st["source"], "error": st["error"],
                         "passed": st["meta"].get("passed"),
                         "total": st["meta"].get("total"),
                         "mx_used": st["meta"].get("mx_used"),
                         "list": out})


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


# ---------------------------------------------------------------- 7. 前端

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>妖股雷达</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#21262d;--tx:#e6edf3;--sub:#8b949e;--up:#f0524f;--dn:#2ea043;--gold:#d4a24a}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:14px}
h1{font-size:20px;display:flex;align-items:center;gap:8px}
.rule{color:var(--sub);font-size:12px;line-height:1.7;margin:6px 0 2px}
.rule b{color:var(--gold);font-weight:600}
.bar{display:flex;justify-content:space-between;align-items:center;color:var(--sub);font-size:12px;margin:8px 0 14px;flex-wrap:wrap;gap:6px}
.bar button{background:#1f2630;color:var(--tx);border:1px solid var(--line);border-radius:6px;padding:5px 12px;font-size:12px;cursor:pointer}
.dot{width:7px;height:7px;border-radius:50%;background:var(--dn);display:inline-block;margin-right:5px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(258px,1fr));gap:11px}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:13px;position:relative;transition:.15s}
.card:hover{border-color:#3d444d;transform:translateY(-1px)}
.full{border-color:rgba(212,162,74,.55)}
.rank{position:absolute;right:12px;top:11px;font-size:11px;color:var(--sub)}
.rank i{color:var(--gold);font-style:normal;font-weight:700}
.nm{font-size:16px;font-weight:700}
.cd{font-size:11px;color:var(--sub);margin-top:2px;letter-spacing:.5px}
.sc{display:flex;align-items:baseline;gap:9px;margin:9px 0 10px}
.sc em{font-style:normal;font-size:25px;font-weight:800;color:var(--gold)}
.sc span{font-size:11px;color:var(--sub)}
.pc{font-weight:700;font-size:14px}
.u{color:var(--up)}.d{color:var(--dn)}
.row{display:flex;justify-content:space-between;font-size:12.5px;padding:3.5px 0;border-top:1px dashed var(--line)}
.row span{color:var(--sub)}
.tag{display:inline-block;font-size:10.5px;padding:2px 7px;border-radius:20px;background:#1f2630;color:var(--sub);margin-top:7px}
.hard{font-size:11px;color:var(--sub);margin-top:6px}
.hard b{color:var(--dn);font-weight:600}.hard i{color:#e3724a;font-style:normal}
.empty{padding:60px 10px;text-align:center;color:var(--sub);font-size:13px;line-height:2}
.warn{background:rgba(227,114,74,.12);border:1px solid rgba(227,114,74,.4);color:#f0a878;border-radius:9px;padding:10px 12px;font-size:12px;margin-bottom:12px;line-height:1.6}
</style>
</head>
<body>
<h1>🔥 妖股雷达</h1>
<div class="rule">硬条件：<b>近3交易日涨停</b> + <b>龙虎榜</b> + <b>总市值≤300亿</b><br>
评分：涨幅 + 换手率 + 龙虎榜资金 + 近3日涨停次数 + 封板强弱</div>
<div class="bar"><div><i class="dot"></i><span id="src">加载中…</span></div>
<div><button onclick="load(1)">↻ 手动刷新</button></div></div>
<div id="warn"></div><div id="out" class="grid"></div>
<script>
const fmt=(v,suf='')=>(v===null||v===undefined||v===''?'--':v+suf);
const yi=v=>(v===null||v===undefined||v===0)?'--':Math.abs(v).toFixed(2)+'亿';
function card(r,i){
  const total=r.hard_total||3, met=r.hard_met||0;
  const full=met===total;
  const pcs=(r.pct||0).toFixed(2);
  const cls=(r.pct||0)>=0?'u':'d';
  const sign=(r.pct||0)>=0?'+':'';
  const net=(r.lhb_net||0);
  const netTxt=r.on_board?(net>=0?'+':'-')+yi(net):'未上榜';
  const nb=r.days&&r.boards?`${r.days}天${r.boards}板`:(r.lianban?`${r.lianban}连板`:'--');
  return `<div class="card${full?' full':''}">
    <div class="rank">#<i>${i+1}</i></div>
    <div class="nm">${r.name}</div><div class="cd">${r.code}</div>
    <div class="sc"><em>${r.score}</em><span>分</span>
      <span class="pc ${cls}">${sign}${pcs}%</span></div>
    <div class="hard">${full?'✓ 硬条件':`⚠ 硬条件 <i>${met}/${total}</i>`}
      ${r.cap===null||r.cap===0?' · 市值未知':''}${r.zt_days===0?' · 近3日未涨停':''}${!r.on_board?' · 未上龙虎榜':''}</div>
    <div class="row"><span>总市值</span><b>${fmt(r.cap,'亿')}</b></div>
    <div class="row"><span>最新价</span><b>${fmt(r.price)}</b></div>
    <div class="row"><span>换手率</span><b>${fmt((r.turnover||0).toFixed(2),'%')}</b></div>
    <div class="row"><span>龙虎榜净额</span><b class="${net>=0?'u':'d'}">${netTxt}</b></div>
    <div class="row"><span>近3日涨停</span><b>${r.zt_days}次 · ${nb}</b></div>
    <div class="row"><span>封板强弱</span><b>${r.seal}分 · 炸板${r.open_times}次</b></div>
    <div class="row"><span>封单</span><b>${yi(r.seal_fund)}</b></div>
    <div class="tag">${r.industry||'—'}</div></div>`;
}
async function load(refresh){
  document.getElementById('src').textContent='加载中…';
  try{
    const d=await (await fetch('/api/radar?refresh='+(refresh?1:0))).json();
    document.getElementById('src').textContent=
      `${d.generated||''} · 共${d.total||0}只 · 硬条件全中${d.passed||0}只 · ${d.mx_used?'妙想选股':'纯涨停池'}`;
    let w='';
    if(!d.mx_used) w+='当前未使用妙想选股（MX_APIKEY 未设置或调用失败），已降级为东方财富涨停池模式，榜单仍可用。<br>';
    if(d.error) w+='数据刷新出错：'+d.error+'（展示的是上次成功的结果）';
    document.getElementById('warn').innerHTML=w?`<div class="warn">${w}</div>`:'';
    const list=d.list||[];
    document.getElementById('out').innerHTML=list.length
      ? list.map(card).join('')
      : '<div class="empty">暂无符合条件的股票<br>休市日或条件过严，可稍后重试</div>';
  }catch(e){document.getElementById('src').textContent='加载失败：'+e}
}
load(0);setInterval(()=>load(0),60000);
</script>
</body>
</html>"""