"""Plot how the elusion estimate's 95% interval closes on the true rate.

The mean of the estimate equals the true rate at every sample size, because a
sample drawn without replacement is unbiased. What changes with sample size is
the interval, and how often a draw finds nothing. The four reviews here are the
title-based runs whose search found most of the included records.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from score import draw_distribution

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
REVIEWS = ("Donners_2021", "Nelson_2002", "Muthu_2021", "Hall_2012")


def median_estimate(pool: int, good: int, n: int) -> float:
    if n >= pool:
        return good / pool if pool else 0.0
    k_min, probs = draw_distribution(pool, good, n)
    cdf = 0.0
    for offset, prob in enumerate(probs):
        cdf += prob
        if cdf >= 0.5:
            return (k_min + offset) / n
    return (k_min + len(probs) - 1) / n


def load(review: str) -> dict:
    score = json.loads((RESULTS / review / "score.json").read_text(encoding="utf-8"))
    primary = score["hits_and_maybe"]
    rows = list(csv.DictReader((RESULTS / review / "sample_size_sweep.csv").open(encoding="utf-8")))
    pool = int(primary["outside"])
    good = int(primary["relevant_outside"])
    points = []
    for row in rows:
        n = int(row["n"])
        points.append(
            {
                "n": n,
                "low": float(row["interval_low"]),
                "high": float(row["interval_high"]),
                "median": median_estimate(pool, good, n),
                "true_rate": float(row["true_rate"]),
                "p_zero": None if row["p_zero_misses"] in ("", "None") else float(row["p_zero_misses"]),
            }
        )
    return {
        "review": review,
        "true_rate": primary["true_elusion_rate"],
        "required": primary.get("required_sample_size"),
        "outside": pool,
        "relevant_outside": good,
        "points": points,
    }


def svg(series: list[dict]) -> str:
    width, row_h, pad_l, pad_r, pad_t = 860, 210, 64, 24, 28
    height = 36 + len(series) * row_h
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fafafa"/>',
        '<text x="64" y="22" font-family="Georgia, serif" font-size="16" fill="#1c1d21">Elusion estimate versus sample size</text>',
    ]
    plot_w = width - pad_l - pad_r
    for index, item in enumerate(series):
        top = 40 + index * row_h
        points = item["points"]
        n_max = max(point["n"] for point in points)
        y_max = max(max(point["high"] for point in points), item["true_rate"] or 0, 0.01) * 1.08
        plot_h = row_h - pad_t - 36

        def x_of(n: int) -> float:
            return pad_l + (n / n_max) * plot_w

        def y_of(rate: float) -> float:
            return top + pad_t + (1 - rate / y_max) * plot_h

        band = " ".join(
            f"{x_of(point['n']):.1f},{y_of(point['high']):.1f}" for point in points
        ) + " " + " ".join(
            f"{x_of(point['n']):.1f},{y_of(point['low']):.1f}" for point in reversed(points)
        )
        median = " ".join(f"{x_of(point['n']):.1f},{y_of(point['median']):.1f}" for point in points)
        truth_y = y_of(item["true_rate"] or 0)
        parts.append(f'<text x="{pad_l}" y="{top + 14}" font-family="sans-serif" font-size="12" fill="#1c1d21">{item["review"].replace("_", " ")}</text>')
        parts.append(f'<line x1="{pad_l}" y1="{top + pad_t + plot_h}" x2="{pad_l + plot_w}" y2="{top + pad_t + plot_h}" stroke="#ccc"/>')
        parts.append(f'<polygon points="{band}" fill="#d9d3c5" stroke="none"/>')
        parts.append(f'<polyline points="{median}" fill="none" stroke="#1c1d21" stroke-width="1.4"/>')
        parts.append(f'<line x1="{pad_l}" y1="{truth_y:.1f}" x2="{pad_l + plot_w}" y2="{truth_y:.1f}" stroke="#8a5a2a" stroke-dasharray="4 3"/>')
        mark = x_of(20) if n_max >= 20 else x_of(points[0]["n"])
        parts.append(f'<line x1="{mark:.1f}" y1="{top + pad_t}" x2="{mark:.1f}" y2="{top + pad_t + plot_h}" stroke="#888" stroke-dasharray="2 3"/>')
        parts.append(
            f'<text x="{pad_l}" y="{top + row_h - 8}" font-family="sans-serif" font-size="11" fill="#555">'
            f'sample size, up to {n_max} · dashed vertical line is 20 · brown line is the true rate · black line is the median estimate · band is the 95% interval'
            f"</text>"
        )
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    series = [load(review) for review in REVIEWS]
    (RESULTS / "convergence.svg").write_text(svg(series), encoding="utf-8")
    compact = []
    for item in series:
        step_points = []
        wanted = {10, 20, 30, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 2500, 3000, 4000, 5000}
        if item["required"]:
            wanted.add(int(item["required"]))
        points = item["points"]
        for point in points:
            if point["n"] in wanted or point["n"] == points[-1]["n"]:
                step_points.append(point)
        if points[0] not in step_points:
            step_points.insert(0, points[0])
        compact.append({**item, "points": step_points})
    (RESULTS / "convergence.json").write_text(json.dumps(compact), encoding="utf-8")
    print(f"wrote {RESULTS / 'convergence.svg'}")


if __name__ == "__main__":
    main()
