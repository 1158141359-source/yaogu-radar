import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("MX_API_URL", "https://mkapi2.dfcfs.com/finskillshub").rstrip("/")
MIN_INTERVAL = 0.35          # 单次请求最短间隔(秒)
_last_call = 0.0
CALL_COUNT = 0

RATE_LIMIT_MARKERS = ("请求频率过高", "请求过于频繁", "操作过于频繁",
                      "too many requests", "frequent", "rate limit", "限流")
QUOTA_MARKERS = ("调用次数已达到上限", "进入休眠", "quota", "次数已达上限")

# 业务错误码(实测)
ERR_CODES = {
    113: "今日调用次数已达上限",
    114: "apikey 无效或已失效",
    115: "请求未携带 apikey",
}


class MiaoXiangError(Exception):
    def __init__(self, code, message, raw=None):
        self.code = code
        self.message = message
        self.raw = raw
        super().__init__(f"[{code}] {message}")


# ------------------------------------------------------------------ 传输层

def _throttle():
    global _last_call
    gap = time.monotonic() - _last_call
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last_call = time.monotonic()


def call(endpoint: str, body: dict, *, api_key: str | None = None,
         retries: int = 3, timeout: float = 25.0) -> dict:
    """POST 一个妙想接口,返回原始 JSON。"""
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
            method="POST",
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
    """业务层校验:code 非 0 直接抛,方便上层统一处理。"""
    code = res.get("code")
    data = res.get("data") or {}
    # 业务码可能出现在 data.code
    bcode = data.get("code", code)
    if res.get("success") is False or (isinstance(bcode, int) and bcode != 0):
        msg = res.get("message") or data.get("message") or "未知错误"
        if isinstance(bcode, int) and bcode in ERR_CODES:
            msg = ERR_CODES[bcode]
        raise MiaoXiangError(bcode, str(msg), res)
    return res


# ------------------------------------------------------------ 结构 / 字段

# 实测确认的核心字段映射(前缀 010000_ 是妙想的指标命名空间)
CANON = {
    "SECURITY_CODE": "code",
    "SECURITY_SHORT_NAME": "name",
    "MARKET_SHORT_NAME": "market",
    "NEWEST_PRICE": "price",
    "CHG": "chg",
    "PCHG": "pchg",
    "010000_HLP": "profit_ratio",             # 获利盘(%)  ← C6 原生字段
    "010000_CMFB_461_JZD90": "chip_conc_90",  # 90%筹码集中度(%) ← 筹码峰原生字段
    "010000_TURNOVER_RATE": "turnover_rate",
    "010000_LIANGBI": "volume_ratio",
    "010000_VOLUME": "volume",
    "010000_TRADING_VOLUMES": "amount",
    "010000_TOAL_MARKET_VALUE": "total_mv",
    "010000_CIRCULATION_MARKET_VALUE": "float_mv",
    "010000_PE_D": "pe",
    "010000_PB": "pb",
    "010000_PEAK_PRICE": "high",
    "010000_BOTTOM_PRICE": "low",
    "010000_DURATION_LIMIT_UP": "limit_up_count",   # 涨停次数 ← C2 原生字段
    "010000_JX": "ma",
    "010000_CUSTOM_IFSTSTOCK_IFSTSTOCK_": "is_st",
}

_KEY_RE = re.compile(r"^(?P<base>[A-Za-z0-9_]+?)(?:<\d+>)?(?:\{(?P<meta>[^}]*)\})?$")
_UNIT_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*(亿|万|%)?")


def split_key(k: str) -> tuple[str, str]:
    """'010000_HLP<70>{2026-10-08}' → ('010000_HLP', '2026-10-08')"""
    m = _KEY_RE.match(k or "")
    if not m:
        return k, ""
    return m.group("base"), (m.group("meta") or "")


def to_num(v):
    """'270.18亿' → 2.7018e10 ; '16.05' → 16.05 ; '否' → None"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _UNIT_RE.search(str(v))
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2)
    if unit == "亿":
        n *= 1e8
    elif unit == "万":
        n *= 1e4
    return n


def _dig(node, *path):
    for p in path:
        if not isinstance(node, dict):
            return None
        node = node.get(p)
    return node


def _parse_md_table(md: str):
    """兜底:partialResults 的 Markdown 表格 → (columns, rows)"""
    if not md or "|" not in md:
        return [], []
    lines = [ln.strip() for ln in md.strip().splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return [], []
    def cells(ln):
        return [c.strip() for c in ln.strip("|").split("|")]
    headers = cells(lines[0])
    rows = []
    for ln in lines[1:]:
        cs = cells(ln)
        if cs and set("".join(cs)) <= set("-: "):
            continue
        rows.append(dict(zip(headers, cs)))
    return headers, rows


def _pick_result(d: dict):
    """按优先级找结果节点。返回 (columns, dataList, total, source)"""
    cands = [
        (_dig(d, "allResults", "result"), "allResults.result"),
        (_dig(d, "result"), "result"),
        (_dig(d, "allResults"), "allResults"),
    ]
    for node, src in cands:
        if isinstance(node, dict) and (node.get("dataList") or node.get("columns")):
            return node.get("columns") or [], node.get("dataList") or [], \
                   node.get("total"), src
    # 兜底:Markdown 表格
    md = d.get("partialResults")
    if isinstance(md, str) and "|" in md:
        cols, rows = _parse_md_table(md)
        if rows:
            columns = [{"title": h, "key": h} for h in cols]
            return columns, rows, len(rows), "partialResults(md)"
    return [], [], 0, "none"


def normalize(d: dict) -> dict:
    """把妙想返回的 data.data 段规整成干净结果。"""
    columns, data_list, total, source = _pick_result(d)

    # 中文标题映射:base_key -> title
    title_of = {}
    for c in columns:
        if not isinstance(c, dict):
            continue
        base, _ = split_key(c.get("key") or "")
        if base:
            title_of.setdefault(base, c.get("title") or base)

    out_rows = []
    for row in data_list:
        if not isinstance(row, dict):
            continue
        canon, extra, meta_date = {}, {}, ""
        for k, v in row.items():
            base, meta = split_key(k)
            if meta and len(meta) >= 8 and not meta_date:
                meta_date = meta
            field = CANON.get(base)
            if field:
                if field == "ma":        # 均线是多日期序列,单独收进 extra
                    extra[f"{title_of.get(base, base)}@{meta}"] = v
                else:
                    canon[field] = v
            else:
                title = title_of.get(base, base)
                if base in ("CHOICE_INNER_CODE", "IN_OPTIONAL", "SERIAL", "MARKET_SHORT_NAME"):
                    continue
                extra[f"{title}@{meta}" if meta else title] = v
        # 数值化常用项
        for f in ("price", "chg", "pchg", "profit_ratio", "chip_conc_90",
                  "turnover_rate", "volume_ratio", "pe", "pb", "high", "low"):
            if f in canon:
                canon[f] = to_num(canon[f])
        for f in ("total_mv", "float_mv", "volume", "amount"):
            if f in canon:
                canon[f + "_raw"] = canon[f]
                canon[f] = to_num(canon[f])
        if "limit_up_count" in canon:
            canon["limit_up_count"] = to_num(canon["limit_up_count"])
        canon["extra"] = extra
        out_rows.append(canon)

    tc = d.get("totalCondition")
    if isinstance(tc, dict):
        tc_desc, tc_cnt = tc.get("describe"), tc.get("stockCount")
    elif isinstance(tc, str):
        tc_desc, tc_cnt = tc, None
    else:
        tc_desc, tc_cnt = None, None
    if not tc_desc:
        tc_desc = _dig(d, "allResults", "totalCondition", "describe")

    return {
        "total": total if total is not None else len(out_rows),
        "condition": tc_desc,
        "condition_list": d.get("responseConditionList") or [],
        "data_date": None,     # 由调用方从 key meta 里取
        "source": source,
        "rows": out_rows,
    }


# ------------------------------------------------------------------ 业务

def stock_screen(keyword: str, page_no: int = 1, page_size: int = 50,
                 api_key: str | None = None) -> dict:
    """自然语言选股。返回规整结果。"""
    body = {"keyword": keyword}
    if page_no != 1:
        body["pageNo"] = page_no
    if page_size:
        body["pageSize"] = page_size
    res = _check_biz(call("/api/claw/stock-screen", body, api_key=api_key))
    d = _dig(res, "data", "data") or {}
    out = normalize(d)
    out["query"] = keyword
    # 数据日期:从列 key 的 meta 里取
    for c in (d.get("allResults", {}) or {}).get("result", {}).get("columns", []) or []:
        _, meta = split_key(c.get("key") or "")
        if len(meta) == 10 and meta[4] == "-":
            out["data_date"] = meta
            break
    return out


def query(tool_query: str, api_key: str | None = None) -> dict:
    """自然语言查数(行情/财务/关系)。"""
    res = _check_biz(call("/api/claw/query", {"toolQuery": tool_query}, api_key=api_key))
    return _dig(res, "data", "data") or {}


def news_search(q: str, api_key: str | None = None) -> dict:
    """金融资讯搜索(板块热度/题材用)。"""
    res = _check_biz(call("/api/claw/news-search", {"query": q}, api_key=api_key))
    return _dig(res, "data", "data") or {}


# ------------------------------------------------- 柚子六脉:粗筛条件构造

def liumei_coarse_keyword(*, mv_min: int = 30, mv_max: int = 300,
                          limit_up_days: int = 15, exclude_st: bool = True,
                          profit_min: float | None = None,
                          profit_max: float | None = None,
                          min_turnover: float | None = None,
                          min_volume_ratio: float | None = None,
                          ma_up: bool = True, chip: bool = True) -> str:
    """把柚子六脉的核心硬条件拼成一句自然语言,一次调用拿回候选池。

    ⚠️ 妙想是「条件式取列」:提问里点到哪个指标,返回里才有哪个字段。
    所以这里把六脉要用的字段全部点名,一次调用拿齐,别分多次打(省额度)。

    细算(C1/C3/C5 等)放本地,妙想只负责粗筛。
    """
    parts = []
    if exclude_st:
        parts.append("非ST")
    parts.append(f"总市值{mv_min}亿到{mv_max}亿")
    parts.append(f"近{limit_up_days}日内有涨停")
    if ma_up:
        parts.append("5日均线向上")
    if profit_min is not None or profit_max is not None:
        lo = f"{profit_min}%" if profit_min is not None else "0%"
        hi = f"{profit_max}%" if profit_max is not None else "100%"
        parts.append(f"获利盘在{lo}到{hi}之间")
    if chip:
        parts.append("筹码集中度")          # 只点名取列,不当硬条件
    if min_turnover is not None:
        parts.append(f"今日换手率大于{min_turnover}%")
    if min_volume_ratio is not None:
        parts.append(f"今日量比大于{min_volume_ratio}")
    return "，".join(parts)


def screen_liumei(**kw) -> dict:
    """一键粗筛,返回候选池。"""
    return stock_screen(liumei_coarse_keyword(**kw))


# ------------------------------------------------------------------ CLI

def _flatten(rows) -> tuple[list[str], list[dict]]:
    """把 rows 展平成 CSV 可写的表格(extra 打平成一列)。"""
    heads, seen = [], set()
    flat = []
    for r in rows:
        f = {}
        for k, v in r.items():
            if k == "extra":
                for ek, ev in (v or {}).items():
                    f[ek] = ev
            else:
                f[k] = v
        for k in f:
            if k not in seen:
                seen.add(k)
                heads.append(k)
        flat.append(f)
    return heads, flat


def _main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    rest = [a for a in argv[2:] if not a.startswith("--")]
    csv_path = None
    if "--csv" in argv:
        i = argv.index("--csv")
        if i + 1 < len(argv):
            csv_path = argv[i + 1]
            rest = [a for a in rest if a != csv_path]

    try:
        if cmd == "screen":
            r = stock_screen(" ".join(rest))
            print(f"条件: {r['condition']}")
            print(f"数据日期: {r['data_date']}   命中: {r['total']}   取数路径: {r['source']}")
            for row in r["rows"][:30]:
                tag = f"{row.get('code')} {row.get('name')}"
                bits = []
                for f, label in (("price", "现价"), ("chg", "涨幅"),
                                 ("profit_ratio", "获利盘"), ("chip_conc_90", "筹码集中度"),
                                 ("turnover_rate", "换手"), ("volume_ratio", "量比"),
                                 ("limit_up_count", "涨停次数")):
                    if row.get(f) is not None:
                        bits.append(f"{label}{row[f]}")
                print("  ", tag, "|", " ".join(bits))
                if row.get("extra"):
                    print("      extra:", json.dumps(row["extra"], ensure_ascii=False))
            if csv_path:
                heads, flat = _flatten(r["rows"])
                with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
                    w = csv.DictWriter(fh, fieldnames=heads, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(flat)
                print(f"\n已写出 CSV: {csv_path}  ({len(flat)} 行)")
        elif cmd == "liumei":
            r = screen_liumei()
            print(json.dumps({k: r[k] for k in ("condition", "total", "data_date")},
                             ensure_ascii=False, indent=1))
            for row in r["rows"]:
                print("  ", row.get("code"), row.get("name"),
                      "获利盘", row.get("profit_ratio"),
                      "筹码集中度", row.get("chip_conc_90"))
        elif cmd == "query":
            print(json.dumps(query(" ".join(rest)), ensure_ascii=False, indent=1)[:4000])
        elif cmd == "news":
            print(json.dumps(news_search(" ".join(rest)), ensure_ascii=False, indent=1)[:4000])
        elif cmd == "quota":
            print(f"本进程已发起 {CALL_COUNT} 次调用(接口未开放额度查询口)")
        else:
            print(__doc__)
            return 1
    except MiaoXiangError as e:
        print(f"调用失败: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
