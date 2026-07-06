"""
Core swing-point and chart-pattern detection logic.
Kept separate from the MCP tool / yfinance wrapper so it can be tested
against synthetic OHLCV data before touching live data.
"""

import numpy as np
import pandas as pd


def get_swing_points(df: pd.DataFrame, window: int = 5):
    """
    Returns a chronological list of swing points:
        [{"date": Timestamp, "idx": int, "price": float, "type": "high"|"low"}, ...]

    A point is a swing high if its High equals the centered rolling max over
    `window` bars; a swing low if its Low equals the centered rolling min.
    Consecutive same-type points (which the rolling-window method can
    produce when two nearby bars both qualify) are collapsed to the single
    most extreme point, since pattern detection requires strict
    high/low/high/low alternation to mean anything.
    """
    highs = df["High"]
    lows = df["Low"]

    swing_high_mask = highs == highs.rolling(window, center=True).max()
    swing_low_mask = lows == lows.rolling(window, center=True).min()

    points = []
    for ts, row in df.loc[swing_high_mask].iterrows():
        points.append({
            "date": ts,
            "idx": int(row["trading_idx"]),
            "price": float(row["High"]),
            "type": "high",
        })
    for ts, row in df.loc[swing_low_mask].iterrows():
        points.append({
            "date": ts,
            "idx": int(row["trading_idx"]),
            "price": float(row["Low"]),
            "type": "low",
        })

    points.sort(key=lambda p: p["idx"])

    deduped = []
    for p in points:
        if deduped and deduped[-1]["type"] == p["type"]:
            prev = deduped[-1]
            if p["type"] == "high" and p["price"] > prev["price"]:
                deduped[-1] = p
            elif p["type"] == "low" and p["price"] < prev["price"]:
                deduped[-1] = p
            # else: keep prev, drop p (it's the less extreme duplicate)
        else:
            deduped.append(p)

    return deduped


def fit_trendline(points):
    """Least-squares line through swing points. Returns slope, intercept, r2."""
    if len(points) < 2:
        return None
    idx = np.array([p["idx"] for p in points], dtype=float)
    price = np.array([p["price"] for p in points], dtype=float)
    slope, intercept = np.polyfit(idx, price, 1)
    pred = slope * idx + intercept
    ss_res = np.sum((price - pred) ** 2)
    ss_tot = np.sum((price - np.mean(price)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {"slope": float(slope), "intercept": float(intercept), "r2": float(r2)}


def _r(x, decimals=2):
    """Round to a plain python float (avoids leaking numpy types into tool output)."""
    if x is None:
        return None
    return float(round(float(x), decimals))


def _volume_confirmation(df, lookback=20, multiple=1.2):
    if len(df) < 2:
        return False
    recent_vol = df["Volume"].iloc[-1]
    avg_vol = df["Volume"].tail(lookback).mean()
    if avg_vol == 0 or np.isnan(avg_vol):
        return False
    return bool(recent_vol > avg_vol * multiple)


# ---------------------------------------------------------------------------
# DOUBLE TOP / DOUBLE BOTTOM
# ---------------------------------------------------------------------------

def detect_double_top_bottom(df, swings, direction="top",
                              tolerance_pct=0.03, min_separation_pct=0.03):
    peak_type = "high" if direction == "top" else "low"
    trough_type = "low" if direction == "top" else "high"

    relevant = [p for p in swings if p["type"] in (peak_type, trough_type)]
    if len(relevant) < 3:
        return None

    for i in range(len(relevant) - 3, -1, -1):
        p1, t, p2 = relevant[i], relevant[i + 1], relevant[i + 2]
        if p1["type"] != peak_type or t["type"] != trough_type or p2["type"] != peak_type:
            continue

        peak_diff_pct = abs(p1["price"] - p2["price"]) / p1["price"]
        if peak_diff_pct > tolerance_pct:
            continue

        if direction == "top":
            sep_pct = (min(p1["price"], p2["price"]) - t["price"]) / t["price"]
        else:
            sep_pct = (t["price"] - max(p1["price"], p2["price"])) / max(p1["price"], p2["price"])

        if sep_pct < min_separation_pct:
            continue

        neckline = t["price"]
        last_close = df["Close"].iloc[-1]

        if direction == "top":
            status = "confirmed" if last_close < neckline else "forming"
        else:
            status = "confirmed" if last_close > neckline else "forming"

        confidence = round(min(1.0, max(0.0, 0.5 + sep_pct * 2 - peak_diff_pct * 5)), 2)

        return {
            "pattern": f"double_{direction}",
            "status": status,
            "confidence": _r(confidence),
            "key_levels": {
                "peak_1": _r(p1["price"]),
                "peak_2": _r(p2["price"]),
                "neckline": _r(neckline),
            },
            "pattern_start": str(p1["date"].date()),
            "pattern_end": str(p2["date"].date()),
            "volume_confirmation": _volume_confirmation(df) if status == "confirmed" else False,
        }

    return None


# ---------------------------------------------------------------------------
# HEAD AND SHOULDERS / INVERSE HEAD AND SHOULDERS
# ---------------------------------------------------------------------------

def detect_head_shoulders(df, swings, direction="top",
                           shoulder_tolerance_pct=0.06, head_margin_pct=0.02):
    shoulder_type = "high" if direction == "top" else "low"
    neck_type = "low" if direction == "top" else "high"

    if len(swings) < 5:
        return None

    for i in range(len(swings) - 5, -1, -1):
        pts = swings[i:i + 5]
        types = [p["type"] for p in pts]
        if types != [shoulder_type, neck_type, shoulder_type, neck_type, shoulder_type]:
            continue

        ls, n1, head, n2, rs = pts

        if direction == "top":
            head_ok = (head["price"] > ls["price"] * (1 + head_margin_pct) and
                       head["price"] > rs["price"] * (1 + head_margin_pct))
        else:
            head_ok = (head["price"] < ls["price"] * (1 - head_margin_pct) and
                       head["price"] < rs["price"] * (1 - head_margin_pct))
        if not head_ok:
            continue

        shoulder_diff_pct = abs(ls["price"] - rs["price"]) / ls["price"]
        if shoulder_diff_pct > shoulder_tolerance_pct:
            continue

        if n2["idx"] == n1["idx"]:
            continue
        neckline_slope = (n2["price"] - n1["price"]) / (n2["idx"] - n1["idx"])
        last_idx = int(df["trading_idx"].iloc[-1])
        neckline_at_last = n1["price"] + neckline_slope * (last_idx - n1["idx"])

        last_close = df["Close"].iloc[-1]
        if direction == "top":
            status = "confirmed" if last_close < neckline_at_last else "forming"
        else:
            status = "confirmed" if last_close > neckline_at_last else "forming"

        confidence = round(min(1.0, max(0.0, 0.6 - shoulder_diff_pct * 3 + head_margin_pct)), 2)
        pattern_name = "head_and_shoulders" if direction == "top" else "inverse_head_and_shoulders"

        return {
            "pattern": pattern_name,
            "status": status,
            "confidence": _r(confidence),
            "key_levels": {
                "left_shoulder": _r(ls["price"]),
                "head": _r(head["price"]),
                "right_shoulder": _r(rs["price"]),
                "neckline_current": _r(neckline_at_last),
            },
            "pattern_start": str(ls["date"].date()),
            "pattern_end": str(rs["date"].date()),
            "volume_confirmation": _volume_confirmation(df) if status == "confirmed" else False,
        }

    return None


# ---------------------------------------------------------------------------
# ASCENDING / DESCENDING TRIANGLE
# ---------------------------------------------------------------------------

def detect_triangle(df, swings, direction="ascending",
                     flat_slope_threshold=0.0008, min_r2=0.5, lookback_points=4):
    highs = [p for p in swings if p["type"] == "high"][-lookback_points:]
    lows = [p for p in swings if p["type"] == "low"][-lookback_points:]

    if len(highs) < 2 or len(lows) < 2:
        return None

    high_line = fit_trendline(highs)
    low_line = fit_trendline(lows)
    if high_line is None or low_line is None:
        return None

    avg_price = df["Close"].tail(20).mean()
    if avg_price == 0:
        return None
    norm_high_slope = high_line["slope"] / avg_price
    norm_low_slope = low_line["slope"] / avg_price

    if direction == "ascending":
        flat_ok = abs(norm_high_slope) < flat_slope_threshold
        sloped_ok = norm_low_slope > flat_slope_threshold and low_line["r2"] > min_r2
        breakout_level = float(np.mean([h["price"] for h in highs]))
    else:
        flat_ok = abs(norm_low_slope) < flat_slope_threshold
        sloped_ok = norm_high_slope < -flat_slope_threshold and high_line["r2"] > min_r2
        breakout_level = float(np.mean([l["price"] for l in lows]))

    if not (flat_ok and sloped_ok):
        return None

    last_close = df["Close"].iloc[-1]
    if direction == "ascending":
        status = "confirmed" if last_close > breakout_level else "forming"
        # only the sloped (rising support) line's fit quality is meaningful here --
        # the flat resistance line has near-zero variance-explained by definition,
        # so including its r2 in the average would unfairly drag confidence down
        confidence = round(min(1.0, 0.4 + low_line["r2"] * 0.6), 2)
    else:
        status = "confirmed" if last_close < breakout_level else "forming"
        confidence = round(min(1.0, 0.4 + high_line["r2"] * 0.6), 2)

    start_date = min(highs[0]["date"], lows[0]["date"])

    return {
        "pattern": f"{direction}_triangle",
        "status": status,
        "confidence": _r(confidence),
        "key_levels": {"breakout_level": _r(breakout_level)},
        "pattern_start": str(start_date.date()),
        "pattern_end": str(df.index[-1].date()),
        "volume_confirmation": _volume_confirmation(df) if status == "confirmed" else False,
    }


# ---------------------------------------------------------------------------
# FLAGS (bull / bear) + HIGH TIGHT FLAG
# ---------------------------------------------------------------------------

def detect_flag(df, direction="bull", pole_window=10, pole_threshold_pct=0.20,
                 flag_window=10, flag_max_range_pct=0.15):
    if len(df) < pole_window + flag_window:
        return None

    flag_segment = df.tail(flag_window)
    pole_segment = df.iloc[-(pole_window + flag_window):-flag_window]
    if len(pole_segment) < 2:
        return None

    pole_start = pole_segment["Close"].iloc[0]
    pole_end = pole_segment["Close"].iloc[-1]
    pole_move_pct = (pole_end - pole_start) / pole_start

    if direction == "bull" and pole_move_pct < pole_threshold_pct:
        return None
    if direction == "bear" and pole_move_pct > -pole_threshold_pct:
        return None

    flag_high = flag_segment["High"].max()
    flag_low = flag_segment["Low"].min()
    flag_range_pct = (flag_high - flag_low) / flag_low
    if flag_range_pct > flag_max_range_pct:
        return None

    flag_slope = (flag_segment["Close"].iloc[-1] - flag_segment["Close"].iloc[0]) / flag_segment["Close"].iloc[0]
    if direction == "bull" and flag_slope > 0.02:
        return None
    if direction == "bear" and flag_slope < -0.02:
        return None

    pole_avg_vol = pole_segment["Volume"].mean()
    flag_avg_vol = flag_segment["Volume"].mean()
    vol_pattern_ok = bool(flag_avg_vol < pole_avg_vol)

    breakout_level = flag_high if direction == "bull" else flag_low
    last_close = df["Close"].iloc[-1]
    if direction == "bull":
        status = "confirmed" if last_close > breakout_level else "forming"
    else:
        status = "confirmed" if last_close < breakout_level else "forming"

    confidence = round(min(1.0, abs(pole_move_pct) + (0.2 if vol_pattern_ok else 0)), 2)

    return {
        "pattern": f"{direction}_flag",
        "status": status,
        "confidence": _r(confidence),
        "key_levels": {
            "pole_move_pct": _r(pole_move_pct * 100, 1),
            "flag_range_pct": _r(flag_range_pct * 100, 1),
            "breakout_level": _r(breakout_level),
        },
        "pattern_start": str(pole_segment.index[0].date()),
        "pattern_end": str(df.index[-1].date()),
        "volume_confirmation": vol_pattern_ok,
    }


def detect_high_tight_flag(df, pole_window_weeks=10, pole_min_pct=1.0,
                            flag_window_weeks=5, flag_max_pullback_pct=0.25):
    """Expects weekly bars -- this is the article's explicit numeric definition:
    100%+ rise in under 10 weeks, then under 25% pullback in under 5 weeks."""
    if len(df) < pole_window_weeks + flag_window_weeks:
        return None

    flag_segment = df.tail(flag_window_weeks)
    pole_segment = df.iloc[-(pole_window_weeks + flag_window_weeks):-flag_window_weeks]

    pole_low = pole_segment["Low"].min()
    pole_high = pole_segment["High"].max()
    pole_move_pct = (pole_high - pole_low) / pole_low
    if pole_move_pct < pole_min_pct:
        return None

    pullback_pct = (pole_high - flag_segment["Low"].min()) / pole_high
    if pullback_pct > flag_max_pullback_pct:
        return None

    breakout_level = pole_high
    last_close = df["Close"].iloc[-1]
    status = "confirmed" if last_close > breakout_level else "forming"
    confidence = round(min(1.0, pole_move_pct / 2), 2)

    return {
        "pattern": "high_tight_flag",
        "status": status,
        "confidence": _r(confidence),
        "key_levels": {
            "pole_move_pct": _r(pole_move_pct * 100, 1),
            "pullback_pct": _r(pullback_pct * 100, 1),
            "breakout_level": _r(breakout_level),
        },
        "pattern_start": str(pole_segment.index[0].date()),
        "pattern_end": str(df.index[-1].date()),
        "volume_confirmation": _volume_confirmation(df),
    }


# ---------------------------------------------------------------------------
# CUP AND HANDLE
# ---------------------------------------------------------------------------

def detect_cup_and_handle(df, swings, min_cup_bars=20, max_cup_bars=130,
                           cup_depth_min_pct=0.12, cup_depth_max_pct=0.50,
                           rim_tolerance_pct=0.07, handle_max_depth_pct=0.15,
                           handle_max_bars=20):
    if len(df) < min_cup_bars:
        return None

    last_trading_idx = df["trading_idx"].iloc[-1]

    left_candidates = [p for p in swings if p["type"] == "high"
                        and p["idx"] <= last_trading_idx - min_cup_bars]
    if not left_candidates:
        return None
    left_lip = max(left_candidates, key=lambda p: p["price"])

    search_end_idx = min(left_lip["idx"] + max_cup_bars, last_trading_idx)
    window_df = df[(df["trading_idx"] > left_lip["idx"]) & (df["trading_idx"] <= search_end_idx)]
    if window_df.empty:
        return None

    cup_bottom_idx_label = window_df["Low"].idxmin()
    cup_bottom_price = window_df.loc[cup_bottom_idx_label, "Low"]
    cup_bottom_trading_idx = window_df.loc[cup_bottom_idx_label, "trading_idx"]

    cup_depth_pct = (left_lip["price"] - cup_bottom_price) / left_lip["price"]
    if not (cup_depth_min_pct <= cup_depth_pct <= cup_depth_max_pct):
        return None

    right_candidates = [p for p in swings if p["type"] == "high" and p["idx"] > cup_bottom_trading_idx]
    # Right lip = the first swing high after the cup bottom that has recovered to
    # within tolerance of the left lip. Using a global max over all remaining bars
    # would incorrectly pick up a later breakout high as the "rim."
    right_lip_point = None
    for p in right_candidates:
        if p["price"] >= left_lip["price"] * (1 - rim_tolerance_pct):
            right_lip_point = p
            break
    if right_lip_point is None:
        return None
    right_lip_price = right_lip_point["price"]
    right_lip_trading_idx = right_lip_point["idx"]

    rim = max(left_lip["price"], right_lip_price)
    recovery_pct = (right_lip_price - left_lip["price"]) / left_lip["price"]
    if recovery_pct < -rim_tolerance_pct:
        return None

    cup_bars = right_lip_trading_idx - left_lip["idx"]
    if cup_bars < min_cup_bars:
        return None

    handle_df = df[df["trading_idx"] > right_lip_trading_idx]
    handle_status = None
    handle_depth_pct = None

    if not handle_df.empty:
        handle_low = handle_df["Low"].min()
        handle_depth_pct = (right_lip_price - handle_low) / right_lip_price
        handle_bars = len(handle_df)
        cup_midpoint = (left_lip["price"] + cup_bottom_price) / 2

        if (handle_depth_pct <= handle_max_depth_pct and
                handle_bars <= handle_max_bars and
                handle_low >= cup_midpoint):
            handle_status = "handle_forming"

    last_close = df["Close"].iloc[-1]
    breakout_level = rim

    if handle_status == "handle_forming" and last_close > breakout_level:
        status = "breakout_confirmed"
    elif handle_status == "handle_forming":
        status = "handle_forming"
    else:
        status = "cup_forming"

    confidence = round(min(1.0, 0.4 + (0.3 if handle_status else 0) +
                            (0.3 if 0.12 <= cup_depth_pct <= 0.33 else 0.1)), 2)

    return {
        "pattern": "cup_and_handle",
        "status": status,
        "confidence": _r(confidence),
        "key_levels": {
            "left_lip": _r(left_lip["price"]),
            "cup_bottom": _r(cup_bottom_price),
            "right_lip": _r(right_lip_price),
            "cup_depth_pct": _r(cup_depth_pct * 100, 1),
            "handle_depth_pct": _r(handle_depth_pct * 100, 1) if handle_depth_pct is not None else None,
            "breakout_level": _r(breakout_level),
        },
        "pattern_start": str(left_lip["date"].date()),
        "pattern_end": str(df.index[-1].date()),
        "volume_confirmation": _volume_confirmation(df) if status == "breakout_confirmed" else False,
    }


# ---------------------------------------------------------------------------
# RANGE CONSOLIDATION / FLAT BASE
# ---------------------------------------------------------------------------

def detect_range_consolidation(df, lookback=20, max_range_pct=0.12, vol_decline_threshold=0.9):
    if len(df) < lookback:
        return None

    segment = df.tail(lookback)
    high = segment["High"].max()
    low = segment["Low"].min()
    range_pct = (high - low) / low
    if range_pct > max_range_pct:
        return None

    half = lookback // 2
    first_half_vol = segment["Volume"].iloc[:half].mean()
    second_half_vol = segment["Volume"].iloc[half:].mean()
    vol_declining = bool(second_half_vol < first_half_vol * vol_decline_threshold)

    last_close = df["Close"].iloc[-1]
    if last_close > high:
        status = "breakout_up"
    elif last_close < low:
        status = "breakout_down"
    else:
        status = "forming"

    confidence = round(min(1.0, (max_range_pct - range_pct) / max_range_pct +
                            (0.2 if vol_declining else 0)), 2)

    return {
        "pattern": "range_consolidation",
        "status": status,
        "confidence": _r(confidence),
        "key_levels": {
            "support": _r(low),
            "resistance": _r(high),
            "range_pct": _r(range_pct * 100, 1),
        },
        "pattern_start": str(segment.index[0].date()),
        "pattern_end": str(df.index[-1].date()),
        "volume_confirmation": _volume_confirmation(df) if status in ("breakout_up", "breakout_down") else False,
    }