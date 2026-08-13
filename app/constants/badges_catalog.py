"""Badge catalog (codes, labels, period type, expiry)."""

from typing import TypedDict


class BadgeDef(TypedDict):
    code: str
    name: str
    description: str
    category: str  # hours | tenure | ranking | milestone
    period_type: str  # weekly | monthly | yearly | one_time
    expires: bool


BADGE_CATALOG: dict[str, BadgeDef] = {
    "hrs_50_week": {
        "code": "hrs_50_week",
        "name": "50 Hours in a Week",
        "description": "Logged at least 50 hours in a calendar week.",
        "category": "hours",
        "period_type": "weekly",
        "expires": True,
    },
    "hrs_200_month": {
        "code": "hrs_200_month",
        "name": "200 Hours in a Month",
        "description": "Logged at least 200 hours in a calendar month.",
        "category": "hours",
        "period_type": "monthly",
        "expires": True,
    },
    "tenure_3_months": {
        "code": "tenure_3_months",
        "name": "3 Months Completed",
        "description": "Completed 3 months with the company.",
        "category": "tenure",
        "period_type": "one_time",
        "expires": False,
    },
    "tenure_6_months": {
        "code": "tenure_6_months",
        "name": "6 Months Completed",
        "description": "Completed 6 months with the company.",
        "category": "tenure",
        "period_type": "one_time",
        "expires": False,
    },
    "weekly_top_1": {
        "code": "weekly_top_1",
        "name": "Weekly Top 1",
        "description": "Ranked 1st by hours in the week.",
        "category": "ranking",
        "period_type": "weekly",
        "expires": True,
    },
    "weekly_top_2": {
        "code": "weekly_top_2",
        "name": "Weekly Top 2",
        "description": "Ranked 2nd by hours in the week.",
        "category": "ranking",
        "period_type": "weekly",
        "expires": True,
    },
    "weekly_top_3": {
        "code": "weekly_top_3",
        "name": "Weekly Top 3",
        "description": "Ranked 3rd by hours in the week.",
        "category": "ranking",
        "period_type": "weekly",
        "expires": True,
    },
    "monthly_top_1": {
        "code": "monthly_top_1",
        "name": "Monthly Top 1",
        "description": "Ranked 1st by hours in the month.",
        "category": "ranking",
        "period_type": "monthly",
        "expires": True,
    },
    "monthly_top_2": {
        "code": "monthly_top_2",
        "name": "Monthly Top 2",
        "description": "Ranked 2nd by hours in the month.",
        "category": "ranking",
        "period_type": "monthly",
        "expires": True,
    },
    "monthly_top_3": {
        "code": "monthly_top_3",
        "name": "Monthly Top 3",
        "description": "Ranked 3rd by hours in the month.",
        "category": "ranking",
        "period_type": "monthly",
        "expires": True,
    },
    "yearly_milestone": {
        "code": "yearly_milestone",
        "name": "Yearly Milestone",
        "description": "Yearly milestone achievement.",
        "category": "milestone",
        "period_type": "yearly",
        "expires": False,
    },
}

VALID_BADGE_CODES = set(BADGE_CATALOG.keys())