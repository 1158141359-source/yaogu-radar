import os, time, threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from zoneinfo import ZoneInfo

app = FastAPI(title='妖股雷达')
TZ = ZoneInfo('Asia/Shanghai')
TODAY = datetime.now(TZ).strftime('%Y-%m-%d')
TIMEOUT = 8
PUSH = 'https://push2.eastmoney.com'
PUSH_HIS = 'https://push2his.eastmoney.com'
PUSH_EX = 'https://push2ex.eastmoney.com'
DATA = 'https://datacenter-web.eastmoney.com'
MX_URL = 'https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen'

session = requests.Session()
session.headers.update({'User-Agent':'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1','Referer':'https://quote.eastmoney.com/'})
CACHE, CACHE_LOCK = {}, threading.Lock()

def safe_float(v, default=0.0):
    try:
        return default if v in (None, '') else float(v)
    except Exception:
        return default

def clean_code(code):
    if not code: return ''
    s=str(code).strip().upper().replace('SH','').replace('SZ','').replace('BJ','').replace('.','')
    return s.zfill(6)

def secid(code):
    c=clean_code(code)
    return ('1.' if c.startswith(('6','68','69')) else '0.')+c

def normalize_date(value):
    if not value: return TODAY
    s=str(value).replace('/','-')
    if 'T' in s: s=s.split('T')[0]
    if ' ' in s: s=s.split(' ')[0]
    try: return datetime.strptime(s[:10],'%Y-%m-%d').strftime('%Y-%m-%d')
    except Exception: return TODAY

def get_json(url, params=None, timeout=TIMEOUT):
    try:
        r=session.get(url,params=params,timeout=timeout)
        if r.status_code != 200: return {}
        x=r.json()
        return x if isinstance(x,dict) else {}
    except Exception: return {}

def get_trading_dates(end_date,count=12):
    end_date=normalize_date(end_date)
    try: dt=datetime.strptime(end_date,'%Y-%m-%d')
    except Exception: dt=datetime.now(TZ)
    data=get_json(f'{PUSH_HIS}/api/qt/stock/kline/get',{
        'secid':'1.000001','klt':'101','fqt':'0','beg':(dt-timedelta(days=45)).strftime('%Y%m%d'),
        'end':dt.strftime('%Y%m%d'),'lmt':'60','fields1':'f1,f2,f3,f4,f5,f6','fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'})
    rows=((data.get('data') or {}).get('klines') or [])
    dates=[]
    for row in rows:
        try:
            d=normalize_date(str(row).split(',')[0])
            if d not in dates: dates.append(d)
        except Exception: pass
    dates.sort()
    if end_date in dates:
        i=dates.index(end_date); return dates[max(0,i-count+1):i+1]
    return dates[-count:]

def get_actual_target_date(target_date):
    d=normalize_date(target_date); dates=get_trading_dates(d,12)
    return d if d in dates else (dates[-1] if dates else d)

def get_kline(code,limit=120,end_date=None):
    end_date=normalize_date(end_date or TODAY)
    try: dt=datetime.strptime(end_date,'%Y-%m-%d')
    except Exception: dt=datetime.now(TZ)
    data=get_json(f'{PUSH_HIS}/api/qt/stock/kline/get',{
        'secid':secid(code),'klt':'101','fqt':'1','beg':(dt-timedelta(days=280)).strftime('%Y%m%d'),
        'end':dt.strftime('%Y%m%d'),'lmt':str(limit),'fields1':'f1,f2,f3,f4,f5,f6','fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'})
    rows=((data.get('data') or {}).get('klines') or [])
    out=[]
    for row in rows:
        p=str(row).split(',')
        if len(p)>=7:
            out.append({'date':normalize_date(p[0]),'open':safe_float(p[1]),'close':safe_float(p[2]),'high':safe_float(p[3]),'low':safe_float(p[4]),'volume':safe_float(p[5]),'amount':safe_float(p[6])})
    return out

def get_quotes(codes):
    codes=[clean_code(x) for x in codes if clean_code(x)]
    out={}
    for i in range(0,len(codes),80):
        batch=codes[i:i+80]
        data=get_json(f'{PUSH}/api/qt/ulist.np/get',{
            'fltt':'2','invt':'2','secids':','.join(secid(x) for x in batch),
            'fields':'f2,f3,f8,f12,f13,f14,f20,f21'})
        rows=((data.get('data') or {}).get('diff') or [])
        if isinstance(rows,dict): rows=list(rows.values())
        for x in rows:
            if not isinstance(x,dict): continue
            c=clean_code(x.get('f12'))
            if c in batch:
                # f20/f21 are yuan; convert to 亿元.
                out[c]={'price':safe_float(x.get('f2')),'pct':safe_float(x.get('f3')),'turnover':safe_float(x.get('f8')),'name':x.get('f14') or c,'market_cap':safe_float(x.get('f20'))/1e8,'float_cap':safe_float(x.get('f21'))/1e8}
    return out

def get_zt_pool(date):
    data=get_json(f'{PUSH_EX}/getTopicZTPool',{
        'ut':'7eea3edcaed734bea9cbfcf3c6a2c7f1','dpt':'wz.ztzt','Pageindex':'0','pagesize':'200',
        'sort':'fbt:asc','date':normalize_date(date).replace('-',''),'type':'zt','zttj':'st','iszt':'1'})
    obj=data.get('data')
    if not isinstance(obj,dict): return []
    pool=obj.get('pool')
    return pool if isinstance(pool,list) else []

def get_zt_counts(dates):
    out={}
    for d in dates:
        for x in get_zt_pool(d):
            if isinstance(x,dict):
                c=clean_code(x.get('c') or x.get('code') or x.get('SECURITY_CODE'))
                if c: out[c]=out.get(c,0)+1
    return out

def get_lhb_one_date(date):
    # Exact-date filter is more reliable than a large range when Eastmoney returns result=None.
    params={
        'reportName':'RPT_DAILYBILLBOARD_DETAILSNEW',
        'columns':'SECURITY_CODE,SECUCODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLAIN,CLOSE_PRICE,CHANGE_RATE,BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,TURNOVERRATE,FREE_MARKET_CAP,EXPLANATION',
        'pageNumber':'1','pageSize':'500','sortColumns':'SECURITY_CODE,TRADE_DATE','sortTypes':'1,-1','source':'WEB','client':'WEB',
        'filter':f"(TRADE_DATE='{normalize_date(date)}')"
    }
    data=get_json(f'{DATA}/api/data/v1/get',params)
    result=data.get('result')
    if not isinstance(result,dict): return []
    rows=result.get('data')
    return [x for x in rows if isinstance(x,dict)] if isinstance(rows,list) else []

def get_lhb(codes=None,dates=None):
    dates=list(dict.fromkeys(normalize_date(x) for x in (dates or [TODAY])))
    out={}
    with ThreadPoolExecutor(max_workers=min(5,len(dates))) as ex:
        fs=[ex.submit(get_lhb_one_date,d) for d in dates]
        for f in as_completed(fs):
            try: rows=f.result()
            except Exception: rows=[]
            for row in rows:
                c=clean_code(row.get('SECURITY_CODE') or row.get('SECUCODE'))
                if not c or (codes and c not in codes): continue
                d=normalize_date(row.get('TRADE_DATE'))
                if c not in out: out[c]={'dates':set(),'net':0.0}
                out[c]['dates'].add(d)
                out[c]['net']+=safe_float(row.get('BILLBOARD_NET_AMT'))
    return out

def get_order_book(code):
    data=get_json(f'{PUSH}/api/qt/stock/get',{'secid':secid(code),'fields':'f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f61,f62,f63,f64,f65,f66'})
    d=data.get('data')
    if not isinstance(d,dict): return {'buy':0,'sell':0,'ratio':0}
    buy=sum(safe_float(d.get(k)) for k in ('f58','f60','f62','f64','f66'))
    sell=sum(safe_float(d.get(k)) for k in ('f57','f59','f61','f63','f65'))
    total=buy+sell
    return {'buy':buy,'sell':sell,'ratio':sell/total if total else 0}

def get_mx_candidates():
    key=os.getenv('MX_APIKEY','').strip()
    if not key: return set()
    try:
        r=session.post(MX_URL,headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'},json={'query':'今日A股强势股票，关注涨停、龙虎榜、短线强势股票'},timeout=6)
        if r.status_code!=200: return set()
        data=r.json()
    except Exception: return set()
    out=set()
    def walk(o):
        import re
        if isinstance(o,dict):
            for k,v in o.items():
                if str(k).lower() in {'code','stock_code','security_code','symbol'}:
                    c=clean_code(v)
                    if len(c)==6: out.add(c)
                walk(v)
        elif isinstance(o,list):
            for x in o: walk(x)
        elif isinstance(o,str):
            out.update(re.findall(r'\b[036]\d{5}\b',o))
    walk(data)
    return out

def score(quote,zt_count,lhb,ob=None,mx=False):
    cap=safe_float(quote.get('market_cap')); pct=safe_float(quote.get('pct')); turnover=safe_float(quote.get('turnover'))
    hard=[zt_count>=1,bool(lhb),0<cap<=300]
    s=0; reasons=[]
    if hard[0]: s+=30; reasons.append('3日内涨停')
    if hard[1]: s+=25; reasons.append('龙虎榜')
    if hard[2]: s+=20; reasons.append('市值≤300亿')
    if pct>=9.5: s+=15; reasons.append('接近涨停')
    elif pct>=7: s+=10; reasons.append('强势上涨')
    elif pct>=5: s+=6; reasons.append('涨幅>5%')
    if turnover>=29.25: s+=15; reasons.append('高换手')
    elif turnover>=15: s+=8; reasons.append('换手较高')
    elif turnover>=8: s+=4
    net=safe_float((lhb or {}).get('net'))
    if net>0: s+=8; reasons.append('龙虎榜净买')
    elif net<0: s-=3
    if ob and safe_float(ob.get('sell'))>safe_float(ob.get('buy')): s+=5; reasons.append('委卖>委买')
    if mx: s+=5; reasons.append('妙想强势')
    return round(s,2),hard,reasons,round(net,2)

def build_scan(target_date=None):
    target=get_actual_target_date(target_date or TODAY)
    dates=get_trading_dates(target,12) or [target]
    last3=dates[-3:]; lhb_dates=dates[-10:]
    zt_counts=get_zt_counts(last3)
    lhb=get_lhb(dates=lhb_dates)
    candidates=list(set(zt_counts)&set(lhb))[:300]
    if not candidates:
        return {'success':True,'date':target,'updated':datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S'),'message':'当前日期没有同时满足涨停+龙虎榜的候选股','conditions':['近3个交易日出现涨停','上过龙虎榜','总市值≤300亿'],'top3':[],'ranking':[],'stats':{'zt_count':len(zt_counts),'lhb_count':len(lhb),'candidate_count':0,'hard_count':0}}
    quotes=get_quotes(candidates)
    history={}
    if target!=TODAY:
        with ThreadPoolExecutor(max_workers=8) as ex:
            fs={ex.submit(get_kline,c,120,target):c for c in candidates}
            for f in as_completed(fs):
                try:
                    c=fs[f]; rows=f.result()
                    if rows: history[c]=rows
                except Exception: pass
    results=[]
    for c in candidates:
        q=quotes.get(c)
        if not q or 'ST' in str(q.get('name','')).upper(): continue
        if zt_counts.get(c,0)<1 or not lhb.get(c): continue
        if not (0<safe_float(q.get('market_cap'))<=300): continue
        q=dict(q)
        if target!=TODAY:
            rows=history.get(c,[]); tr=next((r for r in rows if r['date']==target),None)
            prev=[r['close'] for r in rows if r['date']<target and r['close']>0]
            if tr and prev:
                q['price']=tr['close']; q['pct']=(tr['close']/prev[-1]-1)*100
        s,hard,reasons,net=score(q,zt_counts[c],lhb[c])
        results.append({'code':c,'name':q.get('name') or c,'price':round(safe_float(q.get('price')),2),'pct':round(safe_float(q.get('pct')),2),'turnover':round(safe_float(q.get('turnover')),2),'market_cap':round(safe_float(q.get('market_cap')),2),'zt_count':zt_counts[c],'lhb':True,'lhb_net':net,'score':s,'hard_ok':all(hard),'reasons':reasons,'order_book':None})
    results.sort(key=lambda x:(x['score'],x['pct'],x['zt_count']),reverse=True)
    mx=get_mx_candidates() if target==TODAY else set()
    for x in results:
        if x['code'] in mx: x['score']+=5; x['reasons'].append('妙想强势')
    if target==TODAY and results:
        top=results[:10]
        with ThreadPoolExecutor(max_workers=5) as ex:
            fs={ex.submit(get_order_book,x['code']):x for x in top}
            for f in as_completed(fs):
                x=fs[f]
                try:
                    ob=f.result(); x['order_book']=ob
                    if safe_float(ob.get('sell'))>safe_float(ob.get('buy')): x['score']+=5; x['reasons'].append('委卖>委买')
                except Exception: pass
        results.sort(key=lambda x:(x['score'],x['pct'],x['zt_count']),reverse=True)
    hard=[x for x in results if x['hard_ok']]
    return {'success':True,'date':target,'updated':datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S'),'message':'已按三项核心硬条件完成筛选','conditions':['近3个交易日出现涨停','上过龙虎榜','总市值≤300亿'],'top3':hard[:3],'ranking':hard[:30],'stats':{'zt_count':len(zt_counts),'lhb_count':len(lhb),'candidate_count':len(candidates),'hard_count':len(hard)}}

@app.get('/api/health')
def health(): return {'success':True,'status':'ok','time':datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}

@app.get('/api/scanner')
def scanner(date:str=None):
    target=normalize_date(date or TODAY)
    with CACHE_LOCK:
        c=CACHE.get(target)
        if c and time.time()-c['time']<60: return c['data']
    try:
        data=build_scan(target)
        with CACHE_LOCK: CACHE[target]={'time':time.time(),'data':data}
        return data
    except Exception as e:
        return JSONResponse(status_code=200,content={'success':False,'error':str(e),'date':target,'updated':datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S'),'top3':[],'ranking':[]})

HTML='''<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>🔥 妖股雷达</title><style>*{box-sizing:border-box}body{margin:0;background:#050505;color:#eee;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}.container{max-width:900px;margin:auto;padding:14px}.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}.title{font-size:26px;font-weight:800}.status{font-size:12px;color:#888}.panel{background:#111;border:1px solid #292929;border-radius:14px;padding:14px;margin-bottom:12px}.controls{display:flex;gap:8px;flex-wrap:wrap}input{flex:1;min-width:180px;background:#181818;color:#fff;border:1px solid #333;border-radius:9px;padding:11px;font-size:15px}button{border:0;border-radius:9px;padding:10px 15px;background:#e60012;color:#fff;font-weight:700;font-size:14px}button.secondary{background:#292929}.section-title{font-size:18px;font-weight:800;margin-bottom:10px}.conditions{line-height:1.9;font-size:14px}.red{color:#ff3b45}.green{color:#19d66b}.muted{color:#888;font-size:12px}.card{background:#171717;border:1px solid #303030;border-radius:12px;padding:13px;margin-bottom:9px}.card.top{border-color:#e60012}.row{display:flex;justify-content:space-between;gap:10px}.name{font-size:17px;font-weight:800}.code{color:#888;font-size:12px;margin-left:6px}.score{color:#ffcc00;font-weight:900;font-size:20px}.data{display:flex;flex-wrap:wrap;gap:8px;margin-top:9px}.tag{background:#222;border-radius:6px;padding:4px 7px;font-size:12px}.reason{margin-top:9px;color:#aaa;font-size:12px;line-height:1.6}.error{color:#ff4650;background:#22090b;border:1px solid #5b1419}.loading,.empty{text-align:center;color:#777;padding:25px 5px}.footer{text-align:center;color:#555;font-size:11px;padding:20px 0}</style></head><body><div class="container"><div class="header"><div class="title">🔥 妖股雷达</div><div id="status" class="status">准备就绪</div></div><div class="panel"><div class="controls"><input id="date" type="date"><button onclick="scan()">开始扫描</button><button class="secondary" onclick="today()">今天</button></div></div><div class="panel"><div class="section-title">核心硬条件</div><div class="conditions">① <span class="red">近3个交易日出现涨停</span><br>② <span class="red">上过龙虎榜</span><br>③ <span class="green">总市值 ≤ 300亿</span></div><div class="muted">评分只用于排序，不会突破以上三项硬条件。</div></div><div id="message"></div><div class="panel"><div class="section-title">🔥 TOP 3 强势标的</div><div id="top3"><div class="loading">等待扫描</div></div></div><div class="panel"><div class="section-title">📊 妖股概率排行</div><div id="ranking"><div class="loading">等待扫描</div></div></div><div class="footer">数据：东方财富公开行情接口<br>妖股雷达仅用于量化分析参考，不构成投资建议</div></div><script>const D=document.getElementById('date'),S=document.getElementById('status'),M=document.getElementById('message'),T=document.getElementById('top3'),R=document.getElementById('ranking');function td(){let d=new Date(),m=String(d.getMonth()+1).padStart(2,'0'),x=String(d.getDate()).padStart(2,'0');return d.getFullYear()+'-'+m+'-'+x}function today(){D.value=td();scan()}function card(x,i){let p=Number(x.pct||0);return '<div class="card '+(i<3?'top':'')+'"><div class="row"><div><span class="name">'+(x.name||x.code)+'</span><span class="code">'+x.code+'</span></div><div class="score">'+Number(x.score||0).toFixed(0)+'</div></div><div class="data"><span class="tag">涨幅 <b class="'+(p>=0?'red':'green')+'">'+(p>=0?'+':'')+p.toFixed(2)+'%</b></span><span class="tag">涨停 '+(x.zt_count||0)+'次</span><span class="tag">换手 '+Number(x.turnover||0).toFixed(2)+'%</span><span class="tag">市值 '+Number(x.market_cap||0).toFixed(1)+'亿</span><span class="tag">龙虎榜 ✓</span></div><div class="reason">'+(x.reasons||[]).join(' · ')+'</div></div>'}function render(d){if(!d){M.innerHTML='<div class="panel error">加载失败：没有返回数据</div>';return}if(d.success===false){M.innerHTML='<div class="panel error">加载失败：'+(d.error||'接口异常')+'</div>';T.innerHTML='<div class="empty">暂无数据</div>';R.innerHTML='<div class="empty">暂无数据</div>';return}M.innerHTML='<div class="panel"><div class="muted">'+d.date+' · 更新时间：'+d.updated+'</div><div style="margin-top:7px">'+(d.message||'')+'</div></div>';T.innerHTML=(d.top3||[]).length?d.top3.map(card).join(''):'<div class="empty">当前日期没有满足三项硬条件的股票</div>';R.innerHTML=(d.ranking||[]).length?d.ranking.map(card).join(''):'<div class="empty">暂无符合条件的股票</div>';S.innerText='候选 '+((d.stats||{}).hard_count||0)+' 只'}async function scan(){let date=D.value||td();S.innerText='数据读取中...';M.innerHTML='';T.innerHTML='<div class="loading">正在扫描，请稍候...</div>';R.innerHTML='<div class="loading">正在计算...</div>';try{let c=new AbortController(),t=setTimeout(()=>c.abort(),30000),r=await fetch('/api/scanner?date='+encodeURIComponent(date),{cache:'no-store',signal:c.signal});clearTimeout(t);if(!r.ok)throw Error('服务器 HTTP '+r.status);render(await r.json())}catch(e){S.innerText='数据读取失败';M.innerHTML='<div class="panel error">加载失败：'+(e.message||'Load failed')+'</div>';T.innerHTML='<div class="empty">请稍后重新扫描</div>';R.innerHTML='<div class="empty">暂无数据</div>'}}D.value=td();scan();</script></body></html>'''

@app.get('/',response_class=HTMLResponse)
def home(): return HTML

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='0.0.0.0',port=int(os.getenv('PORT','8000')))
