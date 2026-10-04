import os, re, json, time, math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

APP = FastAPI(title='妖股雷达')
TZ = ZoneInfo('Asia/Shanghai')
TIMEOUT = 12
MX_URL = 'https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen'

session = requests.Session()
session.headers.update({
    'User-Agent':
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
        'AppleWebKit/605.1.15 Mobile/15E148'
})

CACHE: Dict[str, Dict[str, Any]] = {}

QUERY = '''
筛选中国A股强势股票。
重点寻找近3个交易日出现过涨停、近期上过龙虎榜、
总市值不超过300亿元、非ST的股票。
尽量返回至少30只候选。
输出股票代码、股票名称、涨跌幅、换手率、总市值、
龙虎榜净额、近3日涨停次数。
只返回股票，不要解释。
'''


def get_json(url, params=None):
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def num(v, default=0.0):
    if v is None:
        return default

    if isinstance(v, (int, float)):
        return float(v)

    s = (
        str(v)
        .replace(',', '')
        .replace('%', '')
        .replace('亿', '')
        .replace('万', '')
        .strip()
    )

    try:
        return float(s)
    except Exception:
        return default


def clean_code(v):
    m = re.search(r'\d{6}', str(v or ''))
    return m.group(0) if m else ''


def secid(code):
    return ('1.' if code.startswith(('5', '6', '68', '9')) else '0.') + code


def trade_date(s=None):
    if not s:
        return datetime.now(TZ).strftime('%Y%m%d')

    s = str(s).replace('-', '')

    if re.fullmatch(r'\d{8}', s):
        return s

    return datetime.now(TZ).strftime('%Y%m%d')


# ============================================================
# 交易日
# ============================================================

def get_trading_dates(end_date: str, count=10):
    end_date = trade_date(end_date)

    p = {
        'secid': '1.000300',
        'klt': '101',
        'fqt': '0',
        'beg': '19900101',
        'end': end_date,
        'lmt': '500',
        'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2':
            'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'
    }

    d = get_json(
        'https://push2his.eastmoney.com/api/qt/stock/kline/get',
        p
    )

    rows = (
        ((d or {}).get('data') or {}).get('klines') or []
    )

    dates = []

    for x in rows:
        ds = str(x).split(',')[0].replace('-', '')

        if re.fullmatch(r'\d{8}', ds) and ds <= end_date:
            dates.append(ds)

    dates = sorted(set(dates), reverse=True)

    if not dates:
        dt = datetime.strptime(end_date, '%Y%m%d')

        while len(dates) < count:
            if dt.weekday() < 5:
                dates.append(dt.strftime('%Y%m%d'))

            dt -= timedelta(days=1)

    return dates[:count]


# ============================================================
# 历史K线
# ============================================================

def get_kline(code, end_date, limit=120):

    p = {
        'secid': secid(code),
        'klt': '101',
        'fqt': '0',
        'beg': '19900101',
        'end': trade_date(end_date),
        'lmt': str(limit),
        'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2':
            'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'
    }

    d = get_json(
        'https://push2his.eastmoney.com/api/qt/stock/kline/get',
        p
    )

    rows = (
        ((d or {}).get('data') or {}).get('klines') or []
    )

    out = []

    for x in rows:

        a = str(x).split(',')

        if len(a) >= 11:

            out.append({
                'date': a[0].replace('-', ''),
                'open': num(a[1]),
                'close': num(a[2]),
                'high': num(a[3]),
                'low': num(a[4]),
                'volume': num(a[5]),
                'amount': num(a[6]),
                'pct': num(a[8]),
                'turnover': num(a[10])
            })

    return out


# ============================================================
# 实时行情
# ============================================================

def get_quotes(codes: List[str]):

    codes = list(
        dict.fromkeys(
            [
                clean_code(x)
                for x in codes
                if clean_code(x)
            ]
        )
    )

    if not codes:
        return {}

    out = {}

    for i in range(0, len(codes), 80):

        batch = codes[i:i + 80]

        p = {
            'pn': '1',
            'pz': str(len(batch)),
            'po': '1',
            'np': '1',
            'fltt': '2',
            'invt': '2',
            'fid': 'f3',
            'fields':
                'f2,f3,f8,f12,f14,f20,f21'
        }

        p['fs'] = ','.join(
            (
                'm:1~' + c
                if c.startswith(('6', '68'))
                else 'm:0~' + c
            )
            for c in batch
        )

        d = get_json(
            'https://push2.eastmoney.com/api/qt/ulist.np/get',
            p
        )

        diff = (
            ((d or {}).get('data') or {}).get('diff') or []
        )

        if isinstance(diff, dict):
            diff = list(diff.values())

        for x in diff:

            c = clean_code(x.get('f12'))

            if c:

                out[c] = {
                    'price': num(x.get('f2')),
                    'pct': num(x.get('f3')),
                    'turnover': num(x.get('f8')),
                    'name': str(x.get('f14') or ''),
                    'market_cap': num(x.get('f20')) / 1e8,
                    'float_cap': num(x.get('f21')) / 1e8
                }

    # 缺失数据时逐股补充
    for c in codes:

        if c not in out:

            d = get_json(
                'https://push2.eastmoney.com/api/qt/stock/get',
                {
                    'secid': secid(c),
                    'fields':
                        'f43,f57,f58,f60,f116,f117,f168,f170'
                }
            )

            x = (d or {}).get('data') or {}

            if x:

                price = num(x.get('f43'))

                if price > 1000:
                    price /= 100

                pct = num(x.get('f170'))

                if abs(pct) > 100:
                    pct /= 100

                turnover = num(x.get('f168'))

                if turnover > 100:
                    turnover /= 100

                out[c] = {
                    'price': price,
                    'pct': pct,
                    'turnover': turnover,
                    'name': str(x.get('f58') or ''),
                    'market_cap': num(x.get('f116')) / 1e8,
                    'float_cap': num(x.get('f117')) / 1e8
                }

    return out


# ============================================================
# 东方财富涨停池
# ============================================================

def get_zt_pool(date):

    p = {
        'ut': '7eea3edcaed734bea9cbfc24409ed989',
        'dpt': 'wz.ztzt',
        'Pageindex': '0',
        'pagesize': '6000',
        'sort': 'fbt:asc',
        'date': date
    }

    d = get_json(
        'https://push2ex.eastmoney.com/getTopicZTPool',
        p
    )

    pool = (
        ((d or {}).get('data') or {}).get('pool') or []
    )

    return pool if isinstance(pool, list) else []


def get_limit_counts(codes, dates):

    wanted = set(codes)

    counts = {
        c: 0
        for c in codes
    }

    for dt in dates:

        pool = get_zt_pool(dt)

        for x in pool:

            c = clean_code(
                x.get('c') or
                x.get('code')
            )

            if c in wanted:
                counts[c] += 1

    return counts


# ============================================================
# 龙虎榜
# ============================================================

def get_lhb(codes, dates):

    wanted = set(codes)

    out = {
        c: {
            'day_net': 0.0,
            'net10': 0.0,
            'days': 0,
            'on_board': False
        }
        for c in codes
    }

    if not dates:
        return out

    end = dates[0]
    beg = dates[-1]

    p = {
        'reportName':
            'RPT_DAILYBILLBOARD_DETAILSNEW',
        'columns':
            'ALL',
        'quoteColumns':
            '',
        'filter':
            f"(TRADE_DATE>='{beg}') "
            f"(TRADE_DATE<='{end}')",
        'pageNumber':
            '1',
        'pageSize':
            '5000',
        'sortTypes':
            '-1',
        'sortColumns':
            'TRADE_DATE',
        'source':
            'DataCenter',
        'client':
            'web'
    }

    d = get_json(
        'https://datacenter-web.eastmoney.com/api/data/v1/get',
        p
    )

    rows = (
        ((d or {}).get('result') or {}).get('data') or []
    )

    seen = {
        c: set()
        for c in codes
    }

    for x in rows:

        c = clean_code(
            x.get('SECURITY_CODE')
        )

        if c not in wanted:
            continue

        net = num(
            x.get('BILLBOARD_NET_AMT')
        ) / 1e8

        td = str(
            x.get('TRADE_DATE') or ''
        )[:10].replace('-', '')

        out[c]['net10'] += net

        if td == end:
            out[c]['day_net'] += net

        seen[c].add(td)

    for c in codes:

        out[c]['days'] = len(
            seen[c]
        )

        out[c]['on_board'] = (
            out[c]['days'] > 0
        )

    return out


# ============================================================
# 当前五档盘口
# ============================================================

def get_orderbook(code):

    d = get_json(
        'https://push2.eastmoney.com/api/qt/stock/get',
        {
            'secid': secid(code),
            'fields':
                'f2,'
                'f19,f20,'
                'f17,f18,'
                'f15,f16,'
                'f13,f14,'
                'f11,f12,'
                'f39,f40,'
                'f37,f38,'
                'f35,f36,'
                'f33,f34,'
                'f31,f32'
        }
    )

    x = (d or {}).get('data') or {}

    if not x:

        return {
            'buy': 0,
            'sell': 0,
            'imbalance': 0
        }

    # 买1-5
    buys = sum(
        num(x.get(price)) *
        num(x.get(vol)) *
        100
        for price, vol in [
            ('f19', 'f20'),
            ('f17', 'f18'),
            ('f15', 'f16'),
            ('f13', 'f14'),
            ('f11', 'f12')
        ]
    )

    # 卖1-5
    sells = sum(
        num(x.get(price)) *
        num(x.get(vol)) *
        100
        for price, vol in [
            ('f39', 'f40'),
            ('f37', 'f38'),
            ('f35', 'f36'),
            ('f33', 'f34'),
            ('f31', 'f32')
        ]
    )

    imbalance = (
        (buys - sells) /
        (buys + sells)
        if buys + sells
        else 0
    )

    return {
        'buy': buys / 1e8,
        'sell': sells / 1e8,
        'imbalance': imbalance
    }


# ============================================================
# 妙想
# ============================================================

def mx_candidates():

    key = os.getenv(
        'MX_APIKEY',
        ''
    ).strip()

    if not key:
        return []

    headers = {
        'Authorization':
            f'Bearer {key}',
        'Content-Type':
            'application/json'
    }

    payload = {
        'query': QUERY
    }

    try:

        r = session.post(
            MX_URL,
            headers=headers,
            json=payload,
            timeout=20
        )

        if r.status_code >= 400:
            return []

        j = r.json()

        text = json.dumps(
            j,
            ensure_ascii=False
        )

        arr = []

        if isinstance(j, dict):

            for k in (
                'data',
                'result',
                'items',
                'stocks',
                'rows'
            ):

                if isinstance(
                    j.get(k),
                    list
                ):
                    arr = j[k]
                    break

        if not arr and isinstance(j, list):
            arr = j

        out = []

        for x in arr:

            if isinstance(x, dict):

                c = clean_code(
                    x.get('code') or
                    x.get('stock_code') or
                    x.get('SECURITY_CODE')
                )

                n = (
                    x.get('name') or
                    x.get('stock_name') or
                    x.get('SECURITY_NAME_ABBR') or
                    ''
                )

                if c:

                    out.append({
                        'code': c,
                        'name': str(n)
                    })

        if out:
            return out

        found = re.findall(
            r'(?<!\d)(?:0|3|6)\d{5}(?!\d)',
            text
        )

        for c in found:

            if c not in [
                z['code']
                for z in out
            ]:

                out.append({
                    'code': c,
                    'name': ''
                })

        return out

    except Exception:
        return []


# ============================================================
# 妖股评分
# ============================================================

def score_stock(
    c,
    quote,
    hist,
    lhb,
    zt,
    order=None,
    mx=False
):

    pct = (
        quote['pct']
        if c['date'] == TODAY
        else hist.get('pct', 0)
    )

    turnover = (
        quote['turnover']
        if c['date'] == TODAY
        else hist.get('turnover', 0)
    )

    cap = quote.get(
        'market_cap',
        0
    )

    # 历史市值估算
    if (
        c['date'] != TODAY
        and hist.get('close')
        and quote.get('price')
    ):

        cap = (
            cap *
            hist['close'] /
            quote['price']
        )

    # ========================================================
    # 三个硬条件
    # ========================================================

    hard = []

    if zt >= 1:
        hard.append('3日涨停')

    if lhb['on_board']:
        hard.append('龙虎榜')

    if 0 < cap <= 300:
        hard.append('市值≤300亿')

    # ========================================================
    # 100分
    # ========================================================

    s = 0

    # 近3日涨停 20
    s += min(
        20,
        zt * 10
    )

    # 龙虎榜 15
    s += (
        15
        if lhb['on_board']
        else 0
    )

    # 市值 10
    s += (
        10
        if 0 < cap <= 300
        else 0
    )

    # 涨幅 15
    s += (
        15
        if pct >= 5
        else (
            10
            if pct >= 3
            else (
                5
                if pct >= 0
                else 0
            )
        )
    )

    # 换手率 15
    s += (
        15
        if turnover >= 20
        else (
            10
            if turnover >= 10
            else (
                5
                if turnover >= 5
                else 0
            )
        )
    )

    # 龙虎榜10日资金 10
    s += (
        10
        if lhb['net10'] > 1
        else (
            5
            if lhb['net10'] > 0
            else 0
        )
    )

    # 当前盘口 10
    if order:

        s += (
            10
            if order['imbalance'] > 0.20
            else (
                5
                if order['imbalance'] > 0
                else -3
            )
        )

    # 妙想增强 5
    s += (
        5
        if mx
        else 0
    )

    s = max(
        0,
        min(100, s)
    )

    # ========================================================
    # 分级
    # ========================================================

    tier = (
        'S'
        if len(hard) == 3 and s >= 80
        else (
            'A'
            if s >= 65
            else (
                'B'
                if s >= 50
                else 'C'
            )
        )
    )

    return {
        'score': s,
        'tier': tier,
        'hard': hard,
        'pct': pct,
        'turnover': turnover,
        'market_cap': cap
    }


# ============================================================
# 主扫描
# ============================================================

def build(target):

    global TODAY

    TODAY = datetime.now(
        TZ
    ).strftime('%Y%m%d')

    target = trade_date(target)

    # 最近10个交易日
    dates = get_trading_dates(
        target,
        10
    )

    # 最近3个交易日
    d3 = dates[:3]

    # ========================================================
    # 涨停池
    # ========================================================

    zt_pool = []

    for dt in d3:
        zt_pool += get_zt_pool(dt)

    ztcodes = {
        clean_code(
            x.get('c') or
            x.get('code')
        )
        for x in zt_pool
    }

    ztcodes.discard('')

    # ========================================================
    # 龙虎榜股票
    # ========================================================

    lhb_codes = set()

    p = {
        'reportName':
            'RPT_DAILYBILLBOARD_DETAILSNEW',
        'columns':
            'SECURITY_CODE,SECURITY_NAME_ABBR',
        'filter':
            f"(TRADE_DATE>='{dates[-1]}') "
            f"(TRADE_DATE<='{dates[0]}')",
        'pageNumber':
            '1',
        'pageSize':
            '5000',
        'sortTypes':
            '-1',
        'sortColumns':
            'TRADE_DATE',
        'source':
            'DataCenter',
        'client':
            'web'
    }

    d = get_json(
        'https://datacenter-web.eastmoney.com/api/data/v1/get',
        p
    )

    rows = (
        ((d or {}).get('result') or {}).get('data') or []
    )

    for x in rows:

        cc = clean_code(
            x.get('SECURITY_CODE')
        )

        if cc:
            lhb_codes.add(cc)

    # ========================================================
    # 妙想候选
    # ========================================================

    mx = (
        mx_candidates()
        if target == TODAY
        else []
    )

    mxcodes = {
        x['code']
        for x in mx
    }

    # ========================================================
    # 候选池
    # ========================================================

    candidates = sorted(
        ztcodes |
        lhb_codes |
        mxcodes
    )

    quotes = get_quotes(
        candidates
    )

    lhb = get_lhb(
        candidates,
        dates
    )

    zcounts = get_limit_counts(
        candidates,
        d3
    )

    mxmap = {
        x['code']:
            x.get('name', '')
        for x in mx
    }

    results = []

    # ========================================================
    # 每只股票计算
    # ========================================================

    for code in candidates:

        q = quotes.get(code, {})

        if not q:
            continue

        name = (
            q.get('name')
            or mxmap.get(code)
            or code
        )

        # 排除ST
        if (
            'ST' in name.upper()
            or '*ST' in name.upper()
        ):
            continue

        kl = get_kline(
            code,
            target,
            5
        )

        row = next(
            (
                x
                for x in kl
                if x['date'] == target
            ),
            None
        )

        if (
            target != TODAY
            and not row
        ):
            continue

        hist = (
            row
            or {
                'close':
                    q.get('price', 0),
                'pct':
                    q.get('pct', 0),
                'turnover':
                    q.get('turnover', 0)
            }
        )

        # 今日才使用当前五档盘口
        order = (
            get_orderbook(code)
            if target == TODAY
            else None
        )

        meta = {
            'date': target
        }

        lhb_data = lhb.get(
            code,
            {
                'day_net': 0,
                'net10': 0,
                'days': 0,
                'on_board': False
            }
        )

        sc = score_stock(
            meta,
            q,
            hist,
            lhb_data,
            zcounts.get(code, 0),
            order,
            code in mxcodes
        )

        results.append({

            'code':
                code,

            'name':
                name,

            'price':
                hist.get(
                    'close',
                    q.get('price', 0)
                ),

            'pct':
                sc['pct'],

            'turnover':
                sc['turnover'],

            'market_cap':
                sc['market_cap'],

            'lhb_day_net':
                lhb_data.get(
                    'day_net',
                    0
                ),

            'lhb_net10':
                lhb_data.get(
                    'net10',
                    0
                ),

            'lhb_days':
                lhb_data.get(
                    'days',
                    0
                ),

            'zt3':
                zcounts.get(
                    code,
                    0
                ),

            'order_buy':
                order['buy']
                if order
                else None,

            'order_sell':
                order['sell']
                if order
                else None,

            'order_imbalance':
                order['imbalance']
                if order
                else None,

            **sc

        })

    # ========================================================
    # 排序
    # ========================================================

    results.sort(
        key=lambda x: (
            x['score'],
            len(x['hard']),
            x['zt3'],
            x['lhb_net10'],
            x['pct']
        ),
        reverse=True
    )

    # 三个硬条件
    hard = [
        x
        for x in results
        if len(x['hard']) == 3
    ]

    return {

        'date':
            target,

        'trading_dates':
            dates,

        'source':
            (
                '东方财富公开行情 + '
                '龙虎榜/涨停历史 + 妙想增强'
                if mx
                else
                '东方财富公开行情 + '
                '龙虎榜/涨停历史'
            ),

        'count':
            len(results),

        'hard_count':
            len(hard),

        'top3':
            hard[:3],

        'ranking':
            results[:30],

        'note':
            '历史日期的市值按历史收盘价相对当前价格估算；'
            '历史集合竞价未伪造。'
            '今日盘口为当前五档委买委卖。'
    }


# ============================================================
# API
# ============================================================

@app.get('/api/scanner')
def scanner(
    date: str = Query(default='')
):

    target = trade_date(date)

    now = time.time()

    if (
        target in CACHE
        and
        now - CACHE[target]['ts'] < 60
    ):
        return CACHE[target]['data']

    data = build(target)

    CACHE[target] = {
        'ts': now,
        'data': data
    }

    return data


# ============================================================
# 手机网页
# ============================================================

HTML = '''
<!doctype html>

<html lang="zh-CN">

<head>

<meta charset="utf-8">

<meta
name="viewport"
content="width=device-width,initial-scale=1,maximum-scale=1"
>

<title>妖股雷达</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #080b10;
    color: #e8edf5;
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "PingFang SC",
        Arial,
        sans-serif;
}

header {
    padding: 18px 14px 10px;
    border-bottom: 1px solid #202733;
    position: sticky;
    top: 0;
    background: #080b10ee;
    backdrop-filter: blur(10px);
    z-index: 5;
}

.title {
    font-size: 24px;
    font-weight: 800;
}

.sub {
    font-size: 12px;
    color: #7f8a9b;
    margin-top: 5px;
}

.bar {
    display: flex;
    gap: 8px;
    margin-top: 12px;
}

.bar input,
.bar button {
    height: 40px;
    border: 1px solid #2b3442;
    border-radius: 8px;
    background: #10151d;
    color: #fff;
    padding: 0 12px;
}

.bar button {
    background: #c62828;
    border: 0;
    font-weight: 700;
}

.wrap {
    max-width: 1000px;
    margin: auto;
    padding: 12px;
}

.notice {
    font-size: 12px;
    color: #9ba7b7;
    background: #10151d;
    border: 1px solid #202936;
    padding: 10px;
    border-radius: 8px;
    margin-bottom: 12px;
}

.cards {
    display: grid;
    grid-template-columns:
        repeat(3, 1fr);
    gap: 10px;
}

.card {
    background: #0f141c;
    border: 1px solid #26303d;
    border-radius: 12px;
    padding: 13px;
}

.rank {
    font-size: 11px;
    color: #8d98a8;
}

.name {
    font-size: 19px;
    font-weight: 800;
    margin-top: 4px;
}

.code {
    font-size: 11px;
    color: #768294;
}

.score {
    font-size: 30px;
    font-weight: 900;
    margin-top: 6px;
}

.pct {
    font-size: 18px;
    font-weight: 800;
    color: #f04a4a;
}

.green {
    color: #35c878;
}

.tags {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 9px;
}

.tag {
    font-size: 11px;
    padding: 4px 6px;
    border-radius: 5px;
    background: #18202b;
    color: #aeb9c8;
}

.tag.hot {
    color: #ff6262;
}

.table {
    margin-top: 14px;
    background: #0f141c;
    border: 1px solid #26303d;
    border-radius: 12px;
    overflow: auto;
}

table {
    width: 100%;
    border-collapse: collapse;
    min-width: 760px;
}

th,
td {
    padding: 9px 8px;
    border-bottom: 1px solid #202733;
    font-size: 12px;
    text-align: right;
    white-space: nowrap;
}

th:first-child,
td:first-child,
th:nth-child(2),
td:nth-child(2) {
    text-align: left;
}

th {
    color: #7f8b9c;
    font-weight: 500;
}

.s {
    color: #ff5050;
    font-weight: 800;
}

.a {
    color: #ff9e4a;
    font-weight: 800;
}

.b {
    color: #e8d05b;
    font-weight: 800;
}

.c {
    color: #8f9baa;
    font-weight: 800;
}

.up {
    color: #f34c4c;
}

.down {
    color: #32c776;
}

.empty {
    padding: 30px;
    text-align: center;
    color: #8a95a5;
}

@media(max-width:700px) {

    .cards {
        grid-template-columns: 1fr;
    }

    .card {
        padding: 11px;
    }

    .title {
        font-size: 22px;
    }

    .wrap {
        padding: 9px;
    }

    .bar input {
        flex: 1;
    }

    .notice {
        line-height: 1.6;
    }
}

</style>

</head>

<body>

<header>

<div class="title">
🔥 妖股雷达
</div>

<div class="sub">
3日涨停 × 龙虎榜 × 市值 ≤ 300亿 × 妖股概率
</div>

<div class="bar">

<input
id="date"
type="date"
>

<button onclick="load()">
扫描
</button>

</div>

</header>

<main class="wrap">

<div
id="notice"
class="notice"
>
正在读取数据…
</div>

<div
id="cards"
class="cards"
>
</div>

<div
id="table"
class="table"
>
</div>

</main>

<script>

const $ = id =>
    document.getElementById(id);


function fmt(n, d = 2) {

    return n == null
        ? '—'
        : Number(n).toFixed(d);

}


function money(n) {

    return n == null
        ? '—'
        : Number(n).toFixed(2) + '亿';

}


async function load() {

    const v =
        $('date')
        .value
        .replaceAll('-', '');

    $('notice').textContent =
        '正在扫描 ' +
        $('date').value +
        ' …';

    try {

        const r =
            await fetch(
                '/api/scanner?date=' +
                v
            );

        const d =
            await r.json();

        render(d);

    } catch (e) {

        $('notice').textContent =
            '数据读取失败，请稍后重试';

    }

}


function render(d) {

    $('notice').innerHTML =
        '日期：<b>' +
        d.date +
        '</b>　符合全部硬条件：<b>' +
        d.hard_count +
        '</b>只　数据源：' +
        d.source +
        '<br>' +
        d.note;


    let a =
        d.top3 || [];


    $('cards').innerHTML =
        a.length

        ?

        a.map(
            (x, i) => `

<div class="card">

<div class="rank">
TOP ${i + 1}　${x.tier}级
</div>

<div class="name">
${x.name}
</div>

<div class="code">
${x.code}
</div>

<div class="score">
${x.score}
<span
style="font-size:12px;color:#778394"
>
/ 100
</span>
</div>

<div
class="pct ${x.pct >= 0 ? 'up' : 'down'}"
>
${x.pct >= 0 ? '+' : ''}
${fmt(x.pct)}%
</div>

<div class="tags">

${
    x.hard.map(
        z =>
        `<span class="tag hot">
        ✓ ${z}
        </span>`
    ).join('')
}

<span class="tag">
涨停 ${x.zt3}次
</span>

<span class="tag">
龙虎榜 ${money(x.lhb_day_net)}
</span>

</div>

</div>

`
        ).join('')

        :

        '<div class="empty">' +
        '当前日期没有同时满足全部硬条件的股票' +
        '</div>';


    let rows =
        d.ranking || [];


    $('table').innerHTML =

        rows.length

        ?

        `

<table>

<thead>

<tr>

<th>代码</th>
<th>名称</th>
<th>评分</th>
<th>涨幅</th>
<th>换手</th>
<th>市值</th>
<th>3日涨停</th>
<th>龙虎榜当日</th>
<th>龙虎榜10日</th>
<th>级别</th>

</tr>

</thead>

<tbody>

${
    rows.map(
        x => `

<tr>

<td>
${x.code}
</td>

<td>
${x.name}
</td>

<td>
<b>
${x.score}
</b>
</td>

<td
class="${x.pct >= 0 ? 'up' : 'down'}"
>
${x.pct >= 0 ? '+' : ''}
${fmt(x.pct)}%
</td>

<td>
${fmt(x.turnover)}%
</td>

<td>
${money(x.market_cap)}
</td>

<td>
${x.zt3}
</td>

<td>
${money(x.lhb_day_net)}
</td>

<td>
${money(x.lhb_net10)}
</td>

<td
class="${x.tier.toLowerCase()}"
>
${x.tier}
</td>

</tr>

`
    ).join('')
}

</tbody>

</table>

`

        :

        '<div class="empty">' +
        '暂无结果' +
        '</div>';

}


const today =
    new Date();


$('date').value =
    new Date(
        today.getTime() -
        today.getTimezoneOffset() * 60000
    )
    .toISOString()
    .slice(0, 10);


load();

</script>

</body>

</html>
'''


# ============================================================
# 首页
# ============================================================

@app.get(
    '/',
    response_class=HTMLResponse
)
def home():
    return HTML


# ============================================================
# 本地运行
# ============================================================

if __name__ == '__main__':

    import uvicorn

    uvicorn.run(
        APP,
        host='0.0.0.0',
        port=int(
            os.getenv(
                'PORT',
                '8000'
            )
        )
    )
