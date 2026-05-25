"""Event-risk calendar — hardcodes known macro events to flatten positions before volatility.

Major events (FOMC, CPI, NFP) are predictable weeks/months ahead.
This module provides known dates + a check function the main loop calls.
Events are configurable via goal.yaml so users can add/remove without code changes.
"""

import datetime
from datetime import timezone, timedelta
from typing import List, Dict, Optional
from pathlib import Path

# ── Known 2026 FOMC meeting dates (8 per year, scheduled annually) ──
FOMC_2026 = [
    datetime.date(2026, 1, 28),
    datetime.date(2026, 3, 17),
    datetime.date(2026, 5, 6),
    datetime.date(2026, 6, 16),
    datetime.date(2026, 7, 28),
    datetime.date(2026, 9, 15),
    datetime.date(2026, 11, 4),
    datetime.date(2026, 12, 15),
]

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """Return the n-th occurrence of weekday (0=Mon) in a given month."""
    first_day = datetime.date(year, month, 1)
    # Day of week for 1st (0=Mon)
    first_dow = first_day.weekday()
    # Days until first occurrence of target weekday
    delta = (weekday - first_dow) % 7
    target_day = 1 + delta + 7 * (n - 1)
    if target_day > 31:
        # Fallback: last occurrence
        target_day -= 7
    return datetime.date(year, month, target_day)


def generate_cpi_dates(year: int) -> List[datetime.date]:
    """CPI releases: monthly, usually 2nd or 3rd week.
    BLS typically releases CPI around 10th-16th of each month.
    We model as: 2nd Wednesday of each month + 2 days (Friday release).
    Close enough for kill-switch purposes (exact dates known ~2 weeks ahead).
    """
    dates = []
    for month in range(1, 13):
        # Approximate: 2nd Wednesday of month (typical CPI release window)
        cpi_date = _nth_weekday(year, month, 2, 2)  # 2nd Wednesday
        dates.append(cpi_date)
    return dates


def generate_nfp_dates(year: int) -> List[datetime.date]:
    """NFP (Non-Farm Payrolls): first Friday of each month at 8:30 AM ET."""
    dates = []
    for month in range(1, 13):
        nfp_date = _nth_weekday(year, month, 4, 1)  # 1st Friday
        dates.append(nfp_date)
    return dates


def load_event_calendar(config_path: Optional[Path] = None) -> List[Dict]:
    """Load event calendar from config, falling back to known 2026 dates.
    
    Config format (in goal.yaml):
        event_calendar:
          enabled: true
          flatten_minutes_before: 120
          hold_minutes_after: 60
          custom_events:
            - date: "2026-06-10"
              name: "CPI"
            - date: "2026-06-05"
              name: "NFP"
    
    Returns list of events sorted by date.
    """
    events = []
    now = datetime.datetime.now(timezone.utc).date()
    year = now.year
    
    # Auto-generate known event types
    for name, date_list in [
        ("FOMC", FOMC_2026),
        ("CPI", generate_cpi_dates(year)),
        ("NFP", generate_nfp_dates(year)),
    ]:
        for d in date_list:
            if d >= now - timedelta(days=7):  # Only keep relevant window
                events.append({"date": d.isoformat(), "name": name})
    
    # Load custom events from config if available
    if config_path and config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            ec = config.get("event_calendar", {})
            custom = ec.get("custom_events", [])
            for ev in custom:
                ev_name = ev.get("name", ev.get("date", "unknown"))
                ev_date = ev.get("date")
                if ev_date:
                    events.append({"date": ev_date, "name": ev_name})
        except Exception:
            pass  # Non-fatal — fall back to auto-generated dates
    
    # Deduplicate by date+name
    seen = set()
    unique = []
    for ev in sorted(events, key=lambda e: e["date"]):
        key = (ev["date"], ev["name"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    
    return unique


def is_near_macro_event(
    events: List[Dict],
    flatten_minutes_before: int = 120,
    hold_minutes_after: int = 60,
) -> Dict:
    """Check if current time is within the danger window of a macro event.
    
    Returns:
        {
            "blocked": bool,
            "reason": str or None,
            "nearest_event": str or None,
            "minutes_until_event": float or None,
        }
    """
    now = datetime.datetime.now(timezone.utc)
    
    for ev in events:
        try:
            event_dt = datetime.datetime.fromisoformat(ev["date"])
            # Events typically happen at 14:00 UTC for FOMC, 08:30 ET for CPI/NFP
            # Default to a conservative 14:00 UTC for safety
            event_dt = event_dt.replace(hour=14, minute=0, tzinfo=timezone.utc)
            
            minutes_until = (event_dt - now).total_seconds() / 60
            minutes_since = (now - event_dt).total_seconds() / 60
            
            # Before event: flatten if within window
            if 0 <= minutes_until <= flatten_minutes_before:
                return {
                    "blocked": True,
                    "reason": f"FLATTEN: {ev['name']} in {minutes_until:.0f}m — entries blocked until {minutes_until:.0f}m before",
                    "nearest_event": ev["name"],
                    "minutes_until_event": minutes_until,
                }
            
            # After event: hold entries until clear
            if 0 <= minutes_since <= hold_minutes_after:
                return {
                    "blocked": True,
                    "reason": f"HOLD: {ev['name']} was {minutes_since:.0f}m ago — entries resume after {hold_minutes_after}m",
                    "nearest_event": ev["name"],
                    "minutes_until_event": -minutes_since,
                }
        except (ValueError, TypeError):
            continue
    
    return {
        "blocked": False,
        "reason": None,
        "nearest_event": None,
        "minutes_until_event": None,
    }


def pretty_print_event_calendar(events: List[Dict]) -> str:
    """Format upcoming events for dashboard/log."""
    now = datetime.datetime.now(timezone.utc)
    lines = ["📅 Upcoming Macro Events:"]
    upcoming = []
    
    for ev in events:
        try:
            event_dt = datetime.datetime.fromisoformat(ev["date"]).replace(
                hour=14, minute=0, tzinfo=timezone.utc
            )
            days_until = (event_dt - now).days
            if -1 <= days_until <= 14:  # Show from 1 day ago to 14 days ahead
                upcoming.append((days_until, ev["name"], ev["date"]))
        except (ValueError, TypeError):
            continue
    
    for days, name, date_str in sorted(upcoming, key=lambda x: x[0]):
        if days < 0:
            lines.append(f"   🔴 {name} {date_str} (yesterday — hold window active)")
        elif days == 0:
            lines.append(f"   🔴 {name} {date_str} (TODAY)")
        elif days == 1:
            lines.append(f"   🟡 {name} {date_str} (tomorrow)")
        else:
            lines.append(f"   🟢 {name} {date_str} ({days} days away)")
    
    if not upcoming:
        lines.append("   (none in next 14 days)")
    
    return "\n".join(lines)
