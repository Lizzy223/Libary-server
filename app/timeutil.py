"""All timestamps are stored as naive UTC. Display and business-day logic use WAT (Africa/Lagos, UTC+1)."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

WAT = ZoneInfo("Africa/Lagos")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_wat(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(WAT)


def wat_to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=WAT).astimezone(timezone.utc).replace(tzinfo=None)


def wat_date(dt: datetime) -> date:
    return to_wat(dt).date()


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    start = wat_to_utc(datetime.combine(d, time(0, 0)))
    return start, start + timedelta(days=1)
