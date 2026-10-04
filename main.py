def calculate_score(
    zt_count=0,
    lhb_on=False,
    market_cap=0,
    pct=0,
    turnover=0,
    lhb_net=0,
    order_score=0,
    mx_match=False
):
    """
    妖股雷达 V2
    不再要求所有条件同时满足。
    所有条件改为加权评分。
    总分 100 分。
    """

    score = 0
    reasons = []

    # =========================================================
    # 1. 近3个交易日出现涨停 —— 25分
    # =========================================================
    if zt_count >= 1:
        if zt_count >= 3:
            score += 25
            reasons.append("3日3次涨停")
        elif zt_count == 2:
            score += 22
            reasons.append("3日2次涨停")
        else:
            score += 25
            reasons.append("3日内有涨停")

    # =========================================================
    # 2. 上过龙虎榜 —— 20分
    # =========================================================
    if lhb_on:
        score += 20
        reasons.append("上过龙虎榜")

    # =========================================================
    # 3. 市值 —— 15分
    # 不再把300亿作为硬过滤
    # =========================================================
    if market_cap > 0:
        if market_cap <= 100:
            score += 15
            reasons.append("市值≤100亿")
        elif market_cap <= 300:
            score += 15
            reasons.append("市值≤300亿")
        elif market_cap <= 500:
            score += 10
            reasons.append("市值≤500亿")
        elif market_cap <= 1000:
            score += 5
            reasons.append("市值≤1000亿")

    # =========================================================
    # 4. 涨幅 —— 15分
    # =========================================================
    if pct >= 9:
        score += 15
        reasons.append("涨幅强")
    elif pct >= 7:
        score += 13
        reasons.append("涨幅较强")
    elif pct >= 5:
        score += 10
        reasons.append("涨幅>5%")
    elif pct >= 3:
        score += 7
    elif pct > 0:
        score += 4

    # =========================================================
    # 5. 换手率 —— 10分
    # =========================================================
    if turnover >= 20:
        score += 10
        reasons.append("高换手")
    elif turnover >= 15:
        score += 9
        reasons.append("换手活跃")
    elif turnover >= 10:
        score += 7
        reasons.append("换手较强")
    elif turnover >= 5:
        score += 5
    elif turnover >= 3:
        score += 3

    # =========================================================
    # 6. 龙虎榜资金 —— 10分
    # lhb_net 单位：元
    # =========================================================
    if lhb_net > 0:
        if lhb_net >= 50000000:
            score += 10
            reasons.append("龙虎榜大额净流入")
        elif lhb_net >= 20000000:
            score += 8
            reasons.append("龙虎榜净流入")
        elif lhb_net >= 5000000:
            score += 6
        elif lhb_net > 0:
            score += 3
    elif lhb_net < -50000000:
        score -= 5
    elif lhb_net < -20000000:
        score -= 3

    # =========================================================
    # 7. 盘口 —— 5分
    # =========================================================
    if order_score:
        score += max(-5, min(5, order_score))

        if order_score >= 4:
            reasons.append("盘口偏强")
        elif order_score <= -3:
            reasons.append("盘口偏弱")

    # =========================================================
    # 8. 妙想增强 —— 5分
    # 只做增强，不参与硬过滤
    # =========================================================
    if mx_match:
        score += 5
        reasons.append("妙想增强")

    # 限制在0~100
    score = max(0, min(100, score))

    # =========================================================
    # 妖股概率
    # 注意：这是模型评分映射，不是真实统计学概率
    # =========================================================
    probability = round(30 + score * 0.68, 1)
    probability = min(98.0, probability)

    return {
        "score": score,
        "probability": probability,
        "reasons": reasons
    }


def build_scan(date):
    """
    妖股雷达核心扫描

    重要：
    不再要求：
        3日涨停 + 龙虎榜 + 市值≤300亿
    三项全部满足。

    改成：
        三项分别计分
        +涨幅
        +换手
        +龙虎榜资金
        +盘口
        +妙想增强

    最终按总分/妖股概率排名。
    """

    actual_date = get_actual_target_date(date)

    # ---------------------------------------------------------
    # 最近交易日
    # ---------------------------------------------------------
    trading_dates = get_trading_dates(actual_date, 10)

    if not trading_dates:
        trading_dates = [actual_date]

    last3_dates = trading_dates[:3]

    # ---------------------------------------------------------
    # 近3日涨停
    # ---------------------------------------------------------
    zt_codes = set()
    zt_counts = {}

    try:
        zt_counts = get_limit_up_counts(last3_dates)

        if isinstance(zt_counts, dict):
            zt_codes = set(zt_counts.keys())

    except Exception:
        zt_counts = {}
        zt_codes = set()

    # ---------------------------------------------------------
    # 龙虎榜
    # ---------------------------------------------------------
    try:
        lhb_data = get_lhb(
            trading_dates[:10]
        )
    except Exception:
        lhb_data = {}

    if not isinstance(lhb_data, dict):
        lhb_data = {}

    lhb_codes = set(lhb_data.keys())

    # ---------------------------------------------------------
    # 妙想
    # 妙想只增强，不作为硬条件
    # ---------------------------------------------------------
    mx_codes = set()

    if actual_date == TODAY:
        try:
            mx_codes = set(get_mx_candidates() or [])
        except Exception:
            mx_codes = set()

    # ---------------------------------------------------------
    # 候选池
    #
    # 不再要求三项全部满足。
    #
    # 只要：
    #   ① 近3日有涨停
    #   或
    #   ② 上过龙虎榜
    #   或
    #   ③ 妙想发现
    #
    # 就进入评分。
    # ---------------------------------------------------------
    candidate_pool = (
        zt_codes
        | lhb_codes
        | mx_codes
    )

    # 去掉ST、退市等
    candidate_pool = {
        clean_code(x)
        for x in candidate_pool
        if clean_code(x)
    }

    # ---------------------------------------------------------
    # 获取行情
    # ---------------------------------------------------------
    quotes = {}

    if candidate_pool:
        try:
            quotes = get_quotes(list(candidate_pool))
        except Exception:
            quotes = {}

    if not isinstance(quotes, dict):
        quotes = {}

    # ---------------------------------------------------------
    # 为了避免 Render 超时：
    # 最多处理前80只候选。
    #
    # 优先：
    # 涨幅高 + 换手高
    # ---------------------------------------------------------
    def candidate_sort_key(code):
        q = quotes.get(code, {})

        pct = safe_float(
            q.get("pct",
                  q.get("f3", 0))
        )

        turnover = safe_float(
            q.get("turnover",
                  q.get("f8", 0))
        )

        return (
            pct * 2 + turnover,
            pct,
            turnover
        )

    candidate_list = sorted(
        candidate_pool,
        key=candidate_sort_key,
        reverse=True
    )[:80]

    # ---------------------------------------------------------
    # 读取K线
    # 历史日期需要K线计算当日收盘及历史市值
    # ---------------------------------------------------------
    kline_map = {}

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def load_one_kline(code):
            try:
                return code, get_kline(
                    code,
                    actual_date,
                    20
                )
            except Exception:
                return code, []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(load_one_kline, code)
                for code in candidate_list
            ]

            for future in as_completed(futures):
                try:
                    code, rows = future.result()
                    kline_map[code] = rows
                except Exception:
                    pass

    except Exception:
        for code in candidate_list:
            try:
                kline_map[code] = get_kline(
                    code,
                    actual_date,
                    20
                )
            except Exception:
                kline_map[code] = []

    # ---------------------------------------------------------
    # 第一轮评分
    # ---------------------------------------------------------
    results = []

    for code in candidate_list:

        q = quotes.get(code, {})

        if not isinstance(q, dict):
            q = {}

        name = q.get(
            "name",
            q.get("f14", code)
        )

        # ST过滤
        if is_st(name):
            continue

        # -----------------------------------------------------
        # 当前行情
        # -----------------------------------------------------
        current_price = safe_float(
            q.get("price", q.get("f2", 0))
        )

        pct = safe_float(
            q.get("pct", q.get("f3", 0))
        )

        turnover = safe_float(
            q.get("turnover", q.get("f8", 0))
        )

        market_cap = safe_float(
            q.get("market_cap", q.get("f20", 0))
        )

        # 东方财富市值原始单位通常为元
        if market_cap > 100000:
            market_cap = market_cap / 100000000

        # -----------------------------------------------------
        # 历史日期
        # -----------------------------------------------------
        hist_rows = kline_map.get(code, [])

        hist_close = current_price

        if hist_rows:
            try:
                last_row = hist_rows[-1]

                if isinstance(last_row, dict):
                    hist_close = safe_float(
                        last_row.get(
                            "close",
                            last_row.get(
                                "c",
                                last_row.get("f2", current_price)
                            )
                        )
                    )

                    hist_pct = safe_float(
                        last_row.get(
                            "pct",
                            last_row.get(
                                "zdp",
                                last_row.get("f3", pct)
                            )
                        )
                    )

                    hist_turnover = safe_float(
                        last_row.get(
                            "turnover",
                            last_row.get(
                                "hsl",
                                last_row.get("f8", turnover)
                            )
                        )
                    )

                    if hist_pct != 0:
                        pct = hist_pct

                    if hist_turnover != 0:
                        turnover = hist_turnover

            except Exception:
                pass

        # -----------------------------------------------------
        # 历史市值估算
        # 当前市值 × 历史收盘价 / 当前价格
        # -----------------------------------------------------
        historical_market_cap = market_cap

        if (
            actual_date != TODAY
            and market_cap > 0
            and current_price > 0
            and hist_close > 0
        ):
            historical_market_cap = (
                market_cap
                * hist_close
                / current_price
            )

        # -----------------------------------------------------
        # 涨停次数
        # -----------------------------------------------------
        zt_count = int(
            zt_counts.get(code, 0)
        )

        # -----------------------------------------------------
        # 龙虎榜
        # -----------------------------------------------------
        lhb_on = code in lhb_codes

        lhb_item = lhb_data.get(code, {})

        if not isinstance(lhb_item, dict):
            lhb_item = {}

        lhb_net = safe_float(
            lhb_item.get(
                "net",
                lhb_item.get(
                    "lhb_net",
                    lhb_item.get("net_buy", 0)
                )
            )
        )

        # 如果接口返回的是万元
        if 0 < abs(lhb_net) < 1000000:
            lhb_net = lhb_net * 10000

        # -----------------------------------------------------
        # 妙想
        # -----------------------------------------------------
        mx_match = code in mx_codes

        # -----------------------------------------------------
        # 第一轮评分
        # 盘口暂时为0
        # -----------------------------------------------------
        score_info = calculate_score(
            zt_count=zt_count,
            lhb_on=lhb_on,
            market_cap=historical_market_cap,
            pct=pct,
            turnover=turnover,
            lhb_net=lhb_net,
            order_score=0,
            mx_match=mx_match
        )

        # -----------------------------------------------------
        # 硬条件命中数量
        # 只是展示，不再作为淘汰条件
        # -----------------------------------------------------
        hard_count = 0

        if zt_count >= 1:
            hard_count += 1

        if lhb_on:
            hard_count += 1

        if (
            historical_market_cap > 0
            and historical_market_cap <= 300
        ):
            hard_count += 1

        # -----------------------------------------------------
        # 记录
        # -----------------------------------------------------
        results.append({
            "code": code,
            "name": name,
            "price": current_price,
            "close": hist_close,
            "pct": round(pct, 2),
            "turnover": round(turnover, 2),
            "market_cap": round(
                historical_market_cap, 2
            ),
            "zt_count": zt_count,
            "lhb": lhb_on,
            "lhb_net": round(
                lhb_net / 10000,
                2
            ),
            "mx_match": mx_match,

            "hard_count": hard_count,

            "score": score_info["score"],
            "probability": score_info["probability"],
            "reasons": score_info["reasons"],

            "order_score": 0
        })

    # ---------------------------------------------------------
    # 第一次排序
    # ---------------------------------------------------------
    results.sort(
        key=lambda x: (
            x["score"],
            x["probability"],
            x["pct"],
            x["turnover"]
        ),
        reverse=True
    )

    # ---------------------------------------------------------
    # 当前交易日：
    # 只给前12名查询盘口
    #
    # 不再给所有股票查询，避免 Render 超时。
    # ---------------------------------------------------------
    if actual_date == TODAY and results:

        top_codes = [
            x["code"]
            for x in results[:12]
        ]

        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def load_order(code):
                try:
                    return code, get_order_book(code)
                except Exception:
                    return code, None

            order_map = {}

            with ThreadPoolExecutor(max_workers=6) as executor:

                futures = [
                    executor.submit(load_order, code)
                    for code in top_codes
                ]

                for future in as_completed(futures):
                    try:
                        code, order = future.result()

                        if order:
                            order_map[code] = order

                    except Exception:
                        pass

        except Exception:
            order_map = {}

        # -----------------------------------------------------
        # 根据盘口重新评分
        # -----------------------------------------------------
        for item in results[:12]:

            code = item["code"]

            order = order_map.get(code)

            if not order:
                continue

            order_score = 0

            try:
                # 兼容不同字段名称
                buy = safe_float(
                    order.get(
                        "buy_amount",
                        order.get(
                            "buy",
                            order.get("bid_amount", 0)
                        )
                    )
                )

                sell = safe_float(
                    order.get(
                        "sell_amount",
                        order.get(
                            "sell",
                            order.get("ask_amount", 0)
                        )
                    )
                )

                if buy > 0 and sell > 0:

                    ratio = buy / sell

                    if ratio >= 2:
                        order_score = 5
                    elif ratio >= 1.5:
                        order_score = 4
                    elif ratio >= 1.2:
                        order_score = 3
                    elif ratio >= 1:
                        order_score = 1
                    elif ratio >= 0.8:
                        order_score = -1
                    elif ratio >= 0.6:
                        order_score = -3
                    else:
                        order_score = -5

            except Exception:
                order_score = 0

            item["order_score"] = order_score

            # 重新评分
            score_info = calculate_score(
                zt_count=item["zt_count"],
                lhb_on=item["lhb"],
                market_cap=item["market_cap"],
                pct=item["pct"],
                turnover=item["turnover"],
                lhb_net=item["lhb_net"] * 10000,
                order_score=order_score,
                mx_match=item["mx_match"]
            )

            item["score"] = score_info["score"]
            item["probability"] = score_info["probability"]
            item["reasons"] = score_info["reasons"]

        # 重新排序
        results.sort(
            key=lambda x: (
                x["score"],
                x["probability"],
                x["pct"],
                x["turnover"]
            ),
            reverse=True
        )

    # ---------------------------------------------------------
    # TOP3
    # 不再要求三项硬条件全部满足
    # ---------------------------------------------------------
    top3 = results[:3]

    # ---------------------------------------------------------
    # 前20名排行榜
    # ---------------------------------------------------------
    ranking = results[:20]

    # ---------------------------------------------------------
    # 为兼容原来的前端：
    # hard_results 不再表示“全部硬条件满足”
    # 而是当前评分结果。
    # ---------------------------------------------------------
    hard_results = results

    # ---------------------------------------------------------
    # 返回
    # ---------------------------------------------------------
    return {
        "date": date,
        "actual_date": actual_date,

        "top3": top3,

        "ranking": ranking,

        "hard_results": hard_results,

        "count": len(results),

        "strategy": {
            "mode": "weighted",
            "require_all": False,
            "weights": {
                "3日涨停": 25,
                "龙虎榜": 20,
                "市值": 15,
                "涨幅": 15,
                "换手率": 10,
                "龙虎榜资金": 10,
                "盘口": 5
            }
        },

        "note": (
            "现在采用加权评分，不要求股票同时满足全部条件。"
            "近3日涨停、龙虎榜、市值≤300亿均为加分项，"
            "最终按照妖股概率综合排名。"
        ),

        "source": (
            "东方财富公开行情"
            + (" + 妙想增强" if mx_codes else "")
        )
    }
