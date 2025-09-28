# services/srs.py
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

Grade = int  # 1=again, 2=hard, 3=good, 4=easy

def update_srs(
    grade: Grade,
    prev_interval_days: Optional[int],
    prev_ease: Optional[float],
    now: Optional[datetime] = None,
) -> Tuple[float, int, datetime]:
    """
    Returns (new_ease: float, new_interval_days: int, due_at: datetime[UTC])
    Pure + deterministic. No DB or globals.
    """
    now = now or datetime.now(timezone.utc)

    ease = (prev_ease or 2.5)
    if grade == 1:   # again
        ease -= 0.30
    elif grade == 2: # hard
        ease -= 0.20
    elif grade == 4: # easy
        ease += 0.10
    ease = max(1.3, min(2.8, ease))

    if prev_interval_days is None:              # first “graduation”
        interval = 1 if grade < 3 else 2
    elif grade == 1:
        interval = 1
    elif grade == 2:
        interval = max(1, int(prev_interval_days * 0.6))
    elif grade == 3:
        interval = int(prev_interval_days * ease)
    else:  # grade == 4
        interval = int(prev_interval_days * ease * 1.3)

    interval = max(1, interval)
    due_at = now + timedelta(days=interval)
    return ease, interval, due_at
