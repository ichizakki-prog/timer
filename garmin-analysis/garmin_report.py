#!/usr/bin/env python3
"""Garmin Connect daily analysis report generator.

Logs into Garmin Connect (via the `garminconnect` / `garth` libraries, the
same stack used by https://github.com/Taxuspt/garmin_mcp), pulls recent
sleep / heart-rate / body-battery / stress / steps / weight / activity data,
and renders a single self-contained HTML report.

Usage:
    python3 garmin_report.py [--days 14] [--activity-days 7] [--out reports/latest.html]

Environment variables:
    GARMIN_EMAIL       Garmin Connect login email.
                        Required unless a cached/token-store login already exists.
    GARMIN_PASSWORD    Garmin Connect login password. Same rule as above.
    GARMIN_TOKENSTORE  Where to cache the login session so later runs don't
                        need to re-authenticate (default: ~/.garmin_tokens).
                        Can also be a path to a *file* holding a long token
                        string, or the raw token JSON itself (see garminconnect
                        docs) -- useful for headless/automated runs.
    GARMIN_MFA_CODE    A one-time 6-digit code, if the account has 2-step
                        verification enabled and no cached token exists yet.
                        Automated runs cannot solve MFA challenges on their
                        own -- see README.md.

See README.md in this folder for full setup and automation instructions.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import statistics
import sys
from pathlib import Path
from typing import Any

try:
    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )
except ImportError:
    print(
        "garminconnect is not installed. Run: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise


def load_dotenv() -> None:
    """Load GARMIN_* values from a .env file next to this script, if present.

    Keeps local/PC usage simple: put GARMIN_EMAIL=... and GARMIN_PASSWORD=...
    in garmin-analysis/.env and this script picks them up automatically.
    Values already set in the real environment always take priority.
    No external dependency (python-dotenv) required.
    """
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv()


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------

def login() -> Garmin:
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    tokenstore = os.getenv("GARMIN_TOKENSTORE") or str(
        Path.home() / ".garmin_tokens"
    )
    mfa_code_env = os.getenv("GARMIN_MFA_CODE")

    def prompt_mfa() -> str:
        if mfa_code_env:
            return mfa_code_env
        try:
            return input("Garmin Connect MFA code: ").strip()
        except EOFError:
            raise GarminConnectAuthenticationError(
                "MFA code required but no GARMIN_MFA_CODE was set and no "
                "interactive terminal is available. Run this script "
                "interactively once to establish a cached session (see "
                "README.md), then automated runs will reuse it."
            )

    garmin = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)

    try:
        garmin.login(tokenstore)
    except FileNotFoundError:
        # No cached token yet -- fall back to a fresh password login.
        if not email or not password:
            raise SystemExit(
                "No cached Garmin session found and GARMIN_EMAIL / "
                "GARMIN_PASSWORD are not set. See README.md."
            )
        garmin.login(tokenstore)
    except GarminConnectTooManyRequestsError as e:
        raise SystemExit(f"Garmin Connect rate-limited this login: {e}")
    except GarminConnectAuthenticationError as e:
        raise SystemExit(f"Garmin Connect authentication failed: {e}")
    except GarminConnectConnectionError as e:
        raise SystemExit(f"Could not reach Garmin Connect: {e}")

    return garmin


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------

def safe_call(fn, *args, default=None):
    try:
        return fn(*args)
    except Exception:
        return default


def collect_day(garmin: Garmin, date: dt.date) -> dict[str, Any]:
    iso = date.isoformat()
    day: dict[str, Any] = {"date": iso}

    stats = safe_call(garmin.get_stats, iso, default={}) or {}
    day["steps"] = stats.get("totalSteps")
    day["resting_hr"] = stats.get("restingHeartRate")
    day["calories"] = stats.get("totalKilocalories")
    day["floors"] = stats.get("floorsAscended")

    sleep = safe_call(garmin.get_sleep_data, iso, default={}) or {}
    dto = sleep.get("dailySleepDTO") or {}
    sleep_seconds = dto.get("sleepTimeSeconds")
    day["sleep_hours"] = round(sleep_seconds / 3600, 2) if sleep_seconds else None
    scores = dto.get("sleepScores") or {}
    overall = scores.get("overall") or {}
    day["sleep_score"] = overall.get("value")
    day["deep_sleep_h"] = (
        round(dto.get("deepSleepSeconds", 0) / 3600, 2)
        if dto.get("deepSleepSeconds")
        else None
    )
    day["rem_sleep_h"] = (
        round(dto.get("remSleepSeconds", 0) / 3600, 2)
        if dto.get("remSleepSeconds")
        else None
    )

    stress = safe_call(garmin.get_stress_data, iso, default={}) or {}
    day["stress_avg"] = stress.get("avgStressLevel")
    if day["stress_avg"] is not None and day["stress_avg"] < 0:
        day["stress_avg"] = None  # Garmin uses -1/-2 for "no data"

    bb = safe_call(garmin.get_body_battery, iso, iso, default=[]) or []
    if bb and isinstance(bb, list):
        entry = bb[0]
        charged = entry.get("charged")
        drained = entry.get("drained")
        values = entry.get("bodyBatteryValuesArray") or []
        last_val = None
        for v in reversed(values):
            # each item is like [timestamp, status, value, version]
            if len(v) > 2 and isinstance(v[2], (int, float)):
                last_val = v[2]
                break
        day["body_battery_charged"] = charged
        day["body_battery_drained"] = drained
        day["body_battery_end"] = last_val

    weigh = safe_call(garmin.get_daily_weigh_ins, iso, default={}) or {}
    entries = weigh.get("dateWeightList") or []
    if entries:
        grams = entries[-1].get("weight")
        day["weight_kg"] = round(grams / 1000, 1) if grams else None

    return day


def collect_activities(garmin: Garmin, since: dt.date, limit: int = 40) -> list[dict]:
    raw = safe_call(garmin.get_activities, 0, limit, default=[]) or []
    out = []
    for a in raw:
        start = a.get("startTimeLocal", "")
        try:
            start_date = dt.date.fromisoformat(start[:10])
        except ValueError:
            continue
        if start_date < since:
            continue
        distance_m = a.get("distance") or 0
        duration_s = a.get("duration") or 0
        pace_min_per_km = None
        if distance_m and duration_s:
            pace_min_per_km = (duration_s / 60) / (distance_m / 1000)
        out.append(
            {
                "date": start,
                "type": (a.get("activityType") or {}).get("typeKey", "activity"),
                "name": a.get("activityName", ""),
                "distance_km": round(distance_m / 1000, 2) if distance_m else None,
                "duration_min": round(duration_s / 60, 1) if duration_s else None,
                "avg_hr": a.get("averageHR"),
                "max_hr": a.get("maxHR"),
                "calories": a.get("calories"),
                "pace_min_per_km": round(pace_min_per_km, 2)
                if pace_min_per_km
                else None,
            }
        )
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


# --------------------------------------------------------------------------
# Analysis helpers
# --------------------------------------------------------------------------

def trend(series: list[float | None]) -> dict[str, Any]:
    """Compare the last value against the mean of the rest."""
    values = [v for v in series if v is not None]
    if not values:
        return {"latest": None, "avg": None, "delta": None}
    latest = series[-1]
    history = [v for v in series[:-1] if v is not None]
    avg = round(statistics.fmean(history), 2) if history else None
    delta = round(latest - avg, 2) if (latest is not None and avg is not None) else None
    return {"latest": latest, "avg": avg, "delta": delta}


# --------------------------------------------------------------------------
# HTML / SVG rendering
# --------------------------------------------------------------------------

PALETTE = {
    "blue": ("#2a78d6", "#3987e5"),
    "orange": ("#eb6834", "#d95926"),
    "aqua": ("#1baf7a", "#199e70"),
    "magenta": ("#e87ba4", "#d55181"),
    "good": ("#0ca30c", "#0ca30c"),
    "critical": ("#d03b3b", "#e66767"),
}


def spark_path(values: list[float], width: int, height: int, pad: int = 4) -> tuple[str, list[tuple[float, float]]]:
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return "", []
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    n = len(values)
    coords = []
    step = (width - 2 * pad) / (n - 1)
    for i, v in enumerate(values):
        x = pad + i * step
        if v is None:
            coords.append(None)
            continue
        y = height - pad - ((v - lo) / span) * (height - 2 * pad)
        coords.append((round(x, 1), round(y, 1)))
    segments = []
    current = []
    for c in coords:
        if c is None:
            if len(current) > 1:
                segments.append(current)
            current = []
        else:
            current.append(c)
    if len(current) > 1:
        segments.append(current)
    path = " ".join(
        "M " + " L ".join(f"{x},{y}" for x, y in seg) for seg in segments
    )
    return path, coords


def line_chart(chart_id: str, title: str, dates: list[str], values: list[float | None], color_key: str, unit: str) -> str:
    light, dark = PALETTE[color_key]
    width, height = 560, 160
    pad_l, pad_r, pad_t, pad_b = 8, 8, 12, 24
    pts = [v for v in values if v is not None]
    if not pts:
        return f"""
        <div class="chart-card">
          <h3>{title}</h3>
          <p class="no-data">データなし</p>
        </div>"""
    lo, hi = min(pts), max(pts)
    if lo == hi:
        lo -= 1
        hi += 1
    span = hi - lo
    n = len(values)
    step = (width - pad_l - pad_r) / max(n - 1, 1)

    coords: list[tuple[float, float] | None] = []
    for i, v in enumerate(values):
        if v is None:
            coords.append(None)
            continue
        x = pad_l + i * step
        y = pad_t + (height - pad_t - pad_b) * (1 - (v - lo) / span)
        coords.append((round(x, 1), round(y, 1)))

    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for c in coords:
        if c is None:
            if len(current) > 1:
                segments.append(current)
            current = []
        else:
            current.append(c)
    if len(current) > 1:
        segments.append(current)

    line_path = " ".join(
        "M " + " L ".join(f"{x},{y}" for x, y in seg) for seg in segments
    )
    area_parts = []
    baseline = height - pad_b
    for seg in segments:
        if len(seg) < 2:
            continue
        d = f"M {seg[0][0]},{baseline} L " + " L ".join(f"{x},{y}" for x, y in seg)
        d += f" L {seg[-1][0]},{baseline} Z"
        area_parts.append(d)
    area_path = " ".join(area_parts)

    dots = []
    for i, c in enumerate(coords):
        if c is None:
            continue
        x, y = c
        dots.append(
            f'<circle class="dot" cx="{x}" cy="{y}" r="6" '
            f'data-date="{dates[i]}" data-value="{values[i]}" data-unit="{unit}" />'
        )

    latest = values[-1]
    latest_str = f"{latest}{unit}" if latest is not None else "--"

    return f"""
        <div class="chart-card">
          <div class="chart-head">
            <h3>{title}</h3>
            <span class="chart-latest">{latest_str}</span>
          </div>
          <svg class="chart-svg" data-color="{color_key}" viewBox="0 0 {width} {height}" role="img" aria-label="{title}">
            <line class="baseline" x1="{pad_l}" y1="{baseline}" x2="{width - pad_r}" y2="{baseline}" />
            <path class="area" d="{area_path}" />
            <path class="line" d="{line_path}" />
            {''.join(dots)}
          </svg>
          <div class="chart-tooltip" hidden></div>
        </div>"""


def stat_tile(label: str, value, unit: str, delta, direction: str) -> str:
    if value is None:
        return f"""
        <div class="tile">
          <span class="tile-label">{label}</span>
          <span class="tile-value tile-nodata">データなし</span>
        </div>"""
    badge = ""
    if delta is not None and abs(delta) > 0:
        is_up = delta > 0
        good = (is_up and direction == "up") or (not is_up and direction == "down")
        cls = "good" if good else ("bad" if direction != "neutral" else "neutral")
        arrow = "▲" if is_up else "▼"
        badge = f'<span class="tile-delta {cls}">{arrow} {abs(delta):g}{unit}</span>'
    return f"""
        <div class="tile">
          <span class="tile-label">{label}</span>
          <span class="tile-value">{value:g}<small>{unit}</small></span>
          {badge}
        </div>"""


def activities_table(activities: list[dict]) -> str:
    if not activities:
        return '<p class="no-data">この期間の記録されたアクティビティはありません。</p>'
    rows = []
    for a in activities:
        pace = f"{a['pace_min_per_km']:.2f}/km" if a["pace_min_per_km"] else "--"
        rows.append(
            "<tr>"
            f"<td>{a['date'][:10]}</td>"
            f"<td>{a['type']}</td>"
            f"<td>{a['distance_km'] if a['distance_km'] is not None else '--'} km</td>"
            f"<td>{a['duration_min'] if a['duration_min'] is not None else '--'} 分</td>"
            f"<td>{pace}</td>"
            f"<td>{a['avg_hr'] or '--'}</td>"
            f"<td>{a['calories'] or '--'}</td>"
            "</tr>"
        )
    return f"""
        <div class="table-wrap">
        <table>
          <thead><tr><th>日付</th><th>種目</th><th>距離</th><th>時間</th><th>ペース</th><th>平均心拍</th><th>消費cal</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
        </div>"""


def render_report(days_data: list[dict], activities: list[dict], display_name: str) -> str:
    dates = [d["date"] for d in days_data]
    yesterday = days_data[-1]

    sleep_scores = [d.get("sleep_score") for d in days_data]
    resting_hrs = [d.get("resting_hr") for d in days_data]
    steps = [d.get("steps") for d in days_data]
    body_battery = [d.get("body_battery_charged") for d in days_data]
    stress = [d.get("stress_avg") for d in days_data]
    weight = [d.get("weight_kg") for d in days_data]

    t_sleep = trend(sleep_scores)
    t_hr = trend(resting_hrs)
    t_steps = trend(steps)
    t_bb = trend(body_battery)
    t_stress = trend(stress)
    t_weight = trend(weight)

    tiles = "".join(
        [
            stat_tile("睡眠スコア", yesterday.get("sleep_score"), "", t_sleep["delta"], "up"),
            stat_tile("安静時心拍", yesterday.get("resting_hr"), "bpm", t_hr["delta"], "down"),
            stat_tile("歩数", yesterday.get("steps"), "歩", t_steps["delta"], "up"),
            stat_tile(
                "ボディバッテリー(充電)",
                yesterday.get("body_battery_charged"),
                "",
                t_bb["delta"],
                "up",
            ),
            stat_tile("平均ストレス", yesterday.get("stress_avg"), "", t_stress["delta"], "down"),
            stat_tile("体重", yesterday.get("weight_kg"), "kg", t_weight["delta"], "neutral"),
        ]
    )

    charts = "".join(
        [
            line_chart("chart-sleep", "睡眠スコアの推移", dates, sleep_scores, "blue", ""),
            line_chart("chart-hr", "安静時心拍の推移", dates, resting_hrs, "critical", "bpm"),
            line_chart("chart-steps", "歩数の推移", dates, steps, "aqua", ""),
            line_chart("chart-bb", "ボディバッテリー(充電量)の推移", dates, body_battery, "orange", ""),
        ]
    )

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    period = f"{dates[0]} 〜 {dates[-1]}"

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Garmin デイリーレポート</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@500;700;800&family=Source+Sans+3:wght@400;600&family=IBM+Plex+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
:root {{
  color-scheme: light;
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --surface-2: #f2f1ec;
  --text: #0b0b0b;
  --text-2: #52514e;
  --muted: #898781;
  --grid: #e1e0d9;
  --border: rgba(11,11,11,0.10);
  --accent: #2a78d6;
  --accent-soft: #cde2fb;
  --good: #0ca30c;
  --bad: #d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --page: #0d0d0d;
    --surface: #1a1a19;
    --surface-2: #221f1a;
    --text: #ffffff;
    --text-2: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --border: rgba(255,255,255,0.10);
    --accent: #3987e5;
    --accent-soft: #184f95;
    --good: #0ca30c;
    --bad: #e66767;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --page: #0d0d0d;
  --surface: #1a1a19;
  --surface-2: #221f1a;
  --text: #ffffff;
  --text-2: #c3c2b7;
  --muted: #898781;
  --grid: #2c2c2a;
  --border: rgba(255,255,255,0.10);
  --accent: #3987e5;
  --accent-soft: #184f95;
  --good: #0ca30c;
  --bad: #e66767;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--page);
  color: var(--text);
  font-family: 'Source Sans 3', system-ui, -apple-system, sans-serif;
  padding: 32px 20px 64px;
}}
.wrap {{ max-width: 960px; margin: 0 auto; display: flex; flex-direction: column; gap: 28px; }}
header {{ display: flex; flex-direction: column; gap: 4px; }}
h1 {{
  font-family: 'Manrope', sans-serif;
  font-weight: 800;
  font-size: 1.7rem;
  letter-spacing: -0.01em;
  margin: 0;
}}
.subtitle {{ color: var(--text-2); font-size: 0.9rem; }}
.meta {{ color: var(--muted); font-size: 0.78rem; font-family: 'IBM Plex Mono', monospace; }}

.tiles {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 2px;
  background: var(--border);
  border-radius: 16px;
  overflow: hidden;
  border: 1px solid var(--border);
}}
.tile {{
  background: var(--surface);
  padding: 18px 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}}
.tile-label {{ font-size: 0.72rem; color: var(--text-2); text-transform: uppercase; letter-spacing: 0.06em; }}
.tile-value {{ font-family: 'IBM Plex Mono', monospace; font-weight: 600; font-size: 1.6rem; font-variant-numeric: tabular-nums; }}
.tile-value small {{ font-size: 0.9rem; color: var(--text-2); margin-left: 2px; }}
.tile-nodata {{ font-size: 0.95rem; color: var(--muted); font-family: 'Source Sans 3', sans-serif; }}
.tile-delta {{ font-size: 0.78rem; font-family: 'IBM Plex Mono', monospace; width: fit-content; padding: 2px 6px; border-radius: 6px; }}
.tile-delta.good {{ color: var(--good); background: color-mix(in srgb, var(--good) 14%, transparent); }}
.tile-delta.bad {{ color: var(--bad); background: color-mix(in srgb, var(--bad) 14%, transparent); }}
.tile-delta.neutral {{ color: var(--text-2); background: var(--surface-2); }}

.charts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 16px; }}
.chart-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 16px 18px 12px;
  position: relative;
}}
.chart-head {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; }}
.chart-card h3 {{ font-family: 'Manrope', sans-serif; font-size: 0.92rem; font-weight: 700; margin: 0; }}
.chart-latest {{ font-family: 'IBM Plex Mono', monospace; font-size: 0.85rem; color: var(--text-2); font-variant-numeric: tabular-nums; }}
.chart-svg {{ width: 100%; height: auto; overflow: visible; }}
.chart-svg .baseline {{ stroke: var(--grid); stroke-width: 1; }}
.chart-svg .line {{ fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }}
.chart-svg[data-color="blue"] .line, .chart-svg[data-color="blue"] .dot {{ stroke: var(--accent); }}
.chart-svg[data-color="blue"] .area {{ fill: var(--accent); opacity: 0.12; }}
.chart-svg[data-color="critical"] .line, .chart-svg[data-color="critical"] .dot {{ stroke: var(--bad); }}
.chart-svg[data-color="critical"] .area {{ fill: var(--bad); opacity: 0.12; }}
.chart-svg[data-color="aqua"] .line, .chart-svg[data-color="aqua"] .dot {{ stroke: #1baf7a; }}
.chart-svg[data-color="aqua"] .area {{ fill: #1baf7a; opacity: 0.12; }}
.chart-svg[data-color="orange"] .line, .chart-svg[data-color="orange"] .dot {{ stroke: #eb6834; }}
.chart-svg[data-color="orange"] .area {{ fill: #eb6834; opacity: 0.12; }}
.chart-svg .dot {{ fill: transparent; stroke: none; cursor: crosshair; }}
.chart-tooltip {{
  position: absolute;
  background: var(--text);
  color: var(--page);
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.72rem;
  padding: 4px 8px;
  border-radius: 6px;
  pointer-events: none;
  transform: translate(-50%, -130%);
  white-space: nowrap;
}}
.no-data {{ color: var(--muted); font-size: 0.85rem; }}

section h2 {{ font-family: 'Manrope', sans-serif; font-size: 1.05rem; font-weight: 700; margin: 0 0 12px; }}
.table-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 16px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; background: var(--surface); }}
th, td {{ text-align: left; padding: 10px 14px; white-space: nowrap; }}
th {{ color: var(--text-2); font-weight: 600; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; border-bottom: 1px solid var(--border); }}
td {{ border-bottom: 1px solid var(--grid); font-variant-numeric: tabular-nums; }}
tbody tr:last-child td {{ border-bottom: none; }}

footer {{ color: var(--muted); font-size: 0.75rem; text-align: center; padding-top: 8px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Garmin デイリーレポート</h1>
    <span class="subtitle">{display_name} さんの健康・トレーニングデータ ({period})</span>
    <span class="meta">生成日時: {now}</span>
  </header>

  <section>
    <h2>昨日のサマリー</h2>
    <div class="tiles">{tiles}</div>
  </section>

  <section>
    <h2>直近{len(dates)}日間の推移</h2>
    <div class="charts">{charts}</div>
  </section>

  <section>
    <h2>アクティビティ</h2>
    {activities_table(activities)}
  </section>

  <footer>Garmin Connect のデータをもとに自動生成されたレポートです。診断や医療的助言ではありません。</footer>
</div>
<script>
(function() {{
  document.querySelectorAll('.chart-card').forEach(function(card) {{
    var svg = card.querySelector('svg');
    var tip = card.querySelector('.chart-tooltip');
    if (!svg || !tip) return;
    var dots = svg.querySelectorAll('.dot');
    dots.forEach(function(dot) {{
      dot.addEventListener('mouseenter', show);
      dot.addEventListener('mousemove', show);
      dot.addEventListener('mouseleave', function() {{ tip.hidden = true; }});
      function show(e) {{
        var rect = svg.getBoundingClientRect();
        var cardRect = card.getBoundingClientRect();
        var cx = parseFloat(dot.getAttribute('cx'));
        var cy = parseFloat(dot.getAttribute('cy'));
        var scaleX = rect.width / svg.viewBox.baseVal.width;
        var scaleY = rect.height / svg.viewBox.baseVal.height;
        var x = rect.left - cardRect.left + cx * scaleX;
        var y = rect.top - cardRect.top + cy * scaleY;
        tip.style.left = x + 'px';
        tip.style.top = y + 'px';
        tip.textContent = dot.getAttribute('data-date') + ': ' + dot.getAttribute('data-value') + dot.getAttribute('data-unit');
        tip.hidden = false;
      }}
    }});
  }});
}})();
</script>
</body>
</html>"""


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=14, help="Trend window in days (default: 14)")
    parser.add_argument("--activity-days", type=int, default=7, help="How many days of activities to list (default: 7)")
    parser.add_argument("--out", default=None, help="Output HTML path (default: reports/<date>.html and reports/latest.html)")
    args = parser.parse_args()

    garmin = login()

    today = dt.date.today()
    # Garmin data for "today" is usually incomplete; report through yesterday.
    end_date = today - dt.timedelta(days=1)
    dates = [end_date - dt.timedelta(days=i) for i in range(args.days - 1, -1, -1)]

    print(f"Fetching {len(dates)} days of data from Garmin Connect...", file=sys.stderr)
    days_data = [collect_day(garmin, d) for d in dates]

    activity_since = end_date - dt.timedelta(days=args.activity_days - 1)
    activities = collect_activities(garmin, activity_since)

    display_name = getattr(garmin, "display_name", None) or getattr(garmin, "full_name", None) or "ユーザー"

    html = render_report(days_data, activities, display_name)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    dated_path = out_dir / f"{end_date.isoformat()}.html"
    latest_path = Path(args.out) if args.out else out_dir / "latest.html"

    dated_path.write_text(html, encoding="utf-8")
    latest_path.write_text(html, encoding="utf-8")

    print(f"Report written to {dated_path} and {latest_path}", file=sys.stderr)

    # Quick text summary to stdout, handy for automation / chat relay.
    y = days_data[-1]
    print(f"[{y['date']}] 睡眠スコア: {y.get('sleep_score', '--')}  "
          f"安静時心拍: {y.get('resting_hr', '--')}bpm  "
          f"歩数: {y.get('steps', '--')}  "
          f"ボディバッテリー: {y.get('body_battery_charged', '--')}  "
          f"ストレス: {y.get('stress_avg', '--')}  "
          f"体重: {y.get('weight_kg', '--')}kg")
    print(f"アクティビティ({args.activity_days}日間): {len(activities)}件")


if __name__ == "__main__":
    main()
