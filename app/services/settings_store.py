"""Configurable rules (ADM-1). Values live in the settings table; DEFAULTS apply until changed.
All numbers below are the SRS section 5 assumptions and must be confirmed by library management."""
from copy import deepcopy

from sqlalchemy.orm import Session

from ..models import Setting

DEFAULTS: dict = {
    "library_name": "NITT Library, Zaria",
    "loan_days": {"student": 14, "academic_staff": 30, "non_academic_staff": 30, "visitor": 14},   # BR-5
    "max_items": {"student": 3, "academic_staff": 10, "non_academic_staff": 5, "visitor": 1},      # BR-1..4
    "max_renewals": {"student": 1, "academic_staff": 2, "non_academic_staff": 2, "visitor": 0},    # BR-6
    "closing_time": "17:00",
    "short_loan_hours_before_close": 1,                                                            # BR-7
    "closure_weekdays": [5, 6],           # Monday=0 ... Saturday=5, Sunday=6
    "closure_dates": [],                  # ISO dates, e.g. public holidays
    "fine_per_day": 50,                   # BR-9 is TBD in the SRS: placeholder in naira
    "fine_cap_default": 5000,             # used when a copy has no replacement cost
    "fine_block_threshold": 1000,         # BR-10 placeholder
    "overdue_block_days": 14,             # BR-10
    "hold_pickup_hours": 48,              # BR-11
    "max_holds": 3,                       # RES-6
    "replacement_fee": 500,               # BR-12 placeholder
    "reminder_days_after_due": [-2, 0, 1, 7, 14],  # NOT-1
    "templates": {
        "due_soon": {"subject": "Reminder: '{title}' is due on {due_date}",
                     "body": "Hello {name}, '{title}' is due on {due_date}. Renew it online if you still need it."},
        "due_today": {"subject": "'{title}' is due today",
                      "body": "Hello {name}, '{title}' is due today ({due_date}). Please return or renew it to avoid fines."},
        "overdue": {"subject": "Overdue: '{title}'",
                    "body": "Hello {name}, '{title}' was due on {due_date} and is now {days} day(s) overdue. "
                            "Fines apply until it is returned."},
        "hold_ready": {"subject": "Your hold is ready: '{title}'",
                       "body": "Hello {name}, '{title}' is ready at the hold shelf. Please collect it before {expires}."},
        "hold_expired": {"subject": "Hold expired: '{title}'",
                         "body": "Hello {name}, your hold on '{title}' was not collected in time and has expired."},
        "fine_posted": {"subject": "A fine has been posted to your account",
                        "body": "Hello {name}, a fine has been added for '{title}'. Current amount: {amount}."},
        "renewed": {"subject": "Renewed: '{title}'",
                    "body": "Hello {name}, '{title}' has been renewed. New due date: {due_date}."},
        "password_reset": {"subject": "Your library password reset code",
                           "body": "Hello {name}, your one-time code is {code}. It expires in 15 minutes."},
    },
}


def get_all(db: Session) -> dict:
    cfg = deepcopy(DEFAULTS)
    for row in db.query(Setting).all():
        if row.key in cfg and isinstance(cfg[row.key], dict) and isinstance(row.value, dict):
            cfg[row.key].update(row.value)
        else:
            cfg[row.key] = row.value
    return cfg


def update(db: Session, values: dict) -> dict:
    """Only known keys are accepted. Returns the keys that changed (old, new)."""
    changed = {}
    current = get_all(db)
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError(f"Unknown setting: {key}")
        if current[key] != value:
            changed[key] = {"old": current[key], "new": value}
            row = db.get(Setting, key)
            if row:
                row.value = value
            else:
                db.add(Setting(key=key, value=value))
    return changed
