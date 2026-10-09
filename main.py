# main.py
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
import os
import json
import re
import time
import urllib.error
import urllib.request
import requests

# ==========================================
# 模块一：妙想 API 客户端 (代理)
# ==========================================
BASE_URL = "https://mkapi2.dfcfs.com/finskillshub"
MIN_INTERVAL = 0.35
_last_call = 0.0
CALL_COUNT = 0

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

def call(endpoint: str, body: dict, *, api_key: str = "", retries: int = 3, timeout: float = 25.0) -> dict:
    global CALL_COUNT
    if not api_key:
        raise MiaoXiangError(115, "未提供 apikey")
    
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_err = None
    for attempt in range(retries + 1):
        _throttle()
        req = urllib.request.Request(
            BASE_URL + endpoint, data=payload,
            headers={"Content-Type": "application/json;charset=UTF-8", "apikey": api_key},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                CALL_COUNT += 1
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = ""
            try: raw = e.read().decode("utf-8", "ignore")
            except: pass
            if e.code in (429, 503) or any(m in raw for m in ("请求频率过高", "限流", "frequent", "rate limit")):
                last_err = MiaoXiangError(429, "触发频率限制")
                time.sleep(1.2 * (attempt + 1))
                continue
            raise MiaoXiangError(e.code, f"HTTP {e.code}", raw) from e
        except Exception as e:
            last_err = e
            time.sleep(0.8 * (attempt + 1))
    raise MiaoXiangError(-1, f"重试 {retries} 次仍失败: {last_err}")

def _check_biz(res: dict) -> dict:
    code = res.get("code")
    data = res.get("data") or {}
    bcode = data.get("code", code)
    if res.get("success") is False or (isinstance(bcode, int) and bcode != 0):
        msg = res.get("message") or data.get("message") or "未知错误"
        raise MiaoXiangError(bcode, str(msg), res)
    return res

CANON = {
    "SECURITY_CODE": "code", "SECURITY_SHORT_NAME": "name", "NEWEST_PRICE": "price",
    "CHG": "chg", "PCHG": "pchg", "010000_HLP": "profit_ratio",
    "010000_CMFB_461_JZD90": "chip_conc_90", "010000_TURNOVER_RATE": "turnover_rate",
    "010000_LIANGBI": "volume_ratio", "010000_VOLUME": "volume", "010000_TRADING_VOLUMES": "amount",
    "010000_TOAL_MARKET_VALUE": "total_mv", "010000_PEAK_PRICE": "high", "010000_BOTTOM_PRICE": "low",
    "010000_DURATION_LIMIT_UP": "limit_up_count",
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

def _pick_result(d: dict):
    cands = [(_dig(d, "allResults", "result"), "allResults.result"), (_dig(d, "result"), "result"), (_dig(d, "allResults"), "allResults")]
    for node, src in cands:
        if isinstance(node, dict) and (node.get("dataList") or node.get("columns")):
            return node.get("columns") or [], node.get("dataList") or [], node.get("total"), src
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
            if field: canon[field] = v
            else:
                title = title_of.get(base, base)
                if base in ("CHOICE_INNER_CODE", "IN_OPTIONAL", "SERIAL"): continue
                extra[f"{title}@{meta}" if meta else title] = v
        for f in ("price", "chg", "pchg", "profit_ratio", "chip_conc_90", "turnover_rate", "volume_ratio"):
            if f in canon: canon[f] = to_num(canon[f])
        if "limit_up_count" in canon: canon["limit_up_count"] = to_num(canon["limit_up_count"])
        canon["extra"] = extra
        out_rows.append(canon)
    return {"total": total if total is not None else len(out_rows), "rows": out_rows}

def stock_screen(keyword: str, api_key: str = "") -> dict:
    body = {"keyword": keyword, "pageNo": 1, "pageSize": 50}
    res = _check_biz(call("/api/claw/stock-screen", body, api_key=api_key))
    d = _dig(res, "data", "data") or {}
    return normalize(d)


# ==========================================
# 模块二：东财K线代理 + 本地三层逻辑
# ==========================================
def fetch_eastmoney_kline(code: str, days: int = 30) -> list:
    """后端代理请求东方财富K线（解决前端CORS限制）"""
    prefix = "1" if code.startswith("6") else "0"
    secid = f"{prefix}.{code}"
    url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid, "fields1": "f1,f2,f3,f4,f5",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101", "fqt": "1", "beg": "0", "end": "20500000", "lmt": days
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        data = res.json()
        klines = data.get('data', {}).get('klines', [])
        result = []
        for line in klines:
            parts = line.split(",")
            result.append({
                "date": parts[0], "open": float(parts[1]), "close": float(parts[2]),
                "high": float(parts[3]), "low": float(parts[4]), "volume": float(parts[5])
            })
        return result
    except Exception as e:
        print(f"东财K线获取失败 {code}: {e}")
        return []

def analyze_stock(stock: dict) -> dict:
    code = stock.get('code')
    name = stock.get('name')
    price = stock.get('price', 0)
    chg = stock.get('chg', 0)
    limit_up = stock.get('limit_up_count', 0)
    profit_ratio = stock.get('profit_ratio', 0)

    klines = fetch_eastmoney_kline(code, 30)
    if not klines or len(klines) < 10:
        return {"code": code, "name": name, "score": 0, "rating": "数据不足", "error": "K线缺失", "timing_tags": [], "holding_tags": [], "hard_conditions": []}

    closes = [k['close'] for k in klines]
    volumes = [k['volume'] for k in klines]
    highs = [k['high'] for k in klines]
    lows = [k['low'] for k in klines]
    
    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else closes[-1]
    v_ma5 = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else volumes[-1]
    v_ma5_prev = sum(volumes[-10:-5]) / 5 if len(volumes) >= 10 else volumes[-1]
    today = klines[-1]
    yesterday = klines[-2] if len(klines) >= 2 else today
    
    # --- 第一层：硬条件判断 ---
    score = 0
    hard_conditions = []

    c1 = all(closes[-i] > ma5 for i in range(1, 6)) and price > ma5
    hard_conditions.append({"name": "连续5日上涨", "pass": c1, "desc": f"5日线 {ma5:.2f}"})
    if c1: score += 20

    c2 = (limit_up > 0)
    hard_conditions.append({"name": "30日内有过涨停", "pass": c2, "desc": f"涨停 {limit_up} 次"})
    if c2: score += 20

    c3 = (price >= ma5)
    hard_conditions.append({"name": "收盘不破5日线", "pass": c3, "desc": f"现价 {price} vs MA5 {ma5:.2f}"})
    if c3: score += 20

    vol_ratio_5d = v_ma5_prev > 0 and (v_ma5 / v_ma5_prev) or 0
    c4 = (1.3 <= vol_ratio_5d <= 1.8)
    hard_conditions.append({"name": "堆量成交量", "pass": c4, "desc": f"5日均量比 {vol_ratio_5d:.2f}"})
    if c4: score += 20

    c5 = (60 <= profit_ratio <= 85)
    hard_conditions.append({"name": "底部筹码不动", "pass": c5, "desc": f"获利盘 {profit_ratio}%"})
    if c5: score += 20

    # --- 第二层：买卖时机 ---
    timing_tags = []
    if chg < -3 and price > today['low'] and price > yesterday['low']:
        timing_tags.append({"type": "buy", "text": "🟢 援军战法：重挫企稳"})
    if chg < 0 and (today['high'] - price) > (price - today['low']) * 1.5:
        timing_tags.append({"type": "buy", "text": "🟢 反转阴线"})
    if (today['high'] - price) > price * 0.03 and price > ma5:
        timing_tags.append({"type": "watch", "text": "🟡 仙人指路"})
    if chg < -3 and price < ma5:
        timing_tags.append({"type": "risk", "text": "🔴 破位阴线"})
    if not timing_tags:
        timing_tags.append({"type": "watch", "text": "🟡 暂无明确时机信号"})

    # --- 第三层：持股/卖出 ---
    holding_tags = []
    if today['high'] > yesterday['high'] and today['low'] > yesterday['low'] and today['close'] > yesterday['close']:
        holding_tags.append({"type": "hold", "text": "🟢 持股：高点高 + 低点高 + 收盘高"})
    elif today['high'] <= yesterday['high']:
        holding_tags.append({"type": "sell", "text": "🔴 卖出提示：高点不创新高"})
    else:
        holding_tags.append({"type": "hold", "text": "🟡 震荡：等待方向"})

    stock['score'] = score
    stock['rating'] = 'S' if score == 100 else ('A' if score >= 80 else ('B' if score >= 60 else 'C'))
    stock['hard_conditions'] = hard_conditions
    stock['timing_tags'] = timing_tags
    stock['holding_tags'] = holding_tags
    return stock

def local_filter_liumei(candidates: list[dict]) -> dict:
    final_data = []
    data_insufficient = []
    
    for stock in candidates[:30]:
        analyzed = analyze_stock(stock)
        if analyzed.get('score') == 0 and analyzed.get('error') == 'K线缺失':
            data_insufficient.append(analyzed)
        else:
            final_data.append(analyzed)
    
    final_data.sort(key=lambda x: x['score'], reverse=True)
    
    strict_hits = [s for s in final_data if s['score'] == 100]
    # 放宽标准，让60分以上都能展示
    near_hits = [s for s in final_data if s['score'] >= 60]
    
    return {
        "strict_hits": strict_hits[:3],
        "near_hits": near_hits[:5],
        "data_insufficient": data_insufficient[:5],
        "all_data": final_data
    }


# ==========================================
# 模块三：FastAPI Web 服务 (无状态代理)
# ==========================================
app = FastAPI(title="Yaogu-Radar API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ScreenRequest(BaseModel):
    mv_min: int = 30
    mv_max: int = 300
    api_key: str = ""

@app.post("/api/screen")
async def screen_stocks(req: ScreenRequest):
    try:
        kw = f"非ST，总市值{req.mv_min}亿到{req.mv_max}亿，近30日内有涨停"
        coarse_data = stock_screen(kw, api_key=req.api_key)
        result = local_filter_liumei(coarse_data.get('rows', []))
        
        return {
            "code": 200, "msg": "success", 
            "total_strict": len(result['strict_hits']),
            "total_near": len(result['near_hits']),
            "strict_hits": result['strict_hits'],
            "near_hits": result['near_hits'],
            "data_insufficient": result['data_insufficient']
        }
    except MiaoXiangError as e:
        raise HTTPException(status_code=400, detail=f"妙想API调用失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"内部计算错误: {str(e)}")

# 挂载静态文件（前端页面）
app.mount("/", StaticFiles(directory=".", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)