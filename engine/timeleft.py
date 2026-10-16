"""Human-readable time remaining for auctions."""
from datetime import datetime, timezone


def time_left(end: datetime | None, now: datetime | None = None) -> str | None:
    """'2d 4h', '3h 12m', '8m', '<1m', or 'ended'; None when there is no end time."""
    if end is None:
        return None
    secs = int(((end - (now or datetime.now(timezone.utc))).total_seconds()))
    if secs <= 0:
        return "ended"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else "<1m"
