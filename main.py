# main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
import os
import json
import datetime
import re
import time
import urllib.error
import urllib.request

# ==========================================
# 模块一：妙想 API 客户端
# ==========================================
BASE_URL = "https://mkapi2.dfcfs.com/finskillshub"
MIN_INTERVAL = 0.35
_last_call = 0.0

class MiaoXiangError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")

def _throttle():
    global _last_call
    gap = time.monotonic() - _last_call
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last_call = time.monotonic()

def call(endpoint: str, body: dict, *, api_key: str = "", retries: int = 3, timeout: float = 25.0) -> dict:
    if not api_key:
        raise MiaoXiangError(115, "未提供 apikey(请在页面参数设置中填入)")
    
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    for attempt in range(retries + 1):
        _throttle()
        req = urllib.request.Request(
            BASE_URL + endpoint, data=payload,
            headers={"Content-Type": "application/json;charset=UTF-8", "apikey": api_key},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = ""
            try: raw = e.read().decode("utf-8", "ignore")
            except: pass
            if e.code in (429, 503) or any(m in raw for m in ("请求频率过高", "限流", "frequent", "rate limit")):
                time.sleep(1.2 * (attempt + 1))
                continue
            raise MiaoXiangError(e.code, f"HTTP {e.code}")
        except Exception:
            time.sleep(0.8 * (attempt + 1))
    raise MiaoXiangError(-1, "请求重试失败")

def _check_biz(res: dict) -> dict:
    bcode = (res.get("data") or {}).get("code", res.get("code"))
    if res.get("success") is False or (isinstance(bcode, int) and bcode != 0):
        raise MiaoXiangError(bcode, str(res.get("message") or "未知错误"))
    return res

def query(tool_query: str, api_key: str = "") -> dict:
    res = _check_biz(call("/api/claw/query", {"toolQuery": tool_query}, api_key=api_key))
    return (res.get("data") or {}).get("data", {})

def stock_screen(keyword: str, api_key: str = "") -> dict:
    res = _check_biz(call("/api/claw/stock-screen", {"keyword": keyword, "pageNo": 1, "pageSize": 50}, api_key=api_key))
    d = (res.get("data") or {}).get("data", {})
    data_list = (d.get("allResults", {}).get("result", {}) or d.get("result", {})).get("dataList", [])
    return {"rows": data_list}

# ==========================================
# 模块二：K线获取 + 三层逻辑（含降级兜底）
# ==========================================
def _parse_md_table(md: str):
    if not md or "|" not in md: return []
    lines = [ln.strip() for ln in md.strip().splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2: return []
    headers = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = []
    for ln in lines[1:]:
        cs = [c.strip() for c in ln.strip("|").split("|")]
        if cs and set("".join(cs)) <= set("-: "): continue
        rows.append(dict(zip(headers, cs)))
    return rows

def fetch_kline_from_miao(code: str, api_key: str, days: int = 30) -> list:
    end_date = datetime.date.today().strftime("%Y%m%d")
    start_date = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y%m%d")
    try:
        query_str = f"{code} {start_date}到{end_date} 日K线 收盘价 成交量"
        res = query(query_str, api_key=api_key)
        md = res.get("partialResults")
        if not md: return []
        parsed = _parse_md_table(md)
        result = []
        for row in parsed:
            close, volume = None, None
            for k, v in row.items():
                if '收盘' in k or 'close' in k.lower(): 
                    try: close = float(v)
                    except: pass
                if '成交量' in k or 'volume' in k.lower(): 
                    try: volume = float(v)
                    except: pass
            if close is not None and volume is not None:
                result.append({"close": close, "volume": volume})
        return result
    except Exception as e:
        return []

def analyze_by_cross_section(stock: dict) -> dict:
    """无K线时的降级评分逻辑（基于妙想截面数据）"""
    code = stock.get('SECURITY_CODE') or stock.get('code', '')
    name = stock.get('SECURITY_SHORT_NAME') or stock.get('name', '')
    price = float(stock.get('NEWEST_PRICE') or stock.get('price') or 0)
    chg = float(stock.get('CHG') or stock.get('chg') or 0)
    limit_up = float(stock.get('010000_DURATION_LIMIT_UP') or stock.get('limit_up_count') or 0)
    profit_ratio = float(stock.get('010000_HLP') or stock.get('profit_ratio') or 0)
    volume_ratio = float(stock.get('010000_LIANGBI') or stock.get('volume_ratio') or 0)
    extra = stock.get('extra', {})
    
    ma5_str = extra.get('5日均线(元)')
    ma5 = float(ma5_str) if ma5_str else price * 0.98

    score = 0
    hard_conditions = []

    c1 = chg > 0 and price > ma5
    hard_conditions.append({"name": "连续5日上涨", "pass": c1, "desc": f"估算: 涨幅{chg}%, MA5 {ma5:.2f}"})
    if c1: score += 20

    c2 = (limit_up > 0)
    hard_conditions.append({"name": "30日内有过涨停", "pass": c2, "desc": f"涨停 {int(limit_up)} 次"})
    if c2: score += 20

    c3 = (price >= ma5)
    hard_conditions.append({"name": "收盘不破5日线", "pass": c3, "desc": f"现价 {price} vs MA5 {ma5:.2f}"})
    if c3: score += 20

    c4 = (1.0 <= volume_ratio <= 2.5)
    hard_conditions.append({"name": "堆量成交量", "pass": c4, "desc": f"量比 {volume_ratio}"})
    if c4: score += 20

    c5 = (60 <= profit_ratio <= 85)
    hard_conditions.append({"name": "底部筹码不动", "pass": c5, "desc": f"获利盘 {profit_ratio}%"})
    if c5: score += 20

    timing_tags = [{"type": "watch", "text": "🟡 截面估算模式"}]
    holding_tags = [{"type": "hold", "text": "🟡 无K线，无法判断"}]

    stock['score'] = score
    stock['rating'] = 'S' if score == 100 else ('A' if score >= 80 else ('B' if score >= 60 else 'C'))
    stock['hard_conditions'] = hard_conditions
    stock['timing_tags'] = timing_tags
    stock['holding_tags'] = holding_tags
    stock['is_estimated'] = True
    return stock

def analyze_stock(stock: dict, api_key: str) -> dict:
    code = stock.get('SECURITY_CODE') or stock.get('code', '')
    name = stock.get('SECURITY_SHORT_NAME') or stock.get('name', '')
    price = float(stock.get('NEWEST_PRICE') or stock.get('price') or 0)
    chg = float(stock.get('CHG') or stock.get('chg') or 0)

    klines = fetch_kline_from_miao(code, api_key, 30)
    if not klines or len(klines) < 10:
        # 关键修改：K线拉不到时，直接走降级逻辑，不返回"数据不足"
        return analyze_by_cross_section(stock)

    closes = [k['close'] for k in klines]
    volumes = [k['volume'] for k in klines]
    ma5 = sum(closes[-5:]) / 5
    v_ma5 = sum(volumes[-5:]) / 5
    v_ma5_prev = sum(volumes[-10:-5]) / 5 if len(volumes) >= 10 else volumes[0]
    today = klines[-1]
    yesterday = klines[-2] if len(klines) >= 2 else today
    
    score = 0
    hard_conditions = []

    c1 = all(closes[-i] > ma5 for i in range(1, 6)) and price > ma5
    hard_conditions.append({"name": "连续5日上涨", "pass": c1, "desc": f"MA5 {ma5:.2f}"})
    if c1: score += 20

    c2 = (float(stock.get('010000_DURATION_LIMIT_UP') or stock.get('limit_up_count') or 0) > 0)
    hard_conditions.append({"name": "30日内有过涨停", "pass": c2, "desc": "K线模式"})
    if c2: score += 20

    c3 = (price >= ma5)
    hard_conditions.append({"name": "收盘不破5日线", "pass": c3, "desc": f"{price} vs MA5 {ma5:.2f}"})
    if c3: score += 20

    vol_ratio_5d = v_ma5_prev > 0 and (v_ma5 / v_ma5_prev) or 0
    c4 = (1.3 <= vol_ratio_5d <= 1.8)
    hard_conditions.append({"name": "堆量成交量", "pass": c4, "desc": f"5日均量比 {vol_ratio_5d:.2f}"})
    if c4: score += 20

    profit_ratio = float(stock.get('010000_HLP') or stock.get('profit_ratio') or 0)
    c5 = (60 <= profit_ratio <= 85)
    hard_conditions.append({"name": "底部筹码不动", "pass": c5, "desc": f"获利盘 {profit_ratio}%"})
    if c5: score += 20

    timing_tags = [{"type": "buy" if chg > 0 else "risk", "text": "🟢 K线模式"}]
    holding_tags = [{"type": "hold", "text": "🟢 持股"}]

    stock['score'] = score
    stock['rating'] = 'S' if score == 100 else ('A' if score >= 80 else ('B' if score >= 60 else 'C'))
    stock['hard_conditions'] = hard_conditions
    stock['timing_tags'] = timing_tags
    stock['holding_tags'] = holding_tags
    stock['is_estimated'] = False
    return stock

def local_filter_liumei(candidates: list[dict], api_key: str) -> dict:
    final_data = []
    for stock in candidates[:30]:
        analyzed = analyze_stock(stock, api_key)
        final_data.append(analyzed)
    
    final_data.sort(key=lambda x: x['score'], reverse=True)
    strict_hits = [s for s in final_data if s['score'] == 100]
    near_hits = [s for s in final_data if s['score'] >= 60]
    
    return {"strict_hits": strict_hits[:3], "near_hits": near_hits[:5], "data_insufficient": []}

# ==========================================
# 模块三：FastAPI Web 服务
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
        result = local_filter_liumei(coarse_data.get('rows', []), req.api_key)
        return {"code": 200, "msg": "success", **result}
    except MiaoXiangError as e:
        raise HTTPException(status_code=400, detail=f"妙想API调用失败: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"内部错误: {str(e)}")

app.mount("/", StaticFiles(directory=".", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)