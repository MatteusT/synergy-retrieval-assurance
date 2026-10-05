"""Upload one SYNERGY review through the normal presign, S3, mark-uploaded path.

One case per review, named ``SYNERGY <review key>``. Bulk mark-uploaded is the
path the API already offers. The ingest-queue guard is 20,000; this waits
rather than blowing through it.

Fails if the case does not end up with exactly as many documents as the
manifest, if any document failed extraction, or if dedup hid any of them.
A shortfall would make every later number meaningless.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from pathlib import Path

import requests

from stack import Stack, StackError

ROOT = Path(__file__).resolve().parent
CORPORA = ROOT / "corpora"
RESULTS = ROOT / "results"
BATCH = 25
POLL_SECONDS = 20


def manifest_rows(review_key: str) -> list[dict]:
    path = CORPORA / review_key / "manifest.csv"
    if not path.exists():
        raise SystemExit(f"no manifest at {path}; run prepare.py first")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def case_name(review_key: str) -> str:
    return f"SYNERGY {review_key}"


def find_case(stack: Stack, review_key: str) -> dict | None:
    wanted = case_name(review_key)
    cases = stack.get("/cases")
    matches = [row for row in cases if row.get("name") == wanted]
    if len(matches) > 1:
        raise SystemExit(f"more than one case named {wanted!r}; refusing to guess")
    return matches[0] if matches else None


def ensure_case(stack: Stack, review_key: str) -> str:
    existing = find_case(stack, review_key)
    if existing:
        print(f"case already exists: {existing['case_id']} {existing['name']}")
        return existing["case_id"]
    created = stack.post(
        "/cases",
        {"name": case_name(review_key), "citation_mode": "precise"},
    )
    print(f"created case {created['case_id']} {created['name']}")
    return created["case_id"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def existing_filenames(stack: Stack, case_id: str) -> set[str]:
    """Filenames already in the workspace, so a resumed upload does not send them again."""
    names: set[str] = set()
    offset = 0
    total = None
    while True:
        page = stack.get(
            f"/cases/{case_id}/documents/table",
            params={"limit": 500, "offset": offset},
        )
        members = page.get("members") or []
        total = int(page.get("document_count") or 0)
        for row in members:
            if row.get("filename"):
                names.add(str(row["filename"]))
        offset += len(members)
        if not members or offset >= total:
            break
    if total is not None and len(names) != total:
        print(
            f"  warning: listed {len(names)} filenames, workspace reports {total}",
            flush=True,
        )
    return names


def upload_all(stack: Stack, case_id: str, review_key: str, rows: list[dict]) -> None:
    files_dir = CORPORA / review_key / "files"
    stack.post("/upload/init", {"case_id": case_id})

    def mark(batch: list[dict]) -> None:
        stack.wait_for_queue(case_id, len(batch))
        sleep = 10.0
        for _ in range(10):
            try:
                response = stack.session.post(
                    f"{stack.base}/upload/mark-uploaded/bulk",
                    json={"case_id": case_id, "files": batch},
                    timeout=stack.timeout,
                )
            except requests.RequestException as exc:
                print(f"  bulk mark failed ({exc.__class__.__name__}); sleeping {sleep:.0f}s", flush=True)
                time.sleep(sleep)
                sleep = min(sleep * 2, 300)
                continue
            if response.status_code == 429:
                print(f"  queue saturated (429); sleeping {sleep:.0f}s", flush=True)
                time.sleep(sleep)
                sleep = min(sleep * 2, 300)
                stack.wait_for_queue(case_id, len(batch))
                continue
            if response.status_code != 200:
                raise StackError(f"bulk mark -> {response.status_code}: {response.text[:800]}")
            result = response.json()
            renamed = result.get("renamed_files") or []
            if renamed:
                raise SystemExit(
                    "upload renamed files, which would break the record-id join key: "
                    + json.dumps(renamed[:5])
                )
            skipped = result.get("skipped_files") or []
            print(
                f"  marked queued={result.get('queued')} skipped={len(skipped)} of {len(batch)}",
                flush=True,
            )
            if skipped:
                reasons = {}
                for item in skipped:
                    reasons[str(item.get("reason"))] = reasons.get(str(item.get("reason")), 0) + 1
                print(f"  skip reasons: {reasons}", flush=True)
            return
        raise SystemExit("bulk mark-uploaded kept failing")

    already = existing_filenames(stack, case_id)
    todo = [row for row in rows if row["filename"] not in already]
    print(f"  {len(already)} already in the case; {len(todo)} still to upload", flush=True)
    pending: list[dict] = []
    for index, row in enumerate(todo, start=1):
        path = files_dir / row["filename"]
        if not path.exists():
            raise SystemExit(f"missing file {path}")
        body = path.read_bytes()
        presign = stack.post("/upload/presign", {"case_id": case_id, "filename": row["filename"]})
        put_error: Exception | None = None
        put = None
        for attempt in range(6):
            try:
                put = stack.session.put(presign["upload_url"], data=body, timeout=120)
            except requests.RequestException as exc:
                put_error = exc
                time.sleep(min(30, 2 ** attempt))
                continue
            if put.status_code in (200, 204):
                put_error = None
                break
            if put.status_code >= 500:
                time.sleep(min(30, 2 ** attempt))
                continue
            raise StackError(f"S3 PUT {row['filename']} -> {put.status_code}: {put.text[:400]}")
        if put_error is not None or put is None or put.status_code not in (200, 204):
            raise StackError(f"S3 PUT {row['filename']} failed: {put_error}")
        pending.append(
            {
                "filename": row["filename"],
                "s3_key": presign["key"],
                "metadata": {},
                "content_sha256": sha256(body),
            }
        )
        if len(pending) >= BATCH:
            mark(pending)
            pending = []
            print(f"  uploaded {index}/{len(todo)}", flush=True)
    if pending:
        mark(pending)
    print(f"  uploaded {len(todo)}/{len(todo)}", flush=True)


def _phase2_started(summary: dict) -> bool:
    if summary.get("organising"):
        return True
    if summary.get("ontology_status"):
        return True
    if int(summary.get("ontology_batches_total") or 0) > 0:
        return True
    if int(summary.get("classified") or 0) > 0:
        return True
    return False


def _phase2_finished(summary: dict, started: bool) -> bool:
    if not started:
        return False
    if summary.get("organising"):
        return False
    if summary.get("ontology_status"):
        return False
    if int(summary.get("remaining") or 0) != 0:
        return False
    if int(summary.get("pending_entity") or 0) != 0:
        return False
    if summary.get("blocked_on"):
        return False
    # Finalise deletes the phase-2 state key, so a finished run reports no batches.
    return int(summary.get("ontology_batches_total") or 0) == 0


def poll_until_done(stack: Stack, case_id: str, expected: int, timeout_s: float) -> dict:
    deadline = time.monotonic() + timeout_s
    started = False
    ingest_ready_at: float | None = None
    last = {}
    while time.monotonic() < deadline:
        summary = stack.get(f"/cases/{case_id}/progress/summary")
        last = summary
        started = started or _phase2_started(summary)
        total = int(summary.get("total") or 0)
        print(
            "  progress "
            f"total={total} queued={summary.get('queued')} parsing={summary.get('parsing')} "
            f"extracted={summary.get('extracted')} embedded={summary.get('embedded')} "
            f"classified={summary.get('classified')} failed={summary.get('failed')} "
            f"pending_entity={summary.get('pending_entity')} "
            f"phase2={summary.get('ontology_status')} organising={summary.get('organising')} "
            f"blocked_on={summary.get('blocked_on')}",
            flush=True,
        )
        if total > expected:
            raise SystemExit(f"case has {total} documents, manifest has {expected}")
        ingest_settled = (
            total == expected
            and int(summary.get("remaining") or 0) == 0
            and int(summary.get("pending_entity") or 0) == 0
            and not summary.get("blocked_on")
        )
        if ingest_settled and ingest_ready_at is None:
            ingest_ready_at = time.monotonic()
            print("  ingest settled; waiting for phase 2", flush=True)
        if _phase2_finished(summary, started) and total == expected:
            # One extra poll so a lock that flickers open is not taken as done.
            time.sleep(POLL_SECONDS)
            confirm = stack.get(f"/cases/{case_id}/progress/summary")
            if _phase2_finished(confirm, True) and int(confirm.get("total") or 0) == expected:
                return confirm
            summary = confirm
            started = True
        if ingest_ready_at and not started and (time.monotonic() - ingest_ready_at) > 30 * 60:
            raise SystemExit(
                "phase 2 did not start within 30 minutes of ingest settling: "
                + json.dumps({k: summary.get(k) for k in (
                    "total", "embedded", "classified", "failed", "pending_entity",
                    "blocked_on", "ready_for_phase2", "ontology_status", "organising",
                )})
            )
        time.sleep(POLL_SECONDS)
    raise SystemExit("timed out waiting for ingest and phase 2: " + json.dumps(last)[:800])


def assert_counts(stack: Stack, case_id: str, expected: int, summary: dict) -> None:
    total = int(summary.get("total") or 0)
    failed = int(summary.get("failed") or 0)
    visible = stack.get(f"/cases/{case_id}/documents/table", params={"limit": 1})
    visible_n = int(visible.get("document_count") or 0)
    print(f"documents in case: {total}; visible (not hidden as duplicates): {visible_n}; manifest: {expected}")
    if total != expected:
        raise SystemExit(
            f"DOCUMENT COUNT MISMATCH: case has {total}, manifest has {expected}. "
            "Stopping. A shortfall invalidates every number downstream."
        )
    if visible_n != expected:
        raise SystemExit(
            f"VISIBLE COUNT MISMATCH: the workspace shows {visible_n} of {expected} documents. "
            "Dedup hid some. The elusion pool would no longer be the labelled corpus."
        )
    if failed:
        raise SystemExit(
            f"{failed} documents failed ingestion. Their text is not searchable, "
            "so a recall number from this case would be wrong."
        )
    classified = int(summary.get("classified") or 0)
    embedded = int(summary.get("embedded") or 0)
    if classified + failed != total:
        print(
            f"WARNING: phase 2 left {embedded} documents embedded and {classified} classified "
            f"out of {total}. Retrieval can still run on embedded text; classification did not finish for every document.",
            file=sys.stderr,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True)
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--timeout-hours", type=float, default=8.0)
    parser.add_argument("--skip-upload", action="store_true", help="Only poll a case that is already uploading")
    args = parser.parse_args()

    rows = manifest_rows(args.review)
    if not rows:
        raise SystemExit("manifest is empty")
    stack = Stack(args.api_base)
    case_id = ensure_case(stack, args.review)
    out = RESULTS / args.review
    out.mkdir(parents=True, exist_ok=True)
    (out / "case.json").write_text(
        json.dumps({"case_id": case_id, "review_key": args.review, "name": case_name(args.review)}, indent=2),
        encoding="utf-8",
    )
    if not args.skip_upload:
        upload_all(stack, case_id, args.review, rows)
    summary = poll_until_done(stack, case_id, len(rows), args.timeout_hours * 3600)
    assert_counts(stack, case_id, len(rows), summary)
    (out / "progress.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"ingest complete for {args.review}: {len(rows)} documents")


if __name__ == "__main__":
    main()
