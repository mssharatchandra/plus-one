"""Deterministic reminder time parsing: no guessed dates or timezone.

Supported messages::
    remind me to send the report tomorrow at 9am
    remind me to call Sam on 2026-11-02 at 14:00
    remind me to stretch in 5 minutes

``today at`` and ISO dates without ``on`` are also accepted. Relative durations
are elapsed time, not wall-clock arithmetic. Local times in a DST gap or fold
must be clarified by choosing an unambiguous local time. The caller must display
and confirm the returned local_display before saving a reminder.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt_timezone.utc
_MAX_SECONDS = 30 * 24 * 60 * 60
_DURATION = re.compile(r"(?P<n>[0-9]{1,8})\s*(?P<u>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)", re.IGNORECASE)
_PREFIX = re.compile(r"^remind\s+me\s+to\s+", re.IGNORECASE)


def _zone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name or len(name) > 100:
        raise ValueError("Please set a valid IANA timezone, such as Asia/Kolkata or America/New_York.")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("Please set a valid IANA timezone, such as Asia/Kolkata or America/New_York.") from None


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("The reference clock must be a timezone-aware datetime.")
    return value


def _candidates(wall: datetime, zone: ZoneInfo) -> list[datetime]:
    """Distinct UTC instants whose local representation is exactly wall."""
    result = set()
    for fold in (0, 1):
        local = wall.replace(tzinfo=zone, fold=fold)
        instant = local.astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == wall:
            result.add(instant)
    return sorted(result)


def _strict_local(wall: datetime, zone: ZoneInfo) -> datetime:
    options = _candidates(wall, zone)
    if not options:
        raise ValueError("That local time does not exist because the clock changes. Please choose another time.")
    if len(options) > 1:
        raise ValueError("That local time occurs twice because the clock changes. Please choose an unambiguous time.")
    return options[0]


def parse_snooze_duration(text: str) -> int:
    """Return elapsed seconds for one explicit positive unit, up to 30 days."""
    if not isinstance(text, str) or len(text) > 80:
        raise ValueError("Use a duration such as 10m or 2 hours, up to 30 days.")
    match = _DURATION.fullmatch(text.strip())
    if not match:
        raise ValueError("Use a duration such as 10m or 2 hours, up to 30 days.")
    unit = match['u'].lower()[0]
    seconds = int(match['n']) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit]
    if not 1 <= seconds <= _MAX_SECONDS:
        raise ValueError("Choose a positive duration of no more than 30 days.")
    return seconds


def _parse_clock(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"([0-9]{1,2})(?::([0-9]{2}))?\s*(am|pm)?", text.strip(), re.IGNORECASE)
    if not match:
        raise ValueError("Please use a time such as 9am or 14:30.")
    hour, minute = int(match[1]), int(match[2] or '0')
    suffix = (match[3] or '').lower()
    if minute > 59 or (suffix and not 1 <= hour <= 12) or (not suffix and not 0 <= hour <= 23):
        raise ValueError("Please use a valid time such as 9am or 14:30.")
    if not suffix and match[2] is None:
        raise ValueError("Does that mean AM or PM? Please use 9am or 21:00.")
    if suffix:
        hour = hour % 12 + (12 if suffix == 'pm' else 0)
    return hour, minute


def parse_reminder(text: str, timezone: str, now: datetime | None = None) -> dict:
    """Parse only explicit supported grammar; return an unsaved confirmation proposal."""
    zone = _zone(timezone)
    now = _aware(now if now is not None else datetime.now(UTC)).astimezone(UTC)
    if not isinstance(text, str) or len(text) > 1000 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError("Please send a single-line reminder of at most 1000 characters.")
    body = _PREFIX.sub('', text.strip(), count=1)
    if body == text.strip():
        raise ValueError("Try: remind me to send the report tomorrow at 9am.")
    relative = re.fullmatch(r"(.+?)\s+in\s+([0-9]+\s*[a-zA-Z]+)", body, re.IGNORECASE)
    if relative:
        title = relative[1].strip()
        due = now + timedelta(seconds=parse_snooze_duration(relative[2]))
    else:
        absolute = re.fullmatch(r"(.+?)\s+(?:on\s+)?(today|tomorrow|[0-9]{4}-[0-9]{2}-[0-9]{2})\s+at\s+(.+)", body, re.IGNORECASE)
        if not absolute:
            raise ValueError("What date and time should I use? Try 'tomorrow at 9am', 'on 2026-11-02 at 14:00', or 'in 10 minutes'.")
        title = absolute[1].strip()
        day_text = absolute[2].lower()
        if day_text in ('today', 'tomorrow'):
            day = now.astimezone(zone).date() + timedelta(days=day_text == 'tomorrow')
        else:
            try:
                day = datetime.strptime(day_text, '%Y-%m-%d').date()
            except ValueError:
                raise ValueError("That date isn't valid. Please use YYYY-MM-DD.") from None
        hour, minute = _parse_clock(absolute[3])
        due = _strict_local(datetime(day.year, day.month, day.day, hour, minute), zone)
    if not title or len(title) > 500:
        raise ValueError("Please give the task a short title of at most 500 characters.")
    if due <= now:
        raise ValueError("That time has already passed. What future date and time should I use?")
    local = due.astimezone(zone)
    return {
        'title': title,
        'due_at': due.isoformat(),
        'local_display': f"{local:%a, %d %b %Y at %H:%M} {local:%Z} (UTC{local:%z}; {timezone})",
        'timezone': timezone,
    }


def _quiet_clock(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("Quiet hours must use HH:MM in 24-hour time, for example 22:00 and 08:00.")
    hour, minute = map(int, value.split(':'))
    return hour * 60 + minute


def quiet_hours_next(instant: datetime, timezone: str, start: str | None, end: str | None) -> datetime:
    """Return instant or a conservative next quiet-hours end as an aware datetime.

    Both endpoints None disable quiet hours. Start is inclusive, end exclusive.
    Ambiguous DST end uses its later occurrence to avoid a repeated quiet period.
    Nonexistent DST end advances to the first valid local minute after the gap.
    """
    instant = _aware(instant)
    zone = _zone(timezone)
    if start is None and end is None:
        return instant
    if start is None or end is None:
        raise ValueError("Set both quiet-hour endpoints, or disable both.")
    start_minute, end_minute = _quiet_clock(start), _quiet_clock(end)
    if start_minute == end_minute:
        raise ValueError("Quiet-hour start and end must differ; use None for both to disable quiet hours.")
    local = instant.astimezone(zone)
    minute = local.hour * 60 + local.minute
    overnight = start_minute > end_minute
    inside = (minute >= start_minute or minute < end_minute) if overnight else start_minute <= minute < end_minute
    if not inside:
        return instant
    day = local.date() + timedelta(days=overnight and minute >= start_minute)
    wall = datetime(day.year, day.month, day.day, end_minute // 60, end_minute % 60)
    # Accommodate both ordinary DST gaps and historical date-line skips.
    # A spring-forward gap can swallow the *whole* non-quiet interval, so
    # recheck that the resolved boundary is genuinely outside quiet hours.
    for _ in range(3 * 24 * 60 + 1):
        options = _candidates(wall, zone)
        if options:
            result = options[-1]
            resolved = result.astimezone(zone)
            resolved_minute = resolved.hour * 60 + resolved.minute
            still_inside = (resolved_minute >= start_minute or resolved_minute < end_minute) if overnight else start_minute <= resolved_minute < end_minute
            if not still_inside:
                if result < instant.astimezone(UTC):
                    raise ValueError("Could not determine a future quiet-hours boundary.")
                return result
        wall += timedelta(minutes=1)
    raise ValueError("Could not resolve the quiet-hours boundary in this timezone.")
