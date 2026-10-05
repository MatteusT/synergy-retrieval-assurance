"""Download classic SYNERGY and write one ingest file per record.

The product accepts ``.txt`` (``shared/content_kind.py`` TEXT_EXTENSIONS, which
is part of INGEST_EXTENSIONS). Archives are not on that list.

Classic SYNERGY (version 1.0, 26 reviews) is selected with SYNERGY_SET=synergy.
The installed package defaults to SYNERGY+, whose iterator drops records that
are not open-access or whose abstract is shorter than its minimum. That would
silently remove the empty abstracts this evaluation has to keep.

Plaintext abstracts are reconstructed locally for ingest and gitignored.
SYNERGY's own notice says they must not be published again as plaintext.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from pathlib import Path

# Read before synergy_dataset imports, which bind SYNERGY_SET at import time.
os.environ["SYNERGY_SET"] = "synergy"
os.environ["SYNERGY_VERSION"] = "1.0"

from synergy_dataset import Dataset, iter_datasets  # noqa: E402
from synergy_dataset.base import _ensure_dataset_downloaded  # noqa: E402

ROOT = Path(__file__).resolve().parent
CORPORA = ROOT / "corpora"
CATALOGUE = ROOT / "data" / "reviews.csv"

# OpenAlex work id, the join key encoded in the filename.
WORK_ID = re.compile(r"(W\d+)$", re.I)


def work_token(openalex_id: str) -> str:
    match = WORK_ID.search(openalex_id or "")
    if not match:
        raise SystemExit(f"OpenAlex id {openalex_id!r} has no W-number; refusing to invent a join key")
    return match.group(1)


def filename_for(review_key: str, openalex_id: str) -> str:
    return f"SYN-{review_key}-{work_token(openalex_id)}.txt"


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return "" if text == "nan" else text


def _authors(names) -> str:
    if names is None or isinstance(names, float):
        return ""
    if isinstance(names, str):
        return "" if names.strip() == "nan" else names.strip()
    return "; ".join(part for part in (_cell(name) for name in names) if part)


def record_text(*, title: str, authors: str, year: str, doi: str, abstract: str) -> str:
    abstract_body = abstract.strip() if abstract else ""
    return (
        f"Title: {title}\n"
        f"Authors: {authors}\n"
        f"Year: {year}\n"
        f"DOI: {doi}\n"
        f"\n"
        f"Abstract:\n"
        f"{abstract_body}\n"
    )


def catalogue() -> list[dict]:
    """Every classic review, from the dataset metadata, without dropping records."""
    _ensure_dataset_downloaded()
    rows = []
    for dataset in iter_datasets(min_inclusions=None):
        data = dataset.metadata.get("data") or {}
        publication = dataset.metadata.get("publication") or {}
        n_records = int(data.get("n_records") or 0)
        n_included = int(data.get("n_records_included") or 0)
        # counts walks labels.csv for classic SYNERGY and must agree with metadata.
        counted_records, counted_included = dataset.counts
        if counted_records != n_records or counted_included != n_included:
            print(
                f"WARNING {dataset.name}: metadata says {n_records} records / {n_included} included, "
                f"labels.csv says {counted_records} / {counted_included}",
                file=sys.stderr,
            )
        title = publication.get("title") or publication.get("display_name") or ""
        rows.append(
            {
                "review_key": dataset.metadata.get("key") or dataset.name,
                "n_records": counted_records,
                "n_included": counted_included,
                "title": _cell(title),
                "inclusion_rate": round(counted_included / counted_records, 6) if counted_records else 0,
            }
        )
    rows.sort(key=lambda row: (row["n_records"], row["review_key"]))
    return rows


def write_catalogue(rows: list[dict]) -> None:
    CATALOGUE.parent.mkdir(parents=True, exist_ok=True)
    with CATALOGUE.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["review_key", "n_records", "n_included", "inclusion_rate", "title"],
        )
        writer.writeheader()
        writer.writerows(rows)
    total_n = sum(row["n_records"] for row in rows)
    total_r = sum(row["n_included"] for row in rows)
    print(f"{len(rows)} reviews, {total_n} records, {total_r} included "
          f"({(total_r / total_n) if total_n else 0:.4%})")
    print(f"{'review':<28} {'N':>8} {'included':>10}")
    for row in rows:
        print(f"{row['review_key']:<28} {row['n_records']:>8} {row['n_included']:>10}")
    print(f"catalogue: {CATALOGUE}")


def question_from_metadata(dataset: Dataset) -> dict:
    """One rule for every review. Record where the question came from."""
    publication = dataset.metadata.get("publication") or {}
    title = _cell(publication.get("title") or publication.get("display_name") or dataset.name)
    criteria = publication.get("eligibility_criteria")
    if isinstance(criteria, list):
        criteria_text = "\n".join(_cell(item) for item in criteria if _cell(item))
    else:
        criteria_text = _cell(criteria)
    if criteria_text:
        return {
            "question": criteria_text,
            "source": "publication.eligibility_criteria",
            "review_title": title,
            "reconstructed": False,
        }
    # Classic SYNERGY 1.0 stores no eligibility criteria. metadata.json's
    # publication block is a DOI; the OpenAlex work beside it is the review
    # paper, not a research-question field. The spec's fallback is the title.
    return {
        "question": f"Studies relevant to the systematic review: {title}",
        "source": "reconstructed_from_title",
        "review_title": title,
        "reconstructed": True,
        "note": (
            "Classic SYNERGY 1.0 has no research-question or eligibility_criteria field. "
            "The question is the review paper's title, used for every such review."
        ),
    }


def prepare_review(review_key: str) -> Path:
    _ensure_dataset_downloaded()
    dataset = Dataset(review_key)
    records = dataset.to_dict(vars=["title", "abstract_original", "publication_year", "author_names"])
    out = CORPORA / review_key
    files = out / "files"
    files.mkdir(parents=True, exist_ok=True)

    question = question_from_metadata(dataset)
    (out / "question.json").write_text(json.dumps(question, indent=2), encoding="utf-8")

    empty = 0
    seen_names: set[str] = set()
    manifest_path = out / "manifest.csv"
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["record_id", "filename", "label_included", "title", "doi", "abstract_empty"],
        )
        writer.writeheader()
        if not records:
            raise SystemExit(f"{review_key}: to_dict() returned no records")
        included = 0
        for openalex_id, row in records.items():
            if row is None:
                raise SystemExit(
                    f"{review_key}: {openalex_id} is in labels.csv but has no work record. "
                    "Refusing to drop it silently."
                )
            record_id = str(openalex_id)
            name = filename_for(review_key, record_id)
            if name in seen_names:
                raise SystemExit(f"duplicate filename {name} in {review_key}")
            seen_names.add(name)
            title = _cell(row.get("title"))
            doi = _cell(row.get("doi"))
            year = _cell(row.get("publication_year"))
            authors = _authors(row.get("author_names"))
            abstract_raw = row.get("abstract_original")
            abstract = "" if abstract_raw is None or str(abstract_raw) == "nan" else str(abstract_raw)
            is_empty = not abstract.strip()
            if is_empty:
                empty += 1
            label = int(row["label_included"])
            included += label
            (files / name).write_text(
                record_text(title=title, authors=authors, year=year, doi=doi, abstract=abstract),
                encoding="utf-8",
            )
            writer.writerow(
                {
                    "record_id": record_id,
                    "filename": name,
                    "label_included": label,
                    "title": title,
                    "doi": doi,
                    "abstract_empty": int(is_empty),
                }
            )

    n = len(seen_names)
    label_n = len(dataset.labels)
    if n != label_n:
        raise SystemExit(
            f"{review_key}: wrote {n} files but labels.csv has {label_n} records. "
            "A shortfall here would invalidate every number downstream."
        )
    if n and empty == n:
        # The package's default `abstract` field reads abstract_inverted_index_cleaned,
        # which classic SYNERGY 1.0 does not have. All-empty is that bug, not the corpus.
        sample_raw = 0
        for work, _label in dataset.iter():
            if work.get("abstract_inverted_index"):
                sample_raw += 1
            break
        if sample_raw:
            raise SystemExit(
                f"{review_key}: every written abstract is empty, but the raw works "
                "have abstract_inverted_index. The files would not contain the text "
                "the labels were screened on."
            )
    print(f"{review_key}: {n} files, {included} included, {empty} empty abstracts")
    print(f"question source: {question['source']}")
    print(f"manifest: {manifest_path}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", help="Write files for this review key")
    parser.add_argument("--smallest", action="store_true", help="Write files for the smallest review")
    parser.add_argument("--list-only", action="store_true", help="List reviews and stop")
    args = parser.parse_args()

    rows = catalogue()
    write_catalogue(rows)
    if not rows:
        raise SystemExit("SYNERGY returned no reviews")
    if args.list_only:
        return
    if args.review and args.smallest:
        raise SystemExit("pass either --review or --smallest")
    key = args.review or rows[0]["review_key"]
    prepare_review(key)


if __name__ == "__main__":
    main()
