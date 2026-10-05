"""Score a hunt against SYNERGY labels.

The claim under test is whether the elusion estimate is honest, not whether
retrieval found the included records. Nothing here is adjusted to improve a
number.

S is computed two ways:
  hits           band == hit
  hits_and_maybe band in {hit, maybe}   (the methodology's recall target)

The elusion sample the product draws is taken from outside hits and maybes.
The observed estimate and its calibration error are for that set. A hits-only
elusion rate is reported as well, but the product did not draw its sample
from that pool, so it is not calibrated against the sample.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
CORPORA = ROOT / "corpora"
RESULTS = ROOT / "results"

WORK_ID = re.compile(r"(W\d+)", re.I)
SIMULATION_DRAWS = 10_000
SIMULATION_SEED = 20260930
SWEEP_MIN = 10
SWEEP_MAX = 5_000
RELATIVE_TOLERANCE = 0.20
CONFIDENCE = 0.95

WHAT_THIS_DOES_NOT_SHOW = (
    "What this does not show. SYNERGY is titles and abstracts only, so this tests "
    "the assurance method and not the ingestion pipeline. The candidate pool is "
    "already the output of a human Boolean search, so coverage is over that pool "
    "and not over all literature."
)


def work_token(value: str) -> str | None:
    match = WORK_ID.search(value or "")
    return match.group(1) if match else None


def token_from_filename(filename: str, review_key: str) -> str | None:
    name = filename.strip()
    prefix = f"SYN-{review_key}-"
    if name.lower().endswith(".txt"):
        name = name[:-4]
    if not name.startswith(prefix):
        return None
    token = name[len(prefix):]
    return token if WORK_ID.fullmatch(token) else None


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def log_comb(n: int, k: int) -> float:
    if k < 0 or n < 0 or k > n:
        return float("-inf")
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def log_pmf(pool: int, good: int, n: int, k: int) -> float:
    bad = pool - good
    if k < 0 or k > good or n - k < 0 or n - k > bad:
        return float("-inf")
    return log_comb(good, k) + log_comb(bad, n - k) - log_comb(pool, n)


def draw_distribution(pool: int, good: int, n: int) -> tuple[int, list[float]]:
    """Equal-tailed probabilities of the hypergeometric count, normalised."""
    if n > pool:
        raise ValueError(f"sample {n} exceeds pool {pool}")
    if n == pool:
        return good, [1.0]
    k_min = max(0, n - (pool - good))
    k_max = min(n, good)
    logs = [log_pmf(pool, good, n, k) for k in range(k_min, k_max + 1)]
    peak = max(logs)
    weights = [math.exp(value - peak) for value in logs]
    total = sum(weights)
    if total <= 0:
        raise RuntimeError("hypergeometric weights summed to 0")
    return k_min, [weight / total for weight in weights]


def central_counts(pool: int, good: int, n: int) -> tuple[int, int]:
    """Smallest counts whose cdf reaches 2.5% and 97.5%."""
    k_min, probs = draw_distribution(pool, good, n)
    cdf = 0.0
    lo = None
    hi = None
    for offset, prob in enumerate(probs):
        cdf += prob
        k = k_min + offset
        if lo is None and cdf >= 0.025:
            lo = k
        if hi is None and cdf >= 0.975:
            hi = k
            break
    if lo is None:
        lo = k_min
    if hi is None:
        hi = k_min + len(probs) - 1
    return lo, hi


def p_zero(pool: int, good: int, n: int) -> float:
    if good <= 0:
        return 1.0
    if n > pool - good:
        return 0.0
    k_min, probs = draw_distribution(pool, good, n)
    if k_min > 0:
        return 0.0
    return probs[0]


def within_relative(pool: int, good: int, n: int) -> bool:
    """95% interval of the estimate sits inside ±20% of the true rate."""
    if pool <= 0 or good <= 0 or n <= 0:
        return False
    if n > pool:
        return False
    true = good / pool
    lo, hi = central_counts(pool, good, n)
    low = lo / n
    high = hi / n
    return low >= true * (1 - RELATIVE_TOLERANCE) - 1e-12 and high <= true * (1 + RELATIVE_TOLERANCE) + 1e-12


def minimum_sample_size(pool: int, good: int) -> int | None:
    """Smallest n that estimates the true rate to within ±20% at 95% confidence.

    None when the true rate is zero: relative error is undefined, and every
    sample correctly returns no misses.
    """
    if good <= 0 or pool <= 0:
        return None
    if not within_relative(pool, good, pool):
        return None
    lo, hi = 1, pool
    answer = pool
    while lo <= hi:
        mid = (lo + hi) // 2
        if within_relative(pool, good, mid):
            answer = mid
            hi = mid - 1
        else:
            lo = mid + 1
    if answer > 1 and within_relative(pool, good, answer - 1):
        for n in range(1, pool + 1):
            if within_relative(pool, good, n):
                return n
    return answer


def sweep(pool: int, good: int) -> list[dict]:
    rows = []
    if pool <= 0:
        return rows
    true = good / pool
    last = min(SWEEP_MAX, pool)
    for n in range(min(SWEEP_MIN, last), last + 1):
        lo, hi = central_counts(pool, good, n)
        rows.append(
            {
                "n": n,
                "interval_low": lo / n,
                "interval_high": hi / n,
                "interval_width": (hi - lo) / n,
                "p_zero_misses": None if good <= 0 else p_zero(pool, good, n),
                "within_20pct_relative_95": within_relative(pool, good, n) if good > 0 else None,
                "true_rate": true,
            }
        )
    return rows


def simulate(pool: int, good: int, n: int, draws: int = SIMULATION_DRAWS) -> dict:
    if pool <= 0 or n <= 0:
        return {"draws": 0}
    n = min(n, pool)
    rng = np.random.default_rng(SIMULATION_SEED)
    counts = rng.hypergeometric(ngood=good, nbad=pool - good, nsample=n, size=draws)
    estimates = counts / n
    low, high = np.quantile(estimates, [0.025, 0.975])
    return {
        "draws": draws,
        "seed": SIMULATION_SEED,
        "sample_size": n,
        "mean": float(estimates.mean()),
        "median": float(np.median(estimates)),
        "proportion_zero_misses": float(np.mean(counts == 0)),
        "interval_95": [float(low), float(high)],
        "true_rate": good / pool,
    }


def load_manifest(review_key: str) -> dict[str, dict]:
    rows = read_csv(CORPORA / review_key / "manifest.csv")
    by_token: dict[str, dict] = {}
    for row in rows:
        token = work_token(row["record_id"])
        if not token:
            raise SystemExit(f"{review_key}: manifest record_id {row['record_id']!r} has no work id")
        if token in by_token:
            raise SystemExit(f"{review_key}: duplicate work id {token} in the manifest")
        by_token[token] = row
    return by_token


def band_set(members: list[dict], review_key: str, manifest: dict[str, dict], bands: set[str]) -> tuple[set[str], list[str]]:
    chosen: set[str] = set()
    problems: list[str] = []
    for row in members:
        token = token_from_filename(row.get("filename") or "", review_key)
        if not token or token not in manifest:
            problems.append(f"member filename {row.get('filename')!r} does not join to the manifest")
            continue
        if (row.get("band") or "") in bands:
            chosen.add(token)
    return chosen, problems


def score_definition(
    *,
    name: str,
    retrieved: set[str],
    relevant: set[str],
    universe: set[str],
    sample_tokens: list[str] | None,
    sample_size_for_simulation: int,
) -> dict:
    outside = universe - retrieved
    missed = relevant - retrieved
    found = relevant & retrieved
    n = len(universe)
    r = len(relevant)
    true_elusion = (len(missed) / len(outside)) if outside else None
    recall = (len(found) / r) if r else None
    coverage = (len(retrieved) / n) if n else None
    result = {
        "definition": name,
        "retrieved": len(retrieved),
        "coverage": coverage,
        "relevant_retrieved": len(found),
        "true_recall": recall,
        "outside": len(outside),
        "relevant_outside": len(missed),
        "true_elusion_rate": true_elusion,
        "required_sample_size": minimum_sample_size(len(outside), len(missed)),
    }
    if true_elusion is None:
        result["required_sample_size_note"] = "no documents outside the retrieved set"
    elif len(missed) == 0:
        result["required_sample_size_note"] = (
            "true elusion rate is 0, so a relative error of ±20% is undefined; "
            "every sample correctly finds no misses"
        )
    if sample_tokens is not None:
        labelled = [token for token in sample_tokens]
        misses = sum(1 for token in labelled if token in relevant)
        size = len(labelled)
        observed = (misses / size) if size else None
        result["observed_sample_size"] = size
        result["observed_misses"] = misses
        result["observed_elusion_estimate"] = observed
        result["calibration_error"] = (
            abs(observed - true_elusion) if observed is not None and true_elusion is not None else None
        )
    sim_n = sample_size_for_simulation
    if outside and sim_n:
        result["simulation"] = simulate(len(outside), len(missed), sim_n)
    else:
        result["simulation"] = {"draws": 0, "note": "no outside pool or no sample size"}
    result["sweep_path_note"] = "sample_size_sweep.csv"
    return result


def write_sweep(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def score_review(review_key: str, results: Path = RESULTS) -> dict:
    manifest = load_manifest(review_key)
    members = read_csv(results / review_key / "scope_members.csv")
    sample = read_csv(results / review_key / "elusion_sample.csv")
    methodology = json.loads((results / review_key / "methodology.json").read_text(encoding="utf-8"))
    run = json.loads((results / review_key / "run.json").read_text(encoding="utf-8"))

    universe = set(manifest)
    relevant = {
        token
        for token, row in manifest.items()
        if str(row.get("label_included")) == "1"
    }
    empty_abstracts = sum(1 for row in manifest.values() if str(row.get("abstract_empty")) == "1")
    hits, hit_problems = band_set(members, review_key, manifest, {"hit"})
    hits_maybe, maybe_problems = band_set(members, review_key, manifest, {"hit", "maybe"})
    problems = hit_problems + [item for item in maybe_problems if item not in hit_problems]

    other_bands: dict[str, int] = {}
    for row in members:
        band = row.get("band") or ""
        if band not in {"hit", "maybe"}:
            other_bands[band] = other_bands.get(band, 0) + 1

    sample_tokens: list[str] = []
    sample_problems: list[str] = []
    for row in sample:
        token = token_from_filename(row.get("filename") or "", review_key)
        if not token or token not in manifest:
            sample_problems.append(f"elusion filename {row.get('filename')!r} does not join to the manifest")
            continue
        if token in hits_maybe:
            sample_problems.append(
                f"{token} is in the elusion sample and also in the hits+maybe set; "
                "the sample is not drawn from the non-retrieved pool"
            )
        sample_tokens.append(token)
    if len(sample_tokens) != len(set(sample_tokens)):
        sample_problems.append("elusion sample contains a repeated record")

    sample_n = len(sample_tokens)
    method_n = methodology.get("elusion_sample_size")
    primary = score_definition(
        name="hits_and_maybe",
        retrieved=hits_maybe,
        relevant=relevant,
        universe=universe,
        sample_tokens=sample_tokens,
        sample_size_for_simulation=sample_n,
    )
    hits_only = score_definition(
        name="hits",
        retrieved=hits,
        relevant=relevant,
        universe=universe,
        sample_tokens=None,
        sample_size_for_simulation=sample_n,
    )

    outside = universe - hits_maybe
    missed = relevant - hits_maybe
    primary_sweep = sweep(len(outside), len(missed))
    write_sweep(results / review_key / "sample_size_sweep.csv", primary_sweep)
    hits_outside = universe - hits
    hits_sweep = sweep(len(hits_outside), len(relevant - hits))
    write_sweep(results / review_key / "sample_size_sweep_hits.csv", hits_sweep)

    warnings = list(problems) + list(sample_problems)
    n = len(universe)
    comparisons = {
        "methodology_corpus_size": methodology.get("corpus_size"),
        "manifest_n": n,
        "methodology_retrieved": methodology.get("retrieved"),
        "hits_and_maybe": len(hits_maybe),
        "methodology_hit_count": methodology.get("hit_count"),
        "hits": len(hits),
        "methodology_maybe_count": methodology.get("maybe_count"),
        "maybes": len(hits_maybe - hits),
        "methodology_coverage": methodology.get("coverage"),
        "methodology_version": methodology.get("version"),
        "methodology_elusion_sample_size": method_n,
        "exported_elusion_rows": len(sample),
        "joined_elusion_rows": sample_n,
    }
    if comparisons["methodology_corpus_size"] not in (None, n):
        warnings.append(
            f"methodology corpus_size {comparisons['methodology_corpus_size']} != manifest N {n}"
        )
    if comparisons["methodology_retrieved"] not in (None, len(hits_maybe)):
        warnings.append(
            f"methodology retrieved {comparisons['methodology_retrieved']} != |hits+maybe| {len(hits_maybe)}"
        )
    if comparisons["methodology_hit_count"] not in (None, len(hits)):
        warnings.append(
            f"methodology hit_count {comparisons['methodology_hit_count']} != |hits| {len(hits)}"
        )
    if method_n not in (None, sample_n):
        warnings.append(
            f"methodology elusion_sample_size {method_n} != joined sample {sample_n}"
        )
    if primary["coverage"] is not None and methodology.get("coverage") is not None:
        reported = round(primary["coverage"], 4)
        if abs(float(methodology["coverage"]) - reported) > 0.0001:
            warnings.append(
                f"methodology coverage {methodology['coverage']} != |hits+maybe|/N {reported}"
            )
    if other_bands:
        warnings.append(f"scope members with a band other than hit or maybe: {other_bands}")

    # Maybes are capped at RECALL_DOC_LIMIT (800). Hits are not. A maybe count
    # sitting on that cap means the possible-match tail was cut, so the set is
    # not the full net the channels returned.
    if len(hits_maybe - hits) >= 800:
        warnings.append(
            "maybe band is at 800 documents, the product's recall cap "
            "(RECALL_DOC_LIMIT). The possible-match tail was cut."
        )

    result = {
        "review_key": review_key,
        "N": n,
        "R": len(relevant),
        "empty_abstracts": empty_abstracts,
        "question_source": run.get("question_source"),
        "question_reconstructed": run.get("question_reconstructed"),
        "strategy": run.get("strategy"),
        "hits_and_maybe": primary,
        "hits": hits_only,
        "comparisons": comparisons,
        "warnings": warnings,
        "invalid": bool(problems or sample_problems),
    }
    (results / review_key / "score.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    _print_review(result)
    return result


def _fmt(value, digits=4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _print_review(result: dict) -> None:
    primary = result["hits_and_maybe"]
    hits = result["hits"]
    print(f"{result['review_key']}: N={result['N']} R={result['R']} empty_abstracts={result['empty_abstracts']}")
    print(
        f"  coverage hits+maybe={_fmt(primary['coverage'])} hits={_fmt(hits['coverage'])} "
        f"recall hits+maybe={_fmt(primary['true_recall'])} hits={_fmt(hits['true_recall'])}"
    )
    print(
        f"  true elusion={_fmt(primary['true_elusion_rate'])} "
        f"observed={_fmt(primary.get('observed_elusion_estimate'))} "
        f"calibration={_fmt(primary.get('calibration_error'))} "
        f"required_n={primary.get('required_sample_size')}"
    )
    sim = primary.get("simulation") or {}
    if sim.get("draws"):
        print(
            f"  simulation n={sim['sample_size']} mean={_fmt(sim['mean'])} median={_fmt(sim['median'])} "
            f"P(zero)={_fmt(sim['proportion_zero_misses'])} "
            f"95%={sim['interval_95']}"
        )
    for warning in result["warnings"]:
        print(f"  WARNING: {warning}")


def _pct(value) -> str:
    if value is None:
        return "—"
    pct = 100 * float(value)
    if abs(pct) < 1:
        return f"{pct:.2f}%"
    return f"{pct:.1f}%"


def prose(results: list[dict]) -> str:
    """Plain-language findings. A small absolute error is not described as success when the sample usually finds nothing."""
    usable = [row for row in results if not row.get("invalid")]
    if not usable:
        return (
            "No review produced a usable score. The joins between the retrieved set and "
            "the labelled records failed, so there is no number to put in an application."
        )
    n_reviews = len(usable)
    smallest = min(row["N"] for row in usable)
    largest = max(row["N"] for row in usable)
    scope = "one systematic review" if n_reviews == 1 else f"{n_reviews} systematic reviews"
    size_bit = f"{smallest:,} records" if smallest == largest else f"{smallest:,} to {largest:,} records"

    recalls = [
        (row["review_key"], row["hits_and_maybe"]["true_recall"])
        for row in usable
        if row["hits_and_maybe"]["true_recall"] is not None
    ]
    weak = [(key, value) for key, value in recalls if value < 0.5]
    strong = [(key, value) for key, value in recalls if value >= 0.5]
    if strong and weak:
        parts = [f"{key.replace('_', ' ')} ({_pct(value)})" for key, value in weak]
        if len(parts) == 1:
            weak_bits = parts[0]
        elif len(parts) == 2:
            weak_bits = f"{parts[0]} and {parts[1]}"
        else:
            weak_bits = ", ".join(parts[:-1]) + f", and {parts[-1]}"
        recall_sentence = (
            f"On {len(strong)} of them the search found between {_pct(min(value for _, value in strong))} "
            f"and {_pct(max(value for _, value in strong))} of the records human reviewers had included; "
            f"on {weak_bits} it found far fewer, because a confirmed hit in this method is a verified name "
            f"and those questions produced none."
        )
    elif recalls:
        lo = min(value for _, value in recalls)
        hi = max(value for _, value in recalls)
        if hi - lo < 1e-9:
            recall_sentence = f"The search found {_pct(lo)} of the records human reviewers had included."
        else:
            recall_sentence = (
                f"The search found between {_pct(lo)} and {_pct(hi)} of the records human reviewers had included."
            )
    else:
        recall_sentence = "True recall could not be computed."

    sample_sizes = sorted({
        row["hits_and_maybe"].get("observed_sample_size")
        for row in usable
        if row["hits_and_maybe"].get("observed_sample_size")
    })
    drawn = str(sample_sizes[0]) if len(sample_sizes) == 1 else " or ".join(str(n) for n in sample_sizes)

    blind = None
    for row in usable:
        primary = row["hits_and_maybe"]
        sim = primary.get("simulation") or {}
        rate = primary.get("true_elusion_rate") or 0
        zero = sim.get("proportion_zero_misses")
        if rate > 0 and zero is not None and (blind is None or zero > blind[0]):
            blind = (
                zero,
                row["review_key"],
                rate,
                primary.get("relevant_outside"),
                primary.get("outside"),
            )
    if blind and blind[0] >= 0.5:
        zero, key, rate, missed, outside = blind
        blind_bit = (
            f"on {key.replace('_', ' ')}, {missed} included "
            f"{'record was' if missed == 1 else 'records were'} left among {outside:,} not retrieved "
            f"(a miss rate of {_pct(rate)}), and redrawing that sample ten thousand times found none of them "
            f"in {_pct(zero)} of draws"
        )
    elif blind:
        blind_bit = (
            f"redrawing the sample of {drawn}, the share of draws that found nothing "
            f"reached {_pct(blind[0])} on {blind[1].replace('_', ' ')}"
        )
    else:
        blind_bit = ""

    required = [
        row["hits_and_maybe"]["required_sample_size"]
        for row in usable
        if row["hits_and_maybe"].get("required_sample_size")
    ]
    if required:
        req_lo, req_hi = min(required), max(required)
        need = f"{req_lo:,} records" if req_lo == req_hi else f"{req_lo:,} to {req_hi:,} records"
        need_sentence = (
            f"Estimating a review's miss rate to within 20 percent of its true value, 19 times out of 20, "
            f"would take a sample of {need} on these corpora, not the {drawn} the product draws."
        )
    else:
        need_sentence = (
            f"A sample size for a 20 percent estimate is not defined here, because the true miss rate was zero. "
            f"The product drew {drawn}."
        )

    gaps = []
    for row in usable:
        primary = row["hits_and_maybe"]
        err = primary.get("calibration_error")
        if err is not None:
            gaps.append((err, row["review_key"], primary.get("observed_elusion_estimate"), primary.get("true_elusion_rate")))
    if gaps:
        err, key, observed, true = max(gaps)
        gap_bit = (
            f"on the one sample actually drawn, the largest gap was {100 * err:.0f} percentage points "
            f"({key.replace('_', ' ')}: the sample said {_pct(observed)}, the truth was {_pct(true)})"
        )
    else:
        gap_bit = ""

    bits = [bit for bit in (gap_bit, blind_bit) if bit]
    sample_sentence = f"The check on what was missed uses a sample of {drawn}"
    sample_sentence += (": " + "; ".join(bits) + ".") if bits else "."

    opening = (
        f"We checked the retrieval-assurance method on {scope} from SYNERGY "
        f"({size_bit}), where every record is already labelled included or not, "
        f"using each review's title as the question because this version of SYNERGY does not ship one. "
        f"{recall_sentence} {sample_sentence} {need_sentence}"
    )
    return " ".join(opening.split())


def write_summary(results: list[dict], results_dir: Path = RESULTS) -> None:
    results = sorted(results, key=lambda row: row["N"])
    lines = [
        "# SYNERGY retrieval-assurance evaluation",
        "",
        "Retrieved set for coverage, elusion and the required sample size is hits and maybes, "
        "which is the set the methodology says it is aiming to recall and the pool its elusion "
        "sample is drawn from. True recall is also shown for hits alone. "
        "The required sample size is the smallest elusion sample whose 95% interval lies within "
        "±20% of the true elusion rate (sampling without replacement). "
        "That size is not capped at 5,000: the sweep stops there, and a larger figure means "
        "5,000 was not enough. "
        "A dash means the rate was zero or undefined.",
        "",
        "| review | N | included | coverage (hits+maybe) | coverage (hits) | true recall (hits) | true recall (hits+maybe) | true elusion rate | our estimate | calibration error | required sample size |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in results:
        primary = row["hits_and_maybe"]
        hits = row["hits"]
        lines.append(
            "| {review} | {N} | {R} | {cov} | {cov_h} | {rec_h} | {rec} | {elusion} | {est} | {cal} | {need} |".format(
                review=row["review_key"],
                N=row["N"],
                R=row["R"],
                cov=_fmt(primary["coverage"]),
                cov_h=_fmt(hits["coverage"]),
                rec_h=_fmt(hits["true_recall"]),
                rec=_fmt(primary["true_recall"]),
                elusion=_fmt(primary["true_elusion_rate"]),
                est=_fmt(primary.get("observed_elusion_estimate")),
                cal=_fmt(primary.get("calibration_error")),
                need=primary.get("required_sample_size") if primary.get("required_sample_size") is not None else "—",
            )
        )
    lines.extend(["", prose(results), "", WHAT_THIS_DOES_NOT_SHOW, ""])
    flagged = [(row["review_key"], row["warnings"]) for row in results if row["warnings"]]
    if flagged:
        lines.append("Warnings that bear on whether a number is trustworthy:")
        lines.append("")
        for key, warnings in flagged:
            for warning in warnings:
                lines.append(f"- {key}: {warning}")
        lines.append("")
    path = results_dir / "summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {path}")


def self_check() -> None:
    # Brute-force a small hypergeometric against the log-gamma pmf.
    from math import comb

    pool, good, n = 10, 4, 5
    k_min, probs = draw_distribution(pool, good, n)
    for offset, prob in enumerate(probs):
        k = k_min + offset
        expected = comb(good, k) * comb(pool - good, n - k) / comb(pool, n)
        if abs(prob - expected) > 1e-9:
            raise SystemExit(f"pmf mismatch at k={k}: {prob} != {expected}")
    if abs(sum(probs) - 1) > 1e-9:
        raise SystemExit("pmf did not sum to 1")
    # n=5 cannot put a 95% interval inside ±20% of 0.4; a census can.
    if within_relative(pool, good, 5):
        raise SystemExit("small sample was treated as precise enough")
    if not within_relative(pool, good, pool):
        raise SystemExit("a full census was not treated as exact")
    need = minimum_sample_size(pool, good)
    if need is None or not within_relative(pool, good, need):
        raise SystemExit(f"minimum n {need} does not meet the criterion")
    if need > 1 and within_relative(pool, good, need - 1):
        raise SystemExit(f"minimum n {need} is not minimal")
    if minimum_sample_size(100, 0) is not None:
        raise SystemExit("zero rate should not have a relative-error sample size")
    # A rare rate needs a large sample. p=0.02, margin 0.004, infinite-pop n is about 4,700.
    rare = minimum_sample_size(50_000, 1_000)
    if rare is None or not (1_000 <= rare <= 8_000):
        raise SystemExit(f"rare-rate sample size {rare} is outside the expected band")
    print(f"self-check ok (toy minimum n={need}, rare-rate minimum n={rare})")


def discover(results_dir: Path = RESULTS) -> list[str]:
    keys = []
    if not results_dir.exists():
        return keys
    for path in sorted(results_dir.iterdir()):
        if (path / "scope_members.csv").exists() and (path / "methodology.json").exists() and (path / "elusion_sample.csv").exists():
            keys.append(path.name)
    return keys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", action="append", default=[], help="Score this review; repeatable")
    parser.add_argument("--all", action="store_true", help="Score every review that has a retrieval export")
    parser.add_argument("--results", default="", help="Subdirectory of results/ to read and write, e.g. asked")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    results_dir = RESULTS / args.results if args.results else RESULTS
    if args.self_check:
        self_check()
        if not args.review and not args.all:
            return
    keys = discover(results_dir) if args.all else list(args.review)
    if not keys:
        raise SystemExit("name a review, or pass --all")
    results = [score_review(key, results_dir) for key in keys]
    write_summary(results, results_dir)
    if any(row.get("invalid") for row in results):
        raise SystemExit("one or more reviews failed the join; see warnings above")


if __name__ == "__main__":
    main()
