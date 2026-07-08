"""
Deterministic validation for the technical analyst and design agent outputs.
No LLM calls -- pure schema + rule checks derived directly from the two
system prompts (agents/technical-agent.md, agents/design-agent.md).

pip install pydantic beautifulsoup4   (pydantic ships with langchain already)
"""
import json
import re
from typing import List, Tuple

from pydantic import BaseModel, ValidationError


# ---------------------------------------------------------------------------
# Technical Agent Output Schema
# ---------------------------------------------------------------------------

class Trend(BaseModel):
    long: str = ""
    medium: str = ""
    short: str = ""


class Pattern(BaseModel):
    name: str = ""
    status: str = ""
    confidence: str = ""
    key_levels: dict = {}
    description: str = ""
    other_candidates: list = []


class Levels(BaseModel):
    resistance: list = []
    support: list = []


class Momentum(BaseModel):
    rsi_read: str = ""
    atr_read: str = ""


class ActionPoints(BaseModel):
    entry_trigger: str = ""
    hard_stop: str = ""
    first_target: str = ""


class TechnicalOutput(BaseModel):
    ticker: str = ""
    trend: Trend = Trend()
    pattern: Pattern = Pattern()
    levels: Levels = Levels()
    volume_character: str = ""
    momentum: Momentum = Momentum()
    relative_strength: str = ""
    conviction: str = ""
    bull_thesis: str = ""
    bear_risk: str = ""
    action: str = ""
    action_points: ActionPoints = ActionPoints()
    reasoning: str = ""


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


def _extract_numbers(text: str) -> List[float]:
    """Pulls out $-style decimals like 1,148.60 -> 1148.60"""
    return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*\.\d{2}", text)]


def validate_technical_output(raw_text: str) -> Tuple[bool, List[str], List[str]]:
    """
    Returns (is_valid, errors, warnings).
    errors   -> blocking, triggers a retry.
    warnings -> logged only, non-blocking (heuristic / soft checks).
    """
    errors: List[str] = []
    warnings: List[str] = []

    cleaned = _strip_fences(raw_text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return False, [f"Output is not valid JSON: {e}"], []

    try:
        out = TechnicalOutput(**data)
    except ValidationError as e:
        return False, [f"Schema error: {err['msg']} at {err['loc']}" for err in e.errors()], []

    # All three trend windows must be addressed separately.
    for window in ("long", "medium", "short"):
        if not getattr(out.trend, window).strip():
            errors.append(f"trend.{window} is empty -- all three timeframes are required")

    # pattern.name set -> status/confidence must also be populated.
    if out.pattern.name.strip():
        if not out.pattern.status.strip():
            errors.append("pattern.name is set but pattern.status is empty")
        if not out.pattern.confidence.strip():
            errors.append("pattern.name is set but pattern.confidence is empty")
        if not out.pattern.key_levels:
            warnings.append("pattern.name is set but key_levels is empty -- entry trigger may not be traceable to a real level")
    else:
        if out.pattern.other_candidates:
            errors.append("pattern.name is empty but other_candidates is non-empty -- contradictory pattern block")

    # action_points required unless the call is explicitly passive.
    action_lower = out.action.lower()
    is_passive = "avoid" in action_lower or "hold" in action_lower
    ap = out.action_points
    missing_ap = [f for f in ("entry_trigger", "hard_stop", "first_target") if not getattr(ap, f).strip()]
    if missing_ap and not is_passive:
        errors.append(f"action_points missing required field(s) for an active call: {missing_ap}")
    elif missing_ap and is_passive:
        warnings.append(f"action_points has empty field(s) {missing_ap} for a '{out.action}' call -- confirm intentional")

    # A forming (unconfirmed) pattern shouldn't carry high conviction on its own.
    if out.pattern.status.strip().lower() == "forming" and out.conviction.strip().lower() == "high":
        errors.append("pattern.status is 'forming' but conviction is 'high' -- forming patterns shouldn't alone support high conviction")

    # Thesis/risk/reasoning must be substantive, not placeholders.
    for field_name in ("bull_thesis", "bear_risk", "reasoning"):
        if len(getattr(out, field_name).strip()) < 15:
            errors.append(f"{field_name} is missing or too short to be substantive")

    # Soft check: do $ figures cited in thesis/risk text roughly match a real level?
    known_prices = set()
    for lvl in out.levels.resistance + out.levels.support:
        known_prices.update(_extract_numbers(str(lvl)))
    known_prices.update(_extract_numbers(str(out.pattern.key_levels)))

    if known_prices:
        for field_name in ("bull_thesis", "bear_risk"):
            for price in _extract_numbers(getattr(out, field_name)):
                if not any(abs(price - kp) / kp < 0.01 for kp in known_prices if kp):
                    warnings.append(
                        f"{field_name} cites {price} which doesn't match any levels/key_levels within 1% -- possible hallucinated figure"
                    )

    return (len(errors) == 0), errors, warnings


# ---------------------------------------------------------------------------
# Design Agent (HTML) Output Validation
# ---------------------------------------------------------------------------

ALLOWED_HEX = {
    "#EAF3DE", "#27500A",   # bullish
    "#FCEBEB", "#791F1F",   # bearish
    "#E6F1FB", "#0C447C",   # neutral
    "#FAEEDA", "#633806",   # warning
    "#F4F5F7", "#FFFFFF", "#F9FAFB",
    "#111827", "#4B5563", "#9CA3AF", "#E5E7EB",
    "#DC2626", "#639922", "#3B6D11",  # conviction bar
}


def validate_html_output(html: str) -> Tuple[bool, List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []

    if "var(--" in html:
        errors.append("Found CSS variable usage (var(--...)) -- unresolved in email clients")

    if re.search(r"display\s*:\s*flex", html, re.IGNORECASE):
        errors.append("Found display:flex -- unsupported in Outlook")

    if re.search(r"display\s*:\s*grid", html, re.IGNORECASE):
        errors.append("Found display:grid -- unsupported in Outlook")

    if '<i class="ti' in html:
        errors.append('Found icon font usage (<i class="ti...) -- renders as blank boxes')

    style_blocks = re.findall(r"<style", html, re.IGNORECASE)
    if len(style_blocks) > 1:
        errors.append(f"Found {len(style_blocks)} <style> blocks -- only one top-of-file mobile media query block is allowed")

    for banned_tag in ("<html", "<head", "<body", "<!DOCTYPE"):
        if banned_tag.lower() in html.lower():
            errors.append(f"Found forbidden top-level tag: {banned_tag}")

    if re.search(r"box-shadow|linear-gradient|radial-gradient|transform\s*:", html, re.IGNORECASE):
        warnings.append("Found box-shadow/gradient/transform -- silently dropped by email clients, check design intent")

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for div in soup.find_all("div"):
            style = div.get("style", "")
            if re.search(r"float\s*:|position\s*:\s*(absolute|relative|fixed)", style, re.IGNORECASE):
                errors.append(f"Found <div> using float/position for layout: {style[:80]}")
    except ImportError:
        warnings.append("beautifulsoup4 not installed -- skipped <div>/float/position structural check (pip install beautifulsoup4)")

    found_hex = {h.upper() for h in re.findall(r"#[0-9A-Fa-f]{6}", html)}
    off_palette = found_hex - ALLOWED_HEX
    if off_palette:
        warnings.append(f"Found hex colors outside the defined palette: {sorted(off_palette)}")

    for width_match in re.findall(r'width\s*[:=]\s*"?(\d+)(?:px)?"?', html):
        if int(width_match) > 660:
            errors.append(f"Found element with width={width_match}px, exceeding the 640px email-safe max")
            break

    return (len(errors) == 0), errors, warnings