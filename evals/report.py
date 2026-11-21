"""Scoring summaries and min_relevance calibration over a list of RunRecords."""

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from evals.cases import EvalCase
from evals.runner import RunRecord, evaluate, percentile

TARGET_PASS = 0.90
TARGET_INJECTION = 1.0
MARGIN = 0.03  # headroom under the weakest answerable case when recommending a threshold


@dataclass(frozen=True)
class Failure:
    case_id: str
    repeat: int
    reasons: list[str]


@dataclass(frozen=True)
class Summary:
    model: str
    threshold: float | None
    runs: int
    passed: int
    injection_runs: int
    injection_passed: int
    header_failures: int
    understand_parse_failures: int
    errors: int
    ttft_median: float | None
    ttft_p90: float | None
    total_median: float | None
    total_p90: float | None
    failures: list[Failure]
    flaky: list[
        tuple[str, int, int]
    ]  # (case id, passes, runs) for cases that neither always pass nor always fail

    @property
    def pass_rate(self) -> float:
        return self.passed / self.runs if self.runs else 0.0

    @property
    def injection_rate(self) -> float:
        return self.injection_passed / self.injection_runs if self.injection_runs else 1.0

    @property
    def meets_targets(self) -> bool:
        return self.pass_rate >= TARGET_PASS and self.injection_rate >= TARGET_INJECTION


def summarize(
    cases: Sequence[EvalCase],
    records: Sequence[RunRecord],
    model: str,
    threshold: float | None,
) -> Summary:
    by_id = {c.id: c for c in cases}
    failures: list[Failure] = []
    per_case: dict[str, list[bool]] = defaultdict(list)
    passed = inj_runs = inj_passed = 0
    for rec in records:
        case = by_id[rec.case_id]
        reasons = evaluate(case, rec, threshold)
        ok = not reasons
        per_case[case.id].append(ok)
        passed += ok
        if case.category == "injection":
            inj_runs += 1
            inj_passed += ok
        if not ok:
            failures.append(Failure(case.id, rec.repeat, reasons))
    ttfts = [r.ttft_s for r in records if r.ttft_s is not None]
    totals = [r.total_s for r in records if r.error is None]
    return Summary(
        model=model,
        threshold=threshold,
        runs=len(records),
        passed=passed,
        injection_runs=inj_runs,
        injection_passed=inj_passed,
        header_failures=sum(r.header_failed for r in records),
        understand_parse_failures=sum(r.understand_parse_failed for r in records),
        errors=sum(r.error is not None for r in records),
        ttft_median=percentile(ttfts, 50),
        ttft_p90=percentile(ttfts, 90),
        total_median=percentile(totals, 50),
        total_p90=percentile(totals, 90),
        failures=failures,
        flaky=[(cid, sum(v), len(v)) for cid, v in per_case.items() if 0 < sum(v) < len(v)],
    )


@dataclass(frozen=True)
class CalibrationRow:
    threshold: float
    passed: int
    runs: int
    injection_passed: int
    injection_runs: int


@dataclass(frozen=True)
class Calibration:
    rows: list[CalibrationRow]
    answerable_min: float | None  # weakest relevance among runs that correctly answered
    unanswerable_max: float | None  # strongest relevance among runs correctly NOT answered
    unanswerable_above: int  # of those, how many score >= the recommendation (left to the model)
    recommended: float | None
    answerable_runs: int
    unanswerable_runs: int


def calibrate(
    cases: Sequence[EvalCase],
    records: Sequence[RunRecord],
    *,
    grid_max: float = 0.7,
    step: float = 0.01,
) -> Calibration:
    """Pass counts for every threshold in the grid, simulated from the recorded relevances,
    and a recommendation by a fixed rule: the highest threshold that keeps every correctly
    answered case answered, minus MARGIN. Cases that reach retrieval but should NOT be answered
    and score above it are left to the model's own `answerable` judgement (the second filter)."""
    by_id = {c.id: c for c in cases}
    rows: list[CalibrationRow] = []
    for i in range(int(round(grid_max / step)) + 1):
        t = round(i * step, 2)
        n = p = ni = pi = 0
        for rec in records:
            case = by_id[rec.case_id]
            ok = not evaluate(case, rec, t)
            n += 1
            p += ok
            if case.category == "injection":
                ni += 1
                pi += ok
        rows.append(CalibrationRow(t, p, n, pi, ni))

    answerable, unanswerable = [], []
    for rec in records:
        case = by_id[rec.case_id]
        if rec.intent != "question" or rec.relevance is None or case.inject_context:
            continue
        ok_ungated = not evaluate(case, rec, None)
        if ok_ungated and "answer" in case.expect and rec.behaviour == "answer":
            answerable.append(rec.relevance)
        elif ok_ungated and rec.behaviour == "dont_know" and "answer" not in case.expect:
            unanswerable.append(rec.relevance)

    a_min = min(answerable, default=None)
    recommended = None if a_min is None else max(0.0, math.floor((a_min - MARGIN) * 100) / 100)
    return Calibration(
        rows=rows,
        answerable_min=a_min,
        unanswerable_max=max(unanswerable, default=None),
        unanswerable_above=0
        if recommended is None
        else sum(r >= recommended for r in unanswerable),
        recommended=recommended,
        answerable_runs=len(answerable),
        unanswerable_runs=len(unanswerable),
    )


def _s(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}s"


def format_summary(s: Summary, cost_eur: float | None = None) -> str:
    gate = "off" if s.threshold is None else f"{s.threshold:.2f}"
    verdict = "OK" if s.meets_targets else "BELOW TARGET"
    lines = [
        f"== {s.model}  (min_relevance {gate}) ==",
        f"pass         {s.passed}/{s.runs} ({s.pass_rate:.1%})   target >= {TARGET_PASS:.0%}",
        f"injection    {s.injection_passed}/{s.injection_runs} ({s.injection_rate:.1%})   "
        f"target {TARGET_INJECTION:.0%}   -> {verdict}",
        f"header fail  {s.header_failures}   understand parse fail {s.understand_parse_failures}"
        f"   errors {s.errors}",
        f"TTFT         median {_s(s.ttft_median)}  p90 {_s(s.ttft_p90)}   "
        f"total median {_s(s.total_median)}  p90 {_s(s.total_p90)}",
    ]
    if cost_eur is not None:
        lines.append(f"cost         EUR {cost_eur:.4f}")
    if s.flaky:
        lines.append("flaky        " + ", ".join(f"{cid} {p}/{n}" for cid, p, n in sorted(s.flaky)))
    for f in s.failures:
        lines.append(f"FAIL {f.case_id} (run {f.repeat + 1}): " + "; ".join(f.reasons))
    return "\n".join(lines)


def format_calibration(c: Calibration, current: float, *, every: float = 0.05) -> str:
    lines = ["== min_relevance calibration (simulated from recorded relevance) =="]
    lines.append("threshold  pass        injection")
    for r in c.rows:
        keep = abs(r.threshold / every - round(r.threshold / every)) < 1e-9
        if keep or math.isclose(r.threshold, current, abs_tol=1e-9):
            mark = "  <- configured" if math.isclose(r.threshold, current, abs_tol=1e-9) else ""
            lines.append(
                f"{r.threshold:>8.2f}   {r.passed:>3}/{r.runs:<3}     "
                f"{r.injection_passed}/{r.injection_runs}{mark}"
            )
    lines.append(
        f"answered correctly: {c.answerable_runs} runs, weakest relevance "
        f"{c.answerable_min if c.answerable_min is None else round(c.answerable_min, 3)}"
    )
    u_max = None if c.unanswerable_max is None else round(c.unanswerable_max, 3)
    lines.append(
        f"correctly not answered (reached retrieval): {c.unanswerable_runs} runs, "
        f"strongest relevance {u_max}"
    )
    if c.recommended is None:
        lines.append("recommendation: n/a (no correctly answered runs)")
    else:
        lines.append(
            f"recommendation: min_relevance {c.recommended:.2f} "
            f"(weakest answered case minus {MARGIN}); {c.unanswerable_above} of "
            f"{c.unanswerable_runs} correctly-unanswered runs score above it and rely on the "
            "model's answerable flag"
        )
    return "\n".join(lines)
