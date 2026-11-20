"""
Fixed monthly performance-review parameters with category-based weightages.

Seven parameters are hardcoded and grouped by Skill Category with specified weightages:
1. Productivity: Output vs. Standard Benchmark (25%)
2. Accuracy & Quality: QA / Error Identification Accuracy (25%)
3. Diligence: Correction & Feedback Compliance (10%)
4. Professional Discipline: Attendance & Punctuality (5%)
5. Knowledge & Understanding: Standardised Quiz / Assessment (15%)
6. Learning & Application: Application of Guidelines & Reduction in Repeat Errors (15%)
7. Communication & Engagement: Communication SLA Compliance (5%)
Total Weightage = 100% (1.00)

Every monthly evaluation rates each parameter 1-5. The overall rating is the weighted sum:
Overall Rating = sum(rating * weight) for each parameter.

Keep this list in sync with the frontend `src/components/perf/perfParams.js`.
"""
from typing import Any, Dict, List, Optional

PERF_PARAMETERS: List[Dict[str, Any]] = [
    {
        "category": "Productivity",
        "name": "Output vs. Standard Benchmark",
        "weight": 0.25,
        "weight_pct": 25,
        "description": "Volume and output delivery measured against defined project quotas and task benchmarks.",
    },
    {
        "category": "Accuracy & Quality",
        "name": "QA / Error Identification Accuracy",
        "weight": 0.25,
        "weight_pct": 25,
        "description": "Precision of work, QA pass rates, and thoroughness in identifying/preventing errors.",
    },
    {
        "category": "Diligence",
        "name": "Correction & Feedback Compliance",
        "weight": 0.10,
        "weight_pct": 10,
        "description": "Speed, accuracy, and adherence when applying reviewer corrections and feedback loops.",
    },
    {
        "category": "Professional Discipline",
        "name": "Attendance & Punctuality",
        "weight": 0.05,
        "weight_pct": 5,
        "description": "Punctuality, check-in discipline, shift adherence, and attendance consistency.",
    },
    {
        "category": "Knowledge & Understanding",
        "name": "Standardised Quiz / Assessment",
        "weight": 0.15,
        "weight_pct": 15,
        "description": "Performance on standardized domain quizzes, guidelines understanding, and platform assessments.",
    },
    {
        "category": "Learning & Application",
        "name": "Application of Guidelines & Reduction in Repeat Errors",
        "weight": 0.15,
        "weight_pct": 15,
        "description": "Proactive learning, guideline retention, and continuous reduction in repeat mistake patterns.",
    },
    {
        "category": "Communication & Engagement",
        "name": "Communication SLA Compliance",
        "weight": 0.05,
        "weight_pct": 5,
        "description": "Responsiveness on Slack/communication channels, prompt blocker escalation, and stakeholder communication.",
    },
]

# Ordered list of canonical parameter names
PERF_PARAM_NAMES: List[str] = [p["name"] for p in PERF_PARAMETERS]

# Fast membership lookup
PERF_PARAM_NAME_SET = set(PERF_PARAM_NAMES)

# Weights lookup dictionary {param_name: weight_float}
PERF_PARAM_WEIGHTS: Dict[str, float] = {p["name"]: p["weight"] for p in PERF_PARAMETERS}

# Category lookup dictionary {param_name: category_name}
PERF_PARAM_CATEGORIES: Dict[str, str] = {p["name"]: p["category"] for p in PERF_PARAMETERS}

# Legacy 5 parameters preserved for backward-compatible rendering/calculation of older cycles
LEGACY_PERF_PARAMETERS: List[Dict[str, str]] = [
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

LEGACY_PERF_PARAM_NAMES = [p["name"] for p in LEGACY_PERF_PARAMETERS]
LEGACY_PERF_PARAM_NAME_SET = set(LEGACY_PERF_PARAM_NAMES)

# Rating bounds (inclusive)
RATING_MIN = 1
RATING_MAX = 5


def calculate_weighted_rating(param_values: List[Dict[str, Any]], rating_key: str) -> Optional[float]:
    """Calculate the weighted overall rating out of 5.0.

    - For new evaluations using the 7 weighted parameters: computes sum(rating * weight).
    - For legacy evaluations using the 5 parameters: computes the arithmetic mean for backward compatibility.
    """
    if not param_values:
        return None

    # Check if this evaluation uses the new 7 weighted parameters
    has_weighted_param = any(p.get("name") in PERF_PARAM_WEIGHTS for p in param_values)
    if has_weighted_param:
        weighted_sum = 0.0
        total_weight = 0.0
        for p in param_values:
            name = p.get("name")
            rating = p.get(rating_key)
            if rating is not None and name in PERF_PARAM_WEIGHTS:
                w = PERF_PARAM_WEIGHTS[name]
                weighted_sum += float(rating) * w
                total_weight += w

        if total_weight > 0:
            return round(weighted_sum / total_weight, 2)

    # Legacy fallback: arithmetic mean
    vals = [float(p[rating_key]) for p in param_values if p.get(rating_key) is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)
