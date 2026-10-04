def build_scan(target_date):

    requested_date = normalize_date(
        target_date
    )

    actual_date, trading_dates = (
        get_actual_target_date(
            requested_date
        )
    )

    if not trading_dates:

        return {
            "requested_date":
                requested_date,

            "date":
                actual_date,

            "count": 0,

            "hard_count": 0,

            "top3": [],

            "ranking": [],

            "source":
                "东方财富公开行情",

            "note":
                "没有获取到交易日"
        }

    # ========================================================
    # 最近3个交易日
    # ========================================================

    last3 = trading_dates[:3]

    # ========================================================
    # 一次读取3天涨停池
    # 同时统计涨停次数
    # ========================================================

    zt_codes = set()

    zt_counts = {}

    for trade_date in last3:

        try:

            pool = get_zt_pool(
                trade_date
            )

        except Exception:

            pool = []

        for item in pool:

            code = clean_code(
                item.get("c")
                or item.get("code")
                or item.get(
                    "SECURITY_CODE"
                )
            )

            if not code:
                continue

            zt_codes.add(code)

            zt_counts[code] = (
                zt_counts.get(
                    code,
                    0
                ) + 1
            )

    # ========================================================
    # 最近10个交易日龙虎榜
    # ========================================================

    lhb_dates = trading_dates[:10]

    try:

        lhb_all = get_lhb(
            None,
            lhb_dates
        )

    except Exception:

        lhb_all = {}

    if not isinstance(
        lhb_all,
        dict
    ):
        lhb_all = {}

    lhb_codes = set(
        lhb_all.keys()
    )

    # ========================================================
    # 核心硬条件
    #
    # 必须：
    # 近3日涨停
    # +
    # 龙虎榜
    #
    # 市值后面继续过滤 <=300亿
    # ========================================================

    candidate_codes = (
        zt_codes
        &
        lhb_codes
    )

    candidate_codes = set(
        unique_codes(
            candidate_codes
        )
    )

    if not candidate_codes:

        return {
            "requested_date":
                requested_date,

            "date":
                actual_date,

            "trading_dates":
                trading_dates,

            "count": 0,

            "hard_count": 0,

            "top3": [],

            "ranking": [],

            "source":
                "东方财富公开行情",

            "note":
                "当前日期没有同时满足近3日涨停 + 龙虎榜的股票"
        }

    # ========================================================
    # 行情
    # ========================================================

    quotes = get_quotes(
        list(candidate_codes)
    )

    if not isinstance(
        quotes,
        dict
    ):
        quotes = {}

    # ========================================================
    # 历史日期：
    # 并行读取K线
    #
    # 当前日期不需要K线
    # ========================================================

    history_map = {}

    if actual_date != TODAY:

        from concurrent.futures import (
            ThreadPoolExecutor,
            as_completed
        )

        def load_history(code):

            try:

                rows = get_kline(
                    code,
                    actual_date,
                    120
                )

                history = next(
                    (
                        row
                        for row in rows
                        if row.get("date")
                        == actual_date
                    ),
                    None
                )

                return code, history

            except Exception:

                return code, None

        with ThreadPoolExecutor(
            max_workers=8
        ) as executor:

            futures = [
                executor.submit(
                    load_history,
                    code
                )
                for code in candidate_codes
            ]

            for future in as_completed(
                futures
            ):

                try:

                    code, history = (
                        future.result()
                    )

                    history_map[code] = (
                        history
                    )

                except Exception:
                    pass

    # ========================================================
    # 第一轮结果
    # ========================================================

    results = []

    for code in candidate_codes:

        quote = quotes.get(
            code,
            {}
        )

        if not isinstance(
            quote,
            dict
        ):
            quote = {}

        name = str(
            quote.get("name")
            or code
        )

        # ----------------------------------------------------
        # ST过滤
        # ----------------------------------------------------

        if is_st(name):
            continue

        # ----------------------------------------------------
        # 当前行情
        # ----------------------------------------------------

        current_price = safe_float(
            quote.get("price")
        )

        current_cap = safe_float(
            quote.get("market_cap")
        )

        # ----------------------------------------------------
        # 历史数据
        # ----------------------------------------------------

        if actual_date == TODAY:

            history = {
                "date":
                    actual_date,

                "close":
                    current_price,

                "pct":
                    safe_float(
                        quote.get("pct")
                    ),

                "turnover":
                    safe_float(
                        quote.get("turnover")
                    )
            }

        else:

            history = history_map.get(
                code
            )

            if not history:
                continue

        # ----------------------------------------------------
        # 历史市值估算
        # ----------------------------------------------------

        price = safe_float(
            history.get("close")
        )

        if actual_date == TODAY:

            market_cap = current_cap

            cap_estimated = False

        else:

            if (
                current_cap <= 0
                or current_price <= 0
                or price <= 0
            ):
                continue

            market_cap = (
                current_cap
                * price
                / current_price
            )

            cap_estimated = True

        # ====================================================
        # 三项核心硬条件
        # ====================================================

        zt_count = int(
            zt_counts.get(
                code,
                0
            )
        )

        lhb_item = lhb_all.get(
            code,
            {}
        )

        if not isinstance(
            lhb_item,
            dict
        ):
            lhb_item = {}

        lhb_on = bool(
            lhb_item.get(
                "on_board",
                False
            )
        )

        # ----------------------------------------------------
        # 市值必须 <=300亿
        # ----------------------------------------------------

        if (
            market_cap <= 0
            or market_cap > 300
        ):
            continue

        # ----------------------------------------------------
        # 最终确认三项硬条件
        # ----------------------------------------------------

        if zt_count < 1:
            continue

        if not lhb_on:
            continue

        # ====================================================
        # 第一轮评分
        # 盘口先不查
        # 妙想先不查
        # ====================================================

        scoring = calculate_score(
            actual_date,
            quote,
            history,
            lhb_item,
            zt_count,
            None,
            False
        )

        result = {

            "code":
                code,

            "name":
                name,

            "date":
                actual_date,

            "price":
                scoring["price"],

            "pct":
                scoring["pct"],

            "turnover":
                scoring["turnover"],

            "market_cap":
                scoring["market_cap"],

            "market_cap_estimated":
                scoring[
                    "market_cap_estimated"
                ],

            "zt3":
                zt_count,

            "zt_count":
                zt_count,

            "lhb_on":
                True,

            "lhb_days":
                lhb_item.get(
                    "days",
                    0
                ),

            "lhb_day_net":
                lhb_item.get(
                    "day_net",
                    0
                ),

            "lhb_net":
                lhb_item.get(
                    "day_net",
                    0
                ),

            "lhb_net10":
                lhb_item.get(
                    "net10",
                    0
                ),

            "order_buy":
                None,

            "order_sell":
                None,

            "order_imbalance":
                None,

            "order_score":
                0,

            "score":
                scoring["score"],

            "tier":
                scoring["tier"],

            "hard":
                [
                    "近3日涨停",
                    "龙虎榜",
                    "市值≤300亿"
                ],

            "hard_ok":
                True,

            "mx_match":
                False
        }

        results.append(
            result
        )

    # ========================================================
    # 第一轮排序
    # ========================================================

    results.sort(
        key=lambda x: (
            x["score"],
            x["zt3"],
            x["lhb_days"],
            x["lhb_net10"],
            x["pct"],
            x["turnover"]
        ),
        reverse=True
    )

    # ========================================================
    # 妙想增强
    #
    # 只增强已经通过三项硬条件的股票
    # 妙想失败不影响主扫描
    # ========================================================

    mx_codes = set()

    if (
        actual_date == TODAY
        and results
    ):

        try:

            mx_list = get_mx_candidates()

            if isinstance(
                mx_list,
                list
            ):

                mx_codes = {
                    clean_code(
                        x.get("code")
                    )
                    for x in mx_list
                    if isinstance(x, dict)
                    and clean_code(
                        x.get("code")
                    )
                }

        except Exception:

            mx_codes = set()

    # ========================================================
    # 给妙想命中的股票 +5
    # ========================================================

    if mx_codes:

        for item in results:

            if item["code"] in mx_codes:

                item["mx_match"] = True

                scoring = calculate_score(
                    actual_date,
                    quotes.get(
                        item["code"],
                        {}
                    ),
                    (
                        {
                            "date":
                                actual_date,

                            "close":
                                item["price"],

                            "pct":
                                item["pct"],

                            "turnover":
                                item["turnover"]
                        }
                        if actual_date == TODAY
                        else history_map.get(
                            item["code"]
                        )
                    ),
                    lhb_all.get(
                        item["code"],
                        {}
                    ),
                    item["zt_count"],
                    None,
                    True
                )

                item["score"] = (
                    scoring["score"]
                )

                item["tier"] = (
                    scoring["tier"]
                )

    # ========================================================
    # 重新排序
    # ========================================================

    results.sort(
        key=lambda x: (
            x["score"],
            x["zt3"],
            x["lhb_days"],
            x["lhb_net10"],
            x["pct"],
            x["turnover"]
        ),
        reverse=True
    )

    # ========================================================
    # 当前交易日只查询前10名盘口
    #
    # 不再给所有股票请求盘口
    # ========================================================

    if (
        actual_date == TODAY
        and results
    ):

        top_codes = [
            x["code"]
            for x in results[:10]
        ]

        order_map = {}

        from concurrent.futures import (
            ThreadPoolExecutor,
            as_completed
        )

        def load_order(code):

            try:

                return (
                    code,
                    get_order_book(code)
                )

            except Exception:

                return (
                    code,
                    None
                )

        with ThreadPoolExecutor(
            max_workers=5
        ) as executor:

            futures = [
                executor.submit(
                    load_order,
                    code
                )
                for code in top_codes
            ]

            for future in as_completed(
                futures
            ):

                try:

                    code, order = (
                        future.result()
                    )

                    if order:
                        order_map[code] = (
                            order
                        )

                except Exception:
                    pass

        # ----------------------------------------------------
        # 重新评分
        # ----------------------------------------------------

        for item in results[:10]:

            code = item["code"]

            order = order_map.get(
                code
            )

            if not order:
                continue

            scoring = calculate_score(
                TODAY,
                quotes.get(
                    code,
                    {}
                ),
                {
                    "close":
                        item["price"],

                    "pct":
                        item["pct"],

                    "turnover":
                        item["turnover"]
                },
                lhb_all.get(
                    code,
                    {}
                ),
                item["zt_count"],
                order,
                item["mx_match"]
            )

            item["order_buy"] = (
                order.get("buy")
            )

            item["order_sell"] = (
                order.get("sell")
            )

            item["order_imbalance"] = (
                order.get(
                    "imbalance"
                )
            )

            item["order_score"] = (
                scoring["order_score"]
            )

            item["score"] = (
                scoring["score"]
            )

            item["tier"] = (
                scoring["tier"]
            )

    # ========================================================
    # 最终排序
    # ========================================================

    results.sort(
        key=lambda x: (
            x["score"],
            x["zt3"],
            x["lhb_days"],
            x["lhb_net10"],
            x["pct"],
            x["turnover"]
        ),
        reverse=True
    )

    # ========================================================
    # TOP3
    # ========================================================

    hard_results = [
        x
        for x in results
        if x.get("hard_ok")
    ]

    top3 = hard_results[:3]

    # ========================================================
    # 返回
    # ========================================================

    return {

        "requested_date":
            requested_date,

        "date":
            actual_date,

        "trading_dates":
            trading_dates,

        "count":
            len(results),

        "hard_count":
            len(hard_results),

        "top3":
            top3,

        "ranking":
            results[:50],

        "source":
            (
                "东方财富公开行情"
                +
                (
                    " + 妙想增强"
                    if mx_codes
                    else ""
                )
            ),

        "note":
            (
                "核心硬条件保持不变："
                "近3个交易日出现涨停 + "
                "上过龙虎榜 + "
                "总市值≤300亿。"
                "在满足三项硬条件的股票中，"
                "再按涨停次数、涨幅、换手率、"
                "龙虎榜资金、盘口及妙想增强进行加权排名。"
            )
    }
