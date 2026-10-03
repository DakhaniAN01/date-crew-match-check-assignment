"""Pre-Share Match Check: the rules engine.

Hard preferences are deal-breakers. A single violation blocks the share.
Soft preferences carry weights and produce a 0-100 fit score.

Every hard rule is defined once and used twice:
  1. as a Python check (authoritative, explains the reason to the matchmaker)
  2. as a SQL prefilter (fast, cuts a large pool down before scoring)
A test asserts both paths agree, so the fast path can never drift from the rules.
"""

from dataclasses import dataclass, field
from typing import Any, Callable

EDUCATION_LEVELS = ["high_school", "bachelors", "masters", "doctorate"]

PASS_THRESHOLD = 70


@dataclass
class HardRule:
    key: str
    label: str
    applies: Callable[[dict], bool]
    check: Callable[[dict, dict], bool]
    describe: Callable[[dict, dict], str]
    sql: Callable[[dict], tuple[str, list]]


def _in_list(field_name: str, pref_key: str):
    return (
        lambda h: bool(h.get(pref_key)),
        lambda h, c: c[field_name] in h[pref_key],
        lambda h, c: f"candidate is '{c[field_name]}', client accepts {', '.join(h[pref_key])}",
        lambda h: (
            f"{field_name} IN ({','.join('?' * len(h[pref_key]))})",
            list(h[pref_key]),
        ),
    )


def _rule(key: str, label: str, parts) -> HardRule:
    applies, check, describe, sql = parts
    return HardRule(key, label, applies, check, describe, sql)


HARD_RULES: list[HardRule] = [
    HardRule(
        key="seeking_gender",
        label="Gender",
        applies=lambda h: bool(h.get("seeking_gender")),
        check=lambda h, c: c["gender"] == h["seeking_gender"],
        describe=lambda h, c: f"candidate is {c['gender']}, client is looking for {h['seeking_gender']}",
        sql=lambda h: ("gender = ?", [h["seeking_gender"]]),
    ),
    HardRule(
        key="age_range",
        label="Age",
        applies=lambda h: h.get("age_min") is not None and h.get("age_max") is not None,
        check=lambda h, c: h["age_min"] <= c["age"] <= h["age_max"],
        describe=lambda h, c: f"candidate is {c['age']}, client wants {h['age_min']} to {h['age_max']}",
        sql=lambda h: ("age BETWEEN ? AND ?", [h["age_min"], h["age_max"]]),
    ),
    _rule("religions", "Religion", _in_list("religion", "religions")),
    _rule("diets", "Diet", _in_list("diet", "diets")),
    _rule("smoking", "Smoking", _in_list("smoking", "smoking")),
    _rule("wants_children", "Children", _in_list("wants_children", "wants_children")),
    HardRule(
        key="cities",
        label="Location",
        applies=lambda h: bool(h.get("cities")),
        check=lambda h, c: c["city"] in h["cities"] or bool(c["open_to_relocate"]),
        describe=lambda h, c: (
            f"candidate lives in {c['city']} and will not relocate, client wants {', '.join(h['cities'])}"
        ),
        sql=lambda h: (
            f"(city IN ({','.join('?' * len(h['cities']))}) OR open_to_relocate = 1)",
            list(h["cities"]),
        ),
    ),
]


def _soft_satisfied(pref: dict, c: dict) -> bool:
    key, value = pref["key"], pref["value"]
    if key == "min_height_cm":
        return c["height_cm"] >= value
    if key == "max_height_cm":
        return c["height_cm"] <= value
    if key == "min_education":
        return EDUCATION_LEVELS.index(c["education"]) >= EDUCATION_LEVELS.index(value)
    if key == "professions":
        return c["profession"] in value
    if key == "drinking":
        return c["drinking"] in value
    return True


SOFT_LABELS = {
    "min_height_cm": lambda v: f"height at least {v} cm",
    "max_height_cm": lambda v: f"height at most {v} cm",
    "min_education": lambda v: f"education {v.replace('_', ' ')} or higher",
    "professions": lambda v: f"profession in {', '.join(v)}",
    "drinking": lambda v: f"drinking {', '.join(v)}",
}


@dataclass
class CheckResult:
    status: str  # BLOCK | WARN | PASS
    fit_score: int
    violations: list[dict] = field(default_factory=list)
    soft_mismatches: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "fit_score": self.fit_score,
            "violations": self.violations,
            "soft_mismatches": self.soft_mismatches,
        }


def hard_violations(hard: dict, candidate: dict) -> list[dict]:
    out = []
    for rule in HARD_RULES:
        if rule.applies(hard) and not rule.check(hard, candidate):
            out.append({"rule": rule.key, "label": rule.label, "reason": rule.describe(hard, candidate)})
    return out


def fit_score(soft: list[dict], candidate: dict) -> tuple[int, list[str]]:
    if not soft:
        return 100, []
    total = sum(p["weight"] for p in soft)
    got = 0
    mismatches = []
    for p in soft:
        if _soft_satisfied(p, candidate):
            got += p["weight"]
        else:
            mismatches.append(SOFT_LABELS.get(p["key"], lambda v: p["key"])(p["value"]))
    return round(100 * got / total), mismatches


def check(card: dict, candidate: dict) -> CheckResult:
    """Run the full pre-share check for one client and one candidate."""
    violations = hard_violations(card["hard"], candidate)
    score, mismatches = fit_score(card["soft"], candidate)
    if violations:
        status = "BLOCK"
    elif score >= PASS_THRESHOLD:
        status = "PASS"
    else:
        status = "WARN"
    return CheckResult(status, score, violations, mismatches)


def sql_prefilter(hard: dict) -> tuple[str, list]:
    """Translate the hard rules into a SQL WHERE fragment with bound parameters."""
    clauses, params = [], []
    for rule in HARD_RULES:
        if rule.applies(hard):
            clause, p = rule.sql(hard)
            clauses.append(clause)
            params.extend(p)
    return (" AND ".join(clauses) if clauses else "1=1"), params
