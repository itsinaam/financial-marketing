"""The subscription plans shown on the Subscriptions screen.

Prices live here rather than coming from the browser, so a checkout can never be
started for an amount the client made up.
"""

YEARLY_DISCOUNT = 0.20

_SHARED_FEATURES = [
    "System notifications",
    "Post approval queue",
    "Calendars view",
    "Social media pages Integration",
    "Post Mobile/Web view",
]

_PAID_FEATURES = [
    "Whatsapp/Slack/Teams/Email notifications",
    "Weekly/Monthly Planner",
]

PLANS = [
    {
        "code": "free",
        "name": "Free",
        "monthly_price": 0,
        "tagline": "For solo creators just getting started.",
        "badge": None,
        "posts_per_month": 5,
        "businesses": 1,
        "features": ["Generate 5 posts per month", "Up to 1 business", *_SHARED_FEATURES],
    },
    {
        "code": "pro",
        "name": "Pro",
        "monthly_price": 49,
        "tagline": "For growing teams managing a couple of brands.",
        "badge": None,
        "posts_per_month": 100,
        "businesses": 2,
        "features": [
            "Generate 100 posts per month",
            "Up to 2 businesses",
            _SHARED_FEATURES[0],
            *_PAID_FEATURES,
            *_SHARED_FEATURES[1:],
        ],
    },
    {
        "code": "plus",
        "name": "Plus",
        "monthly_price": 99,
        "tagline": "For agencies managing multiple clients.",
        "badge": "MOST POPULAR",
        "posts_per_month": 200,
        "businesses": 5,
        "features": [
            "Generate 200 posts per month",
            "Up to 5 businesses",
            _SHARED_FEATURES[0],
            *_PAID_FEATURES,
            *_SHARED_FEATURES[1:],
        ],
    },
    {
        "code": "top_tier",
        "name": "Top Tier",
        "monthly_price": 299,
        "tagline": "For large teams that need maximum scale.",
        "badge": "PREMIUM",
        # None means no cap.
        "posts_per_month": None,
        "businesses": None,
        "features": [
            "Generate unlimited posts per month",
            "Up to the maximum number of businesses",
            _SHARED_FEATURES[0],
            *_PAID_FEATURES,
            *_SHARED_FEATURES[1:],
        ],
    },
]

PLANS_BY_CODE = {plan["code"]: plan for plan in PLANS}
FREE_PLAN_CODE = "free"


def yearly_price(monthly_price: int) -> int:
    """A year up front at 20% off, to the whole dollar, matching the pricing table."""
    return int(monthly_price * 12 * (1 - YEARLY_DISCOUNT))


def price_for(plan_code: str, billing_period: str) -> float:
    plan = PLANS_BY_CODE[plan_code]
    return float(yearly_price(plan["monthly_price"]) if billing_period == "yearly" else plan["monthly_price"])


def plan_label(plan_code: str, billing_period: str) -> str:
    """What the customer sees on the Stripe page and on their payment record."""
    period = "Yearly" if billing_period == "yearly" else "Monthly"
    return f"{PLANS_BY_CODE[plan_code]['name']} Plan ({period})"
