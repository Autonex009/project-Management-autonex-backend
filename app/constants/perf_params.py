"""
Fixed monthly performance-review parameters.

These five parameters are hardcoded (no longer PM-defined). Every monthly
evaluation rates each of them 1-5. The employee sets `employee_rating`; the PM
sets `pm_rating`, `approved`, and (on reject) `feedback`. Keep this list in sync
with the frontend `src/components/perf/perfParams.js`.
"""

PERF_PARAMETERS = [
    {
        "name": "Quality of Work",
        "description": "Consistently delivers accurate, thorough, and high-quality work.",
    },
    {
        "name": "Productivity & Efficiency",
        "description": "Completes assigned tasks on time and effectively manages workload.",
    },
    {
        "name": "Communication Skills",
        "description": "Communicates clearly, professionally, and keeps relevant stakeholders informed.",
    },
    {
        "name": "Teamwork & Collaboration",
        "description": "Works well with colleagues, contributes positively, and supports others when needed.",
    },
    {
        "name": "Initiative & Problem Solving",
        "description": "Demonstrates ownership, proactively identifies issues, and proposes effective solutions.",
    },
]

# Ordered list of the canonical parameter names.
PERF_PARAM_NAMES = [p["name"] for p in PERF_PARAMETERS]

# Fast membership lookup.
PERF_PARAM_NAME_SET = set(PERF_PARAM_NAMES)

# Rating bounds (inclusive).
RATING_MIN = 1
RATING_MAX = 5
