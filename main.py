# main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import json

# ==========================================
# 模块一：妙想 API 客户端
# ==========================================
import re
import time
import urllib.error
import urllib.request

BASE_URL = "https://mkapi2.dfcfs.com/finskillshub"
MIN_INTERVAL = 0.35
_last_call = 0.0
CALL_COUNT = 0

RATE_LIMIT_MARKERS = ("请求频率过高", "请求过于频繁", "操作过于频繁", "too many requests", "frequent", "rate limit", "限流")
ERR_CODES = {113: "今日调用次数已达上限", 114: "apikey 无效或已失效", 115: "请求未携带 apikey"}

class MiaoXiangError(Exception):
    def __init__(self, code, message, raw=None):
        self.code = code
        self.message = message
        self.raw = raw
        super().__init__(f"[{code}] {message}")

def _throttle():
    global _last_call
    gap = time.monotonic() - _last_call
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last_call = time.monotonic()

def call(endpoint: str, body: dict, *, api_key: str | None = None, retries: int = 3, timeout: float = 25.0) -> dict:
    global CALL_COUNT
    key = (api_key or os.environ.get("MX_APIKEY", "")).strip()
    if not key:
        raise MiaoXiangError(115, "未提供 apikey(请设置环境变量 MX_APIKEY)")
    
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_err = None
    for attempt in range(retries + 1):
        _throttle()
        req = urllib.request.Request(
            BASE_URL + endpoint, data=payload,
            headers={"Content-Type": "application/json;charset=UTF-8", "apikey": key},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                CALL_COUNT += 1
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8", "ignore")
            except Exception:
                pass
            if e.code in (429, 503) or any(m in raw for m in RATE_LIMIT_MARKERS):
                last_err = MiaoXiangError(429, "触发频率限制")
                time.sleep(1.2 * (attempt + 1))
                continue
            raise MiaoXiangError(e.code, f"HTTP {e.code}", raw) from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(0.8 * (attempt + 1))
    raise MiaoXiangError(-1, f"重试 {retries} 次仍失败: {last_err}")

def _check_biz(res: dict) -> dict:
    code = res.get("code")
    data = res.get("data") or {}
    bcode = data.get("code", code)
    if res.get("success") is False or (isinstance(bcode, int) and bcode != 0):
        msg = res.get("message") or data.get("message") or "未知错误"
        if isinstance(bcode, int) and bcode in ERR_CODES:
            msg = ERR_CODES[bcode]
        raise MiaoXiangError(bcode, str(msg), res)
    return res

CANON = {
    "SECURITY_CODE": "code", "SECURITY_SHORT_NAME": "name", "MARKET_SHORT_NAME": "market",
    "NEWEST_PRICE": "price", "CHG": "chg", "PCHG": "pchg", "010000_HLP": "profit_ratio",
    "010000_CMFB_461_JZD90": "chip_conc_90", "010000_TURNOVER_RATE": "turnover_rate",
    "010000_LIANGBI": "volume_ratio", "010000_VOLUME": "volume", "010000_TRADING_VOLUMES": "amount",
    "010000_TOAL_MARKET_VALUE": "total_mv", "010000_CIRCULATION_MARKET_VALUE": "float_mv",
    "010000_PE_D": "pe", "010000_PB": "pb", "010000_PEAK_PRICE": "high", "010000_BOTTOM_PRICE": "low",
    "010000_DURATION_LIMIT_UP": "limit_up_count", "010000_JX": "ma", "010000_CUSTOM_IFSTSTOCK_IFSTSTOCK_": "is_st",
}
_KEY_RE = re.compile(r"^(?P<base>[A-Za-z0-9_]+?)(?:<\d+>)?(?:\{(?P<meta>[^}]*)\})?$")
_UNIT_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*(亿|万|%)?")

def split_key(k: str) -> tuple[str, str]:
    m = _KEY_RE.match(k or "")
    return (m.group("base"), (m.group("meta") or "")) if m else (k, "")

def to_num(v):
    if v is None or isinstance(v, bool): return None
    if isinstance(v, (int, float)): return float(v)
    m = _UNIT_RE.search(str(v))
    if not m: return None
    n = float(m.group(1)); unit = m.group(2)
    if unit == "亿": n *= 1e8
    elif unit == "万": n *= 1e4
    return n

def _dig(node, *path):
    for p in path:
        if not isinstance(node, dict): return None
        node = node.get(p)
    return node

def _parse_md_table(md: str):
    if not md or "|" not in md: return [], []
    lines = [ln.strip() for ln in md.strip().splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2: return [], []
    def cells(ln): return [c.strip() for c in ln.strip("|").split("|")]
    headers = cells(lines[0]); rows = []
    for ln in lines[1:]:
        cs = cells(ln)
        if cs and set("".join(cs)) <= set("-: "): continue
        rows.append(dict(zip(headers, cs)))
    return headers, rows

def _pick_result(d: dict):
    cands = [(_dig(d, "allResults", "result"), "allResults.result"), (_dig(d, "result"), "result"), (_dig(d, "allResults"), "allResults")]
    for node, src in cands:
        if isinstance(node, dict) and (node.get("dataList") or node.get("columns")):
            return node.get("columns") or [], node.get("dataList") or [], node.get("total"), src
    md = d.get("partialResults")
    if isinstance(md, str) and "|" in md:
        cols, rows = _parse_md_table(md)
        if rows: return [{"title": h, "key": h} for h in cols], rows, len(rows), "partialResults(md)"
    return [], [], 0, "none"

def normalize(d: dict) -> dict:
    columns, data_list, total, source = _pick_result(d)
    title_of = {}
    for c in columns:
        if isinstance(c, dict):
            base, _ = split_key(c.get("key") or "")
            if base: title_of.setdefault(base, c.get("title") or base)
    out_rows = []
    for row in data_list:
        if not isinstance(row, dict): continue
        canon, extra = {}, {}
        for k, v in row.items():
            base, meta = split_key(k)
            field = CANON.get(base)
            if field:
                if field == "ma": extra[f"{title_of.get(base, base)}@{meta}"] = v
                else: canon[field] = v
            else:
                title = title_of.get(base, base)
                if base in ("CHOICE_INNER_CODE", "IN_OPTIONAL", "SERIAL", "MARKET_SHORT_NAME"): continue
                extra[f"{title}@{meta}" if meta else title] = v
        for f in ("price", "chg", "pchg", "profit_ratio", "chip_conc_90", "turnover_rate", "volume_ratio", "pe", "pb", "high", "low"):
            if f in canon: canon[f] = to_num(canon[f])
        for f in ("total_mv", "float_mv", "volume", "amount"):
            if f in canon: canon[f + "_raw"] = canon[f]; canon[f] = to_num(canon[f])
        if "limit_up_count" in canon: canon["limit_up_count"] = to_num(canon["limit_up_count"])
        canon["extra"] = extra
        out_rows.append(canon)
    tc = d.get("totalCondition")
    tc_desc = tc.get("describe") if isinstance(tc, dict) else tc if isinstance(tc, str) else None
    if not tc_desc: tc_desc = _dig(d, "allResults", "totalCondition", "describe")
    return {"total": total if total is not None else len(out_rows), "condition": tc_desc, "data_date": None, "source": source, "rows": out_rows}

def stock_screen(keyword: str, page_no: int = 1, page_size: int = 50, api_key: str | None = None) -> dict:
    body = {"keyword": keyword}
    if page_no != 1: body["pageNo"] = page_no
    if page_size: body["pageSize"] = page_size
    res = _check_biz(call("/api/claw/stock-screen", body, api_key=api_key))
    d = _dig(res, "data", "data") or {}
    out = normalize(d)
    out["query"] = keyword
    for c in (d.get("allResults", {}) or {}).get("result", {}).get("columns", []) or []:
        _, meta = split_key(c.get("key") or "")
        if len(meta) == 10 and meta[4] == "-": out["data_date"] = meta; break
    return out

def liumei_coarse_keyword(*, mv_min: int = 30, mv_max: int = 300, limit_up_days: int = 15, exclude_st: bool = True, profit_min: float | None = None, profit_max: float | None = None, min_turnover: float | None = None, min_volume_ratio: float | None = None, ma_up: bool = True, chip: bool = True) -> str:
    parts = []
    if exclude_st: parts.append("非ST")
    parts.append(f"总市值{mv_min}亿到{mv_max}亿")
    parts.append(f"近{limit_up_days}日内有涨停")
    if ma_up: parts.append("5日均线向上")
    if profit_min is not None or profit_max is not None:
        lo = f"{profit_min}%" if profit_min is not None else "0%"
        hi = f"{profit_max}%" if profit_max is not None else "100%"
        parts.append(f"获利盘在{lo}到{hi}之间")
    if chip: parts.append("筹码集中度")
    if min_turnover is not None: parts.append(f"今日换手率大于{min_turnover}%")
    if min_volume_ratio is not None: parts.append(f"今日量比大于{min_volume_ratio}")
    return "，".join(parts)

def screen_liumei(**kw) -> dict:
    return stock_screen(liumei_coarse_keyword(**kw))

# ==========================================
# 模块二：FastAPI Web 服务
# ==========================================
app = FastAPI(title="Yaogu-Radar API", description="柚子六脉选股工具后端")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ScreenRequest(BaseModel):
    mv_min: int = 30
    mv_max: int = 300
    profit_min: float = 60
    profit_max: float = 85

@app.get("/")
def read_root():
    return {"status": "ok", "message": "妖股雷达服务已启动，请访问 /docs 测试接口"}

@app.post("/api/screen")
async def screen_stocks(req: ScreenRequest):
    try:
        coarse_data = screen_liumei(
            mv_min=req.mv_min, mv_max=req.mv_max,
            profit_min=req.profit_min, profit_max=req.profit_max
        )
        return {"code": 200, "msg": "success", "data_date": coarse_data.get('data_date'), "total": coarse_data.get('total'), "data": coarse_data.get('rows', [])}
    except MiaoXiangError as e:
        raise HTTPException(status_code=400, detail=f"妙想API调用失败: {e.message}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)