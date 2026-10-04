from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from datetime import datetime

app = FastAPI(title="妖股雷达")

DATA = [
    {"code": "000001", "name": "平安银行", "price": 12.58, "pct": 9.96, "streak": 2, "score": 86},
    {"code": "000725", "name": "京东方A", "price": 4.31, "pct": 8.29, "streak": 1, "score": 78},
    {"code": "002594", "name": "比亚迪", "price": 112.30, "pct": 7.12, "streak": 3, "score": 91},
    {"code": "300750", "name": "宁德时代", "price": 248.60, "pct": 6.45, "streak": 2, "score": 88},
    {"code": "600519", "name": "贵州茅台", "price": 1488.00, "pct": 5.36, "streak": 1, "score": 72},
]


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "time": datetime.now().isoformat()
    }


@app.get("/api/scanner")
def scanner():
    return {
        "updated_at": datetime.now().isoformat(),
        "data": DATA
    }


@app.get("/", response_class=HTMLResponse)
def home():

    rows = "".join(
        """
        <tr>
            <td>{code}</td>
            <td class="name">{name}</td>
            <td>{price:.2f}</td>
            <td class="up">+{pct:.2f}%</td>
            <td>{streak}板</td>
            <td><b>{score}</b></td>
        </tr>
        """.format(**x)
        for x in DATA
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return """
<!doctype html>
<html lang="zh-CN">

<head>

<meta charset="utf-8">

<meta name="viewport"
content="width=device-width,initial-scale=1">

<title>妖股雷达</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #090d14;
    color: #e8edf5;
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        "PingFang SC",
        sans-serif;
}

.wrap {
    max-width: 1100px;
    margin: auto;
    padding: 24px 16px;
}

h1 {
    margin: 0;
    font-size: 28px;
}

.sub {
    color: #7f8da3;
    margin: 7px 0 22px;
}

.cards {
    display: grid;
    grid-template-columns:
        repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 18px;
}

.card {
    background: #111824;
    border: 1px solid #1d2938;
    border-radius: 14px;
    padding: 16px;
}

.label {
    color: #8794a8;
    font-size: 13px;
}

.num {
    font-size: 25px;
    font-weight: 700;
    margin-top: 8px;
}

.up {
    color: #ff4d67;
}

.tablebox {
    background: #111824;
    border: 1px solid #1d2938;
    border-radius: 14px;
    overflow: hidden;
}

table {
    width: 100%;
    border-collapse: collapse;
}

th,
td {
    padding: 14px 12px;
    text-align: left;
    border-bottom: 1px solid #1d2938;
}

th {
    color: #8c99ad;
    font-size: 13px;
    font-weight: 500;
}

.name {
    font-weight: 600;
}

.footer {
    color: #68758a;
    font-size: 12px;
    margin-top: 14px;
}

@media (max-width: 700px) {

    .cards {
        grid-template-columns:
            repeat(2, 1fr);
    }

}

</style>

</head>

<body>

<div class="wrap">

<h1>🔥 妖股雷达</h1>

<div class="sub">
短线强势股监测 · 实时数据接口已预留
</div>

<div class="cards">

<div class="card">
<div class="label">监测标的</div>
<div class="num">5</div>
</div>

<div class="card">
<div class="label">涨停/强势</div>
<div class="num up">5</div>
</div>

<div class="card">
<div class="label">最高连板</div>
<div class="num">3板</div>
</div>

<div class="card">
<div class="label">平均评分</div>
<div class="num">83</div>
</div>

</div>

<div class="tablebox">

<table>

<thead>

<tr>
<th>代码</th>
<th>名称</th>
<th>现价</th>
<th>涨幅</th>
<th>连板</th>
<th>雷达评分</th>
</tr>

</thead>

<tbody>

""" + rows + """

</tbody>

</table>

</div>

<div class="footer">

最后更新：
""" + now + """
 · 当前为演示数据

</div>

</div>

</body>

</html>
"""
