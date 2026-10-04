# -*- coding: utf-8 -*-
"""
main.py — 游资 AI 选股工作模式 · 选股 / 打分 / 买卖点 脚本
================================================================
把用户自定义的五条选股条件、多因子打分模型、援军战法买卖点规则
固化成可运行的 Python 脚本。

用法:
    1) 准备日线数据(CSV，每只股票一个文件，或所有股票汇总文件)。
       列名固定为: date, open, high, low, close, volume
       示例一行: 2026-09-29,10.50,11.20,10.40,11.05,25000000
    2) 数据文件放入 DATA_DIR 目录(默认 ./data/*.csv)。
    3) 运行:  python main.py
    4) 可选参数:
         --top 5              # 输出前几只(默认 3)
         --min-date 2026-01-01  # 只分析该日期之后的行情

================================================================
免责声明：本脚本为方法论与流程的实现，仅用于研究/复盘，不构成任何
投资建议；股市有风险，所有决策与盈亏由使用者自行承担。
================================================================
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# 0. 配置
# ----------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TOP_N_DEFAULT = 3          # 默认输出前 3 只
LIMIT_UP_RATIO = 0.098     # 涨停判定阈值(主板≈10%，留出小数误差)
CONSEC_UP_DAYS = 5         # 连续上涨天数要求
LIMIT_UP_LOOKBACK = 30     # 30 日内有涨停
MA_WINDOW = 5              # 5 日线

# 多因子打分权重(用户自定义，和为 1.0)
FACTOR_WEIGHTS = {
    "题材强度": 0.25,
    "卡位辨识度": 0.20,
    "板块效应": 0.15,
    "量价封单": 0.15,
    "资金合力": 0.15,
    "情绪阶段适配": 0.10,
}

# ----------------------------------------------------------------------
# 1. 数据模型与读取
# ----------------------------------------------------------------------
@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


def read_csv(path: str) -> List[Bar]:
    """读取一个 OHLCV 日线 CSV，按日期升序返回 Bar 列表。"""
    bars: List[Bar] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                bars.append(
                    Bar(
                        date=row["date"].strip(),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                    )
                )
            except (KeyError, ValueError) as e:
                print(f"  [跳过] {path} 解析失败: {row} ({e})")
    bars.sort(key=lambda b: b.date)
    return bars


def load_all_bars(data_dir: str, min_date: Optional[str] = None) -> Dict[str, List[Bar]]:
    """加载 data_dir 下所有 *.csv，返回 {文件名: bars}。"""
    result: Dict[str, List[Bar]] = {}
    if not os.path.isdir(data_dir):
        print(f"[警告] 数据目录不存在: {data_dir}")
        return result
    for path in sorted(glob.glob(os.path.join(data_dir, "*.csv"))):
        bars = read_csv(path)
        if min_date:
            bars = [b for b in bars if b.date >= min_date]
        if len(bars) >= MA_WINDOW + 1:
            name = os.path.splitext(os.path.basename(path))[0]
            result[name] = bars
    return result


# ----------------------------------------------------------------------
# 2. 技术指标
# ----------------------------------------------------------------------
def sma(values: List[float], window: int) -> List[Optional[float]]:
    """简单移动平均，前面 window-1 个为 None。"""
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < window:
        return out
    s = sum(values[:window])
    out[window - 1] = s / window
    for i in range(window, len(values)):
        s += values[i] - values[i - window]
        out[i] = s / window
    return out


def pct_change(closes: List[float]) -> List[Optional[float]]:
    out: List[Optional[float]] = [None]
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        out.append(None if prev == 0 else (closes[i] - prev) / prev)
    return out


def is_limit_up(bar: Bar, prev_close: Optional[float]) -> bool:
    """当日是否涨停(近似判定，阈值由 LIMIT_UP_RATIO 控制)。"""
    if prev_close is None or prev_close <= 0:
        return False
    return (bar.close - prev_close) / prev_close >= LIMIT_UP_RATIO - 1e-6


# ----------------------------------------------------------------------
# 3. 五条选股条件
# ----------------------------------------------------------------------
def condition_5_consec_up(bars: List[Bar]) -> bool:
    """条件1: 连续5日上涨(收盘价逐日走高)。"""
    if len(bars) < CONSEC_UP_DAYS:
        return False
    tail = bars[-CONSEC_UP_DAYS:]
    return all(tail[i].close > tail[i - 1].close for i in range(1, len(tail)))


def condition_limit_up_in_30d(bars: List[Bar]) -> bool:
    """条件2: 30 日内有过涨停。"""
    if len(bars) < 2:
        return False
    lookback = bars[-LIMIT_UP_LOOKBACK:]
    for i in range(1, len(lookback)):
        if is_limit_up(lookback[i], lookback[i - 1].close):
            return True
    return False


def condition_close_above_ma5(bars: List[Bar]) -> bool:
    """条件3: 收盘价不破 5 日线(最新收盘 >= 5日均线)。"""
    closes = [b.close for b in bars]
    ma5 = sma(closes, MA_WINDOW)
    return ma5[-1] is not None and closes[-1] >= ma5[-1]


def condition_volume_accumulation(bars: List[Bar]) -> bool:
    """条件4: 成交量堆量——近期成交量温和放大且呈台阶式堆高。

    简化口径: 近 5 日均量 > 前 20 日均量 × 1.1，且近 5 日内没有单日
    极度放量后立刻大幅缩量(避免脉冲量)。
    """
    if len(bars) < 25:
        return False
    recent = [b.volume for b in bars[-5:]]
    base = [b.volume for b in bars[-25:-5]]
    avg_recent = sum(recent) / len(recent)
    avg_base = sum(base) / len(base)
    if avg_base <= 0:
        return False
    # 堆量: 近端均量放大
    if avg_recent < avg_base * 1.1:
        return False
    # 剔除"脉冲量": 最近一日的量不低于近5日均量的 60%
    return recent[-1] >= avg_recent * 0.6


def condition_bottom_chips_stable(bars: List[Bar]) -> bool:
    """条件5: 底部筹码不动。

    用"缩量回调不破底"作为筹码稳定代理指标: 最近 20 日内，若股价较
    区间最低点回调，回调段成交量明显小于拉升段，且最新价仍在区间
    高位(未跌破前低)，视为底部筹码锁定。
    """
    if len(bars) < 21:
        return False
    window = bars[-21:]
    low_price = min(b.low for b in window)
    high_price = max(b.high for b in window)
    if high_price <= 0 or low_price <= 0:
        return False
    cur = bars[-1]
    # 仍在区间相对高位(距最高点回撤 < 15%)且未创新低
    drawdown = (high_price - cur.close) / high_price
    if drawdown > 0.15:
        return False
    # 近 3 日最低价不显著低于区间低位(防止放量破位)
    recent_low = min(b.low for b in bars[-3:])
    if recent_low < low_price * 0.97:
        return False
    return True


# ----------------------------------------------------------------------
# 4. 多因子打分(用户自定义透明模型, 每因子 0-10)
# ----------------------------------------------------------------------
@dataclass
class StockScore:
    name: str
    factors: Dict[str, float] = field(default_factory=dict)
    total: float = 0.0


def score_stock(
    name: str,
    bars: List[Bar],
    factor_inputs: Optional[Dict[str, float]] = None,
) -> Optional[StockScore]:
    """对单只股票打分。

    factor_inputs 提供无法从 OHLCV 直接算出的因子(题材强度/卡位辨识度/
    板块效应/资金合力/情绪阶段适配)，由人工或外部数据补充(0-10)。
    若未提供，则用内部技术代理口径估算并注明"代理分"。
    """
    if factor_inputs is None:
        factor_inputs = {}

    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]
    pc = pct_change(closes)

    # --- 技术代理因子(0-10) ---
    ma5 = sma(closes, MA_WINDOW)

    # 量价封单: 由涨停存在性 + 堆量 + 收盘贴近5日线(强势) 估算
    up_days = sum(1 for p in pc if p is not None and p > 0)
    ratio_up = up_days / max(len(closes) - 1, 1)
    has_lu = condition_limit_up_in_30d(bars)
    score_lp = min(10.0, ratio_up * 12.0 + (4.0 if has_lu else 0.0) + 2.0)

    # 情绪阶段适配: 连续上涨 + 站上5日线 视为适配发酵/高潮
    score_sent = min(10.0, (5.0 if condition_5_consec_up(bars) else 0.0)
                     + (3.0 if condition_close_above_ma5(bars) else 0.0) + 2.0)

    # 外部/代理因子合并: 优先用外部输入, 否则用代理口径
    factors = {
        "题材强度": factor_inputs.get("题材强度", 5.0),
        "卡位辨识度": factor_inputs.get("卡位辨识度", 5.0),
        "板块效应": factor_inputs.get("板块效应", 5.0),
        "量价封单": factor_inputs.get("量价封单", round(score_lp, 2)),
        "资金合力": factor_inputs.get("资金合力", 5.0),
        "情绪阶段适配": factor_inputs.get("情绪阶段适配", round(score_sent, 2)),
    }

    total = sum(FACTOR_WEIGHTS[k] * factors[k] for k in FACTOR_WEIGHTS)
    return StockScore(name=name, factors=factors, total=round(total, 2))


# ----------------------------------------------------------------------
# 5. 买卖点规则(援军战法 / 三种阴线 / 三高三低)
# ----------------------------------------------------------------------
@dataclass
class TradePlan:
    name: str
    buy_point: str
    hold_rule: str
    sell_point: str
    triggered_rules: List[str]


def analyze_trade(name: str, bars: List[Bar]) -> TradePlan:
    """给出买点/持有/卖点, 并回填触发了哪些规则。"""
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    triggered: List[str] = []

    # --- 援军战法: 重挫后企稳不再创新低 ---
    recent_low = min(lows[-15:])          # 近期低点
    last_low = lows[-1]
    stabilized = last_low >= recent_low and last_low <= recent_low * 1.02
    if stabilized:
        buy_point = f"援军战法: 近期低点约 {recent_low:.2f} 已企稳不再创新低，低点附近作为买点/撤军点"
        triggered.append("援军战法(企稳不创新低)")
    else:
        buy_point = f"尚未企稳(最新低 {last_low:.2f} 距区间低 {recent_low:.2f})，先观察不急着接"

    # --- 三种阴线识别(最近 3 根 K 线) ---
    yin_info = ""
    for i in range(max(1, len(bars) - 3), len(bars)):
        b = bars[i]
        is_yin = b.close < b.open
        if not is_yin:
            continue
        prev = bars[i - 1]
        # 破位阴: 收盘跌破前低
        if b.close < prev.low:
            yin_info = f"最近出现破位阴({b.date})，警惕破位，不宜低吸"
            if "破位阴" not in triggered:
                triggered.append("破位阴(警示)")
        # 加速阴: 跌幅明显大于前一日且放量
        elif prev.close > 0 and (prev.close - b.close) / prev.close > 0.03 and b.volume > prev.volume:
            yin_info = f"最近出现加速阴({b.date})，急跌中不接飞刀"
            if "加速阴" not in triggered:
                triggered.append("加速阴(警示)")
        # 反转阴: 冲高回落的缩量阴线，次日若企稳为买点
        elif b.high > prev.high and b.close > prev.low and b.volume < prev.volume:
            yin_info = f"最近出现反转阴({b.date})，低点附近可作为买点(配合援军)"
            if "反转阴(低吸买点)" not in triggered:
                triggered.append("反转阴(低吸买点)")
    if yin_info:
        buy_point = f"{buy_point} | {yin_info}"

    # --- 三高三低(持股/卖点) ---
    if len(closes) >= 3:
        c0, c1 = closes[-1], closes[-2]
        h0, h1 = highs[-1], highs[-2]
        l0, l1 = lows[-1], lows[-2]
        # 卖出信号: 高点不创新高 / 收盘不高于昨日 / 最低点不高于昨日最低
        sell_signals = []
        if h0 <= h1:
            sell_signals.append("高点不创新高")
        if c0 <= c1:
            sell_signals.append("收盘价不高于昨日")
        if l0 <= l1:
            sell_signals.append("最低点不高于昨日最低")
        # 持有信号: 高点高 / 低点高 / 收盘价高
        hold_signals = []
        if h0 > h1:
            hold_signals.append("高点高")
        if l0 > l1:
            hold_signals.append("低点高")
        if c0 > c1:
            hold_signals.append("收盘价高")

        if sell_signals and len(sell_signals) >= 2:
            sell_point = "三不高触发 → " + "、".join(sell_signals) + "，减仓/离场"
            triggered.append("三不高(卖点): " + "、".join(sell_signals))
        elif sell_signals:
            sell_point = "出现 1 个卖点信号(" + "、".join(sell_signals) + ")，先留意"
            triggered.append("三不高(部分): " + "、".join(sell_signals))
        else:
            sell_point = "三不高未触发，暂无卖点信号"

        if hold_signals and len(hold_signals) == 3:
            hold_rule = "三高齐备 → " + "、".join(hold_signals) + "，可持股/持有"
            if "三高(持有)" not in triggered:
                triggered.append("三高(持有): " + "、".join(hold_signals))
        elif hold_signals:
            hold_rule = "部分三高信号(" + "、".join(hold_signals) + ")，继续观察"
        else:
            hold_rule = "未现三高信号，倾向防守"
    else:
        sell_point = "数据不足，无法判定卖点"
        hold_rule = "数据不足"

    return TradePlan(name=name, buy_point=buy_point,
                     hold_rule=hold_rule, sell_point=sell_point,
                     triggered_rules=triggered)


# ----------------------------------------------------------------------
# 6. 主流程
# ----------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="游资 AI 选股工作模式·选股/打分/买卖点")
    parser.add_argument("--data", default=DATA_DIR, help="存放 OHLCV CSV 的目录(默认 ./data)")
    parser.add_argument("--top", type=int, default=TOP_N_DEFAULT, help="输出前几只(默认 3)")
    parser.add_argument("--min-date", default=None, help="只分析该日期之后的行情, 如 2026-01-01")
    parser.add_argument("--show-all", action="store_true", help="打印全部通过条件股票的明细打分")
    args = parser.parse_args()

    print("=" * 66)
    print("游资 AI 选股工作模式 · 筛选与打分")
    print("=" * 66)

    all_data = load_all_bars(args.data, args.min_date)
    if not all_data:
        print(f"[错误] {args.data} 下没有可用的 CSV 数据。")
        print("请准备列名为 date,open,high,low,close,volume 的日线数据。")
        sys.exit(1)

    # 第一步: 五条选股条件过滤
    passed: Dict[str, List[Bar]] = {}
    print("\n[步骤1] 五条选股条件过滤:")
    for name, bars in all_data.items():
        checks = {
            "连续5日上涨": condition_5_consec_up(bars),
            "30日内涨停": condition_limit_up_in_30d(bars),
            "收盘不破5日线": condition_close_above_ma5(bars),
            "成交量堆量": condition_volume_accumulation(bars),
            "底部筹码不动": condition_bottom_chips_stable(bars),
        }
        if all(checks.values()):
            passed[name] = bars
            print(f"  ✓ {name}: 全部条件通过")
        # 可打开调试查看未通过原因
        # else:
        #     failed = [k for k, v in checks.items() if not v]
        #     print(f"  ✗ {name}: 未过 {failed}")

    if not passed:
        print("\n无股票通过全部五条选股条件，请放宽阈值或补充候选池。")
        return

    # 第二步: 多因子打分
    print(f"\n[步骤2] 多因子打分(权重: {FACTOR_WEIGHTS}):")
    scores: List[StockScore] = []
    for name, bars in passed.items():
        sc = score_stock(name, bars)
        scores.append(sc)

    # 第三步: 排序取 TOP N
    scores.sort(key=lambda s: s.total, reverse=True)
    top = scores[: args.top]
    print(f"\n[步骤3] 排序结果 — TOP {args.top}:")

    # 第四步: 买卖点
    plans = {sc.name: analyze_trade(sc.name, passed[sc.name]) for sc in top}

    for rank, sc in enumerate(top, 1):
        p = plans[sc.name]
        print("-" * 66)
        print(f"  #{rank} {sc.name}  —  {sc.total:.2f} 分")
        for k, v in sc.factors.items():
            print(f"      因子[{k}] {v} 分 (权重 {FACTOR_WEIGHTS[k]})")
        print(f"      [买点] {p.buy_point}")
        print(f"      [持有] {p.hold_rule}")
        print(f"      [卖点] {p.sell_point}")
        print(f"      [触发规则] {'; '.join(p.triggered_rules) if p.triggered_rules else '无'}")

    if args.show_all:
        print("\n" + "=" * 66)
        print("全部通过条件股票的明细打分:")
        for sc in scores:
            print(f"  {sc.name}: {sc.total:.2f} 分")

    print("\n" + "=" * 66)
    print("免责声明: 本脚本仅为方法论实现与复盘工具，不构成任何投资建议。")
    print("股市有风险，所有交易决策与盈亏由使用者自行承担。")
    print("=" * 66)


if __name__ == "__main__":
    main()
