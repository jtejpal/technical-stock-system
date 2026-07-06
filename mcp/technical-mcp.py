import numpy as np
import pandas as pd
from mcp.server.fastmcp import FastMCP
from yfinance import Ticker
from pandas import DataFrame

from pattern_core import (
    get_swing_points,
    detect_double_top_bottom,
    detect_head_shoulders,
    detect_triangle,
    detect_flag,
    detect_high_tight_flag,
    detect_cup_and_handle,
    detect_range_consolidation,
)

mcp = FastMCP("technical")

def getSMA(df:DataFrame, days:int) -> DataFrame:
    return (df["Open"].rolling(window=days).mean()+df["High"].rolling(window=days).mean()+df["Low"].rolling(window=days).mean()+df["Close"].rolling(window=days).mean()) / 4

def calculate_candle_type(open, high, low, close):
    body = close - open
    total_range = high - low
    body_pct = abs(body) / total_range if total_range > 0 else 0
    upper_wick = high - max(open, close)
    lower_wick = min(open, close) - low

    # Doji - body is tiny relative to range
    if body_pct < 0.1:
        return "doji"

    # Strong candles - body dominates
    if body > 0 and body_pct > 0.6:
        return "strong_bullish"
    if body < 0 and body_pct > 0.6:
        return "strong_bearish"

    # Hammer - small body at top, long lower wick
    if lower_wick > (2 * abs(body)) and upper_wick < abs(body):
        return "hammer" if body > 0 else "hanging_man"

    # Shooting star - small body at bottom, long upper wick
    if upper_wick > (2 * abs(body)) and lower_wick < abs(body):
        return "shooting_star" if body < 0 else "inverted_hammer"

    # Standard
    if body > 0:
        return "bullish"
    if body < 0:
        return "bearish"

    return "neutral"


def price_vs_sma(price, sma, near_threshold=0.02) -> str:
    diff_pct = (price - sma) / sma

    if diff_pct > 0.05:
        return "well_above"  # >5% above
    elif diff_pct > near_threshold:
        return "above"  # 2-5% above
    elif diff_pct >= -near_threshold:
        return "near"  # within ±2%
    elif diff_pct >= -0.05:
        return "below"  # 2-5% below
    else:
        return "well_below"  # >5% below


def sma_stack(price, sma10, sma21, sma50, sma200):
    mas = [sma10, sma21, sma50, sma200]

    if all(mas[i] > mas[i + 1] for i in range(len(mas) - 1)):
        return "bullish"  # perfect bull stack
    elif all(mas[i] < mas[i + 1] for i in range(len(mas) - 1)):
        return "bearish"  # perfect bear stack
    elif sma10 > sma21 > sma50:
        return "partially_bullish"
    elif sma10 < sma21 < sma50:
        return "partially_bearish"
    else:
        return "mixed"

@mcp.tool()
def getOHLCVData(stock_name: str, period: str, interval: str, window_in_days: int) -> dict:
    """
    Fetch daily or weekly OHLCV price data with technical indicators for a stock,
    benchmarked against the S&P 500. Use this tool to investigate price action over
    a specific lookback window before forming or revising a technical thesis.

    Each call returns one row per bar (day or week) containing: raw OHLCV, SMA 10/21/
    50/200, RSI(14), 20-day volume average, derived fields (price_vs_sma for each MA,
    sma_stack, candle_type, body/wick percentages, daily return), and market context
    (S&P 500 return and relative strength for that bar).

    WHEN TO CALL THIS TOOL
    -----------------------
    - Call with a SHORT window (period="1mo" to "3mo", interval="1d", window_in_days
      10-30) to assess immediate momentum, the most recent candle's character, and
      near-term entry/exit timing.
    - Call with a MEDIUM window (period="3mo" to "6mo", interval="1d", window_in_days
      60-120) to identify the current trend phase: is the stock trending, correcting,
      basing, or distributing right now?
    - Call with a LONG window (period="1y" or more, interval="1wk", window_in_days
      26-52) to find macro structure: where the current move originated, the largest
      support/resistance zones, and whether the long-term trend is still intact. Use
      WEEKLY interval for long windows — daily bars over a year add little additional
      signal and consume far more context tokens.
    - If a shorter window doesn't give you enough context to judge whether a level or
      move is significant, call again with a longer window before concluding. Do not
      guess at macro context you have not actually retrieved.
    - You may call this tool more than once per analysis to compare windows directly
      (e.g. call once with a 30-day daily window, then again with a 180-day weekly
      window). Each call should be justified by a specific gap in your understanding.
    - Maximum 6 calls to this tool per analysis. If you reach the limit, proceed to
      your final analysis using the data already gathered and note that in your
      reasoning rather than withholding a conclusion.
    - Always pad `period` well beyond `window_in_days` so SMA_50 and SMA_200 are
      fully populated for every returned row — see CRITICAL: PERIOD PADDING below.
      Never shrink `period` down to just barely cover `window_in_days`.

    PARAMETERS
    ----------
    stock_name : str
        The name of the stock ticker to be analyzed.
    period : str
        How far back to fetch from the data source BEFORE trimming to
        window_in_days. This must be padded well beyond window_in_days, or the
        moving averages for most/all returned rows will be NaN — see CRITICAL:
        PERIOD PADDING below. To extend coverage, increase `period` while leaving
        `interval` unchanged; do not switch interval to compensate for insufficient
        history.
    interval : str
        Bar size: "1d" for daily bars (short/medium windows) or "1wk" for weekly
        bars (long windows, >120 days of history). Do not request daily bars for
        windows longer than ~180 days — use weekly instead to conserve context.
        Choose interval based on the ANALYSIS WINDOW you want (short/medium/long),
        not as a tool for fixing missing SMA data — that is `period`'s job.
    window_in_days : int
        Number of most recent bars to return after indicators are calculated. This
        trims the larger `period` fetch down to the relevant analysis window. It
        does NOT affect how much history is fetched for indicator calculation —
        that is controlled entirely by `period`. Note: when interval="1wk", this
        still refers to number of bars returned (weeks), not calendar days —
        request accordingly (e.g. window_in_days=26 for ~6 months of weekly bars).

    CRITICAL: PERIOD PADDING TO AVOID NaN MOVING AVERAGES
    -------------------------------------------------------
    SMA_200 requires 200 prior bars of history before it produces its first valid
    value (SMA_50 requires 50, SMA_21 requires 21, SMA_10 requires 10). Because the
    tool fetches `period` and only THEN calculates rolling SMAs on that fetched
    range, requesting a `period` that is barely larger than `window_in_days` will
    leave most or all of the returned rows with NaN for SMA_200 and a meaningful
    chunk with NaN for SMA_50.

    Example of the bug: period="6mo", interval="1d", window_in_days=120 fetches
    only ~126 daily bars total. SMA_200 needs 200 bars to compute even one value,
    so EVERY row returned will have SMA_200 = NaN, and roughly the first 50 rows
    will also have SMA_50 = NaN.

    The fix is to pad `period` well beyond `window_in_days`, keeping `interval`
    fixed, and let `window_in_days` control only how many of the resulting
    (fully-populated) rows are returned. Rule of thumb: `period` should cover at
    least (window_in_days + 200) bars at the chosen interval, with extra buffer
    for weekends/holidays on daily data.

    Recommended pairings (interval="1d"):
        window_in_days=20   -> period="1y"   (gives ~250 bars: 200+ buffer, 20 valid rows wanted)
        window_in_days=60   -> period="1y" or "18mo"
        window_in_days=120  -> period="2y"

    Recommended pairings (interval="1wk"):
        window_in_days=26   -> period="5y"   (200 weekly bars ≈ ~4 years; pad accordingly)
        window_in_days=52   -> period="5y" or "6y"

    When in doubt, over-pad `period`. Fetching extra history is cheap; returning
    rows with NaN SMA_50/SMA_200 silently degrades every downstream calculation
    that depends on `sma_stack` or `price_vs_sma200`, which are core to the long-
    window macro trend assessment.

    RETURNS
    -------
    dict with:
        - "ticker": the stock's ticker symbol
        - "window": list of per-bar dicts, oldest to most recent, each containing
          "date", "ohlcv", "mas", "indicators", "derived", and "context" fields.

    NOTES
    -----
    - "atr_14" is not currently populated in "indicators". If volatility context is
      needed, derive a proxy from the high-low range of recent bars and label it
      explicitly as a proxy rather than treating it as true ATR.
    - All numeric fields are pre-formatted strings (2 decimal places) — cast to
      float before performing further arithmetic on them.
    - "relative_strength" in "context" is the stock's daily/weekly return minus the
      S&P 500's return for the same bar — positive values mean the stock
      outperformed the index for that period.
    """

    s_and_p = Ticker("^GSPC")
    df_s_and_p = s_and_p.history(period=period, interval=interval)
    df_s_and_p["sp500_return_pct"] = (df_s_and_p["Close"] - df_s_and_p["Close"].shift(1)) / df_s_and_p["Close"].shift(1) * 100
    stock_data = Ticker(stock_name)
    df = stock_data.history(period=period, interval=interval)
    df["SMA_10"] = getSMA(df, 10)
    df["SMA_21"] = getSMA(df, 21)
    df["SMA_50"] = getSMA(df, 50)
    df["SMA_200"] = getSMA(df, 200)
    df["Volume_Avg_20"] = df["Volume"].rolling(window=20).mean()
    df["volume_vs_avg"] = df['Volume'] / df["Volume_Avg_20"]
    df["body_pct"] = (df['Close'] - df['Open']) / df["Open"] * 100
    df["upper_wick_pct"] = (df['High'] - df['Close']) / df["Open"] * 100
    df["lower_wick_pct"] = (df['Open'] - df['Low']) / df["Open"] * 100

    df["sp500_return_pct"] = df_s_and_p["sp500_return_pct"]
    df["daily_return_pct"] = (df["Close"] - df["Close"].shift(1)) / df["Close"].shift(1) * 100
    df["relative_strength"] = df["daily_return_pct"] - df["sp500_return_pct"]

    delta = df["Close"].diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    df["RSI"] = 100 - (100 / (1 + (gains.rolling(window=14).mean() / losses.rolling(window=14).mean())))

    df["days_ago"] = (df.index.max() - df.index).days
    df["weights"] = np.exp(-df["days_ago"] / 30)

    window = []
    for date, row in df.tail(window_in_days).iterrows():
        price_vs_sma10 =  price_vs_sma(row["Close"], row['SMA_10'])
        price_vs_sma21 = price_vs_sma(row["Close"], row['SMA_21'])
        price_vs_sma50 = price_vs_sma(row["Close"], row['SMA_50'])
        price_vs_sma200 = price_vs_sma(row["Close"], row['SMA_200'])
        sma_stack_result = sma_stack(row["Close"], row['SMA_10'], row['SMA_21'], row['SMA_50'], row['SMA_200'])
        candle_type = calculate_candle_type(row["Open"], row["High"], row["Low"], row["Close"])
        row_formatted = row.map('{:.2f}'.format)
        day_info = {
            "date": date,
            "ohlcv": {
                "open": row_formatted['Open'],
                "high": row_formatted['High'],
                "low": row_formatted['Low'],
                "close": row_formatted['Close'],
                "volume": row_formatted['Volume']
            },
            "mas": {
                "sma_10": row_formatted['SMA_10'],
                "sma_21": row_formatted['SMA_21'],
                "sma_50": row_formatted['SMA_50'],
                "sma_200": row_formatted['SMA_200']
            },
            "indicators": {
                "rsi_14": row_formatted["RSI"],
                # Update price_vs_sma once you know how to calculate atr_14
                # "atr_14": 47.07,
                "volume_avg_20": row_formatted["Volume_Avg_20"]
            },
            "derived": {
                "price_vs_sma10": price_vs_sma10,
                "price_vs_sma21": price_vs_sma21,
                "price_vs_sma50": price_vs_sma50,
                "price_vs_sma200": price_vs_sma200,
                "sma_stack": sma_stack_result,
                "volume_vs_avg": row_formatted["volume_vs_avg"],
                "candle_type": candle_type,
                "body_pct": row_formatted["body_pct"],
                "upper_wick_pct": row_formatted["upper_wick_pct"],
                "lower_wick_pct": row_formatted["lower_wick_pct"],
                "daily_return_pct": row_formatted["daily_return_pct"]
            },
            "context": {
                "sp500_return_pct": row_formatted["sp500_return_pct"],
                "relative_strength": row_formatted["relative_strength"]
                # "analyst_event": "upgrade",
                # "analyst_pt": 1206
            }
        }
        window.append(day_info)
    summary = {
        "ticker": stock_name,
        "window": window
    }
    return summary

@mcp.tool()
def get_support_resistance(
    stock_ticker: str,
    period:str,
    interval: str,
    window_in_days: int,
    window: int = 5,
    cluster_pct: float = 0.015,
    tolerance_pct: float = 0.01,
    decay: float = 30
):
    """
    Identify and rank support and resistance levels from OHLC price data using
    swing detection, clustering, and exponentially decayed touch scoring.

    This tool is designed for market structure analysis over a rolling time window.
    It detects price levels where the market has historically reacted (support/resistance),
    clusters nearby price points into zones, and ranks them by recent relevance using
    trading-day based exponential decay.

    ------------------------------------------------------------------------
    PURPOSE
    ------------------------------------------------------------------------
    This function helps an agent identify:
    - Key SUPPORT levels where price has historically bounced upward
    - Key RESISTANCE levels where price has historically rejected downward
    - The relative strength of each level based on:
        * Number of touches
        * Recency of touches (weighted by exponential decay)
        * Clustering of nearby price zones

    Useful for:
    - Technical analysis
    - Mean reversion strategies
    - Entry/exit decision support
    - Market structure understanding

    ------------------------------------------------------------------------
    INPUTS
    ------------------------------------------------------------------------

    stock_ticker : str
        ticker name to be used for gathering support and resistance levels

    period : str
        How far back to fetch from the data source before trimming to
        window_in_days. Use a value at least as large as window_in_days requires
        (e.g. "1mo", "3mo", "6mo", "1y", "2y"). A larger period than window_in_days
        is fine and is required for indicators like SMA_200 to be populated for the
        earliest rows in the window.
    interval : str
        Bar size: "1d" for daily bars (short/medium windows) or "1wk" for weekly
        bars (long windows, >120 days of history). Do not request daily bars for
        windows longer than ~180 days — use weekly instead to conserve context.
    window_in_days : int
        Number of most recent bars to return after indicators are calculated. This
        trims the larger `period` fetch down to the relevant analysis window. Note:
        when interval="1wk", this still refers to number of bars returned (weeks),
        not calendar days — request accordingly (e.g. window_in_days=26 for ~6
        months of weekly bars).

    window : int (default = 5)
        Rolling window size used for swing detection.

        - Higher values = fewer, more significant swing points
        - Lower values = more granular but noisier levels

        A point is considered:
        - Swing high if it equals the rolling max of High over this window
        - Swing low  if it equals the rolling min of Low over this window

    cluster_pct : float (default = 0.015)
        Percentage threshold used to group nearby swing levels into zones.

        Example:
        - 0.015 = 1.5%
        - Levels within ±1.5% of each other are merged into a single zone

        Purpose:
        - Removes duplicate/near-identical levels
        - Creates meaningful support/resistance "zones" instead of raw prices

    tolerance_pct : float (default = 0.01)
        Percentage tolerance used to define whether a price "touches" a level.

        A touch occurs when:
        - High or Low is within ±tolerance_pct of a level

        Example:
        - 0.01 = 1%
        - If level = 100, any price between 99 and 101 counts as a touch

    decay : float (default = 30)
        Exponential decay factor measured in TRADING DAYS.

        Used to weight recent touches more heavily than older ones.

        Weight formula:
            weight = exp(-trading_days_ago / decay)

        Interpretation:
        - Small decay (e.g. 15) → strongly favors recent levels
        - Large decay (e.g. 60) → smoother, longer memory

    ------------------------------------------------------------------------
    OUTPUT
    ------------------------------------------------------------------------

    Returns:
        dict with structure:

        {
            "supports": [
                {
                    "level": float,
                    "score": float
                },
                ...
            ],
            "resistances": [
                {
                    "level": float,
                    "score": float
                },
                ...
            ]
        }

    OUTPUT FIELDS:

    supports / resistances:
        List of ranked price levels.

    level:
        The clustered price zone representing a support or resistance area.

    score:
        A weighted strength score computed as:

            score = sum(exp(-trading_days_ago / decay))

        where each touch contributes based on how recent it occurred.

    Higher score = stronger, more relevant level.

    ------------------------------------------------------------------------
    NOTES FOR AGENT USE
    ------------------------------------------------------------------------
    - Do NOT treat output levels as exact prices; they represent zones.
    - Prefer higher score levels for decision-making.
    - Combine with trend context (uptrend/downtrend) for best results.
    - Works best on 30–180 day windows of daily data.

    ------------------------------------------------------------------------
    """

    #         -Acceptable values
    #             "1mo" - 1 Month of data
    #             "3mo" - 3 Month of data
    #             "6mo" - 6 Month of data
    #             "1yr" - 1 Year of data
    #             "2yr" - 2 Year of data

    #         -Acceptable values
    #             "1d" - Daily
    #             "1wk" - Weekly
    #             "1mo" - Monthly

    stock_ticker_data = Ticker(stock_ticker)
    df = stock_ticker_data.history(period=period, interval=interval).tail(window_in_days)

    df = add_trading_index(df)

    supports, resistances = get_swing_levels(df, window)

    support_levels = cluster_levels(supports, cluster_pct)
    resistance_levels = cluster_levels(resistances, cluster_pct)

    ranked_supports = rank_levels(
        df,
        support_levels,
        tolerance_pct,
        decay
    )

    ranked_resistances = rank_levels(
        df,
        resistance_levels,
        tolerance_pct,
        decay
    )

    return {
        "supports": ranked_supports,
        "resistances": ranked_resistances
    }

def add_trading_index(df: pd.DataFrame):
    df = df.copy()
    df = df.sort_index()

    df["trading_idx"] = np.arange(len(df))
    return df

def get_swing_levels(df, window=5):
    points = get_swing_points(df, window)
    supports = [p["price"] for p in points if p["type"] == "low"]
    resistances = [p["price"] for p in points if p["type"] == "high"]
    return supports, resistances

def cluster_levels(levels, threshold_pct=0.015):
    if not levels:
        return []

    levels = np.sort(np.array(levels))

    clusters = [[levels[0]]]

    for level in levels[1:]:
        center = np.mean(clusters[-1])

        if abs(level - center) / center <= threshold_pct:
            clusters[-1].append(level)
        else:
            clusters.append([level])

    return [float(np.mean(c)) for c in clusters]

def weighted_touch_score(
    df: pd.DataFrame,
    level: float,
    tolerance_pct: float = 0.01,
    decay: float = 30
):
    highs = df["High"].values
    lows  = df["Low"].values
    idx   = df["trading_idx"].values

    last_idx = idx[-1]

    high_touch = np.abs(highs - level) / level <= tolerance_pct
    low_touch  = np.abs(lows  - level) / level <= tolerance_pct

    touch_mask = high_touch | low_touch

    if not np.any(touch_mask):
        return 0.0

    distances = last_idx - idx[touch_mask]  # trading days ago

    weights = np.exp(-distances / decay)

    return float(np.sum(weights))

def rank_levels(df, levels, tolerance_pct=0.01, decay=30):
    scored = []

    for level in levels:
        score = weighted_touch_score(
            df,
            level,
            tolerance_pct,
            decay
        )

        scored.append({
            "level": round(level, 2),
            "score": score
        })

    return sorted(scored, key=lambda x: x["score"], reverse=True)


@mcp.tool()
def detect_chart_patterns(
    stock_ticker: str,
    period: str,
    interval: str,
    window_in_days: int,
    window: int = 5,
    patterns: list[str] = None,
):
    """
    Scan recent price action for the classic swing-trading chart patterns
    (cup and handle, head & shoulders, triangles, double tops/bottoms,
    flags, range consolidations) and report which are currently forming,
    confirmed, or just broke out -- with the key price levels needed to
    act on each one.

    ------------------------------------------------------------------------
    PURPOSE
    ------------------------------------------------------------------------
    Each detector runs a deterministic geometric/volume check (not a
    visual/LLM judgment call) against swing points derived from OHLCV data.
    Only patterns that pass their criteria are returned -- this is a
    filtered, ranked list, not an exhaustive report.

    Pattern coverage:
        - cup_and_handle
        - head_and_shoulders / inverse_head_and_shoulders
        - ascending_triangle / descending_triangle
        - double_top / double_bottom
        - bull_flag / bear_flag
        - high_tight_flag   (note: needs interval="1wk" -- see below)
        - range_consolidation

    ------------------------------------------------------------------------
    INPUTS
    ------------------------------------------------------------------------
    stock_ticker, period, interval, window_in_days : same semantics as
        get_support_resistance(). interval="1d" works for everything except
        high_tight_flag and cup_and_handle on long-cycle stocks, which need
        interval="1wk" with enough window_in_days to cover ~15-20 weeks.

    window : int (default = 5)
        Swing detection sensitivity, same meaning as in get_support_resistance.
        Higher = fewer, more significant swing points.

    patterns : list[str], optional
        Restrict detection to specific pattern names (see list above).
        Defaults to checking all of them.

    ------------------------------------------------------------------------
    OUTPUT
    ------------------------------------------------------------------------
    {
        "patterns": [
            {
                "pattern": str,
                "status": str,            # e.g. "forming" | "confirmed" |
                                           # "breakout_confirmed" | "handle_forming"
                "confidence": float,       # 0.0-1.0, heuristic -- not a probability
                "key_levels": {...},       # pattern-specific (neckline, pivot, etc.)
                "pattern_start": "YYYY-MM-DD",
                "pattern_end": "YYYY-MM-DD",
                "volume_confirmation": bool
            },
            ...
        ]
    }
    Sorted by confidence, descending. Empty list if nothing matched.

    ------------------------------------------------------------------------
    NOTES FOR AGENT USE
    ------------------------------------------------------------------------
    - "confidence" is a heuristic score from the detection thresholds, not a
      statistically validated win rate -- treat relative ranking as more
      meaningful than the absolute number.
    - A pattern with status "forming" is not yet confirmed -- the agent
      should not treat it as a completed signal. Use key_levels to state
      what would confirm it (e.g. "needs a close above the breakout_level
      on above-average volume").
    - Cross-check volume_confirmation and broader trend context (MAs,
      relative strength) before treating any single pattern as high-quality
      -- per standard technical analysis practice, patterns are most
      reliable in a confirmed uptrend with strong relative strength, not in
      isolation.
    """
    stock_ticker_data = Ticker(stock_ticker)
    df = stock_ticker_data.history(period=period, interval=interval).tail(window_in_days)
    df = add_trading_index(df)

    swings = get_swing_points(df, window)

    detectors = {
        "double_top": lambda: detect_double_top_bottom(df, swings, direction="top"),
        "double_bottom": lambda: detect_double_top_bottom(df, swings, direction="bottom"),
        "head_and_shoulders": lambda: detect_head_shoulders(df, swings, direction="top"),
        "inverse_head_and_shoulders": lambda: detect_head_shoulders(df, swings, direction="bottom"),
        "ascending_triangle": lambda: detect_triangle(df, swings, direction="ascending"),
        "descending_triangle": lambda: detect_triangle(df, swings, direction="descending"),
        "bull_flag": lambda: detect_flag(df, direction="bull"),
        "bear_flag": lambda: detect_flag(df, direction="bear"),
        "high_tight_flag": lambda: detect_high_tight_flag(df),
        "cup_and_handle": lambda: detect_cup_and_handle(df, swings),
        "range_consolidation": lambda: detect_range_consolidation(df),
    }

    if patterns:
        detectors = {k: v for k, v in detectors.items() if k in patterns}

    results = []
    for fn in detectors.values():
        result = fn()
        if result is not None:
            results.append(result)

    results.sort(key=lambda r: r["confidence"], reverse=True)

    return {"patterns": results}

if __name__ == "__main__":
    mcp.run(transport="stdio")
