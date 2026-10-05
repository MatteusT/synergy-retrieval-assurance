"""One retrieval run per review, through the product's hunt path.

The same query template is used for every review. Nothing is rewritten to
retrieve more. The question text is whatever prepare.py recorded, and that
source is copied into the output so it can be audited.

Exports the set the hunt built, the methodology payload, and the elusion
sample the review screen draws from list_reject_sample().
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from stack import Stack, StackError

ROOT = Path(__file__).resolve().parent
CORPORA = ROOT / "corpora"
RESULTS = ROOT / "results"

# Recorded, and identical for every review. "Find" and "all documents" are
# hunt hints in the product; mode=hunt is the Build a set switch. top_k is
# the API default.
STRATEGY = {
    "name": "fixed-hunt-v1",
    "endpoint": "GET /research/stream",
    "mode": "hunt",
    "top_k": 35,
    "template": "Find all documents relevant to: {question}",
}


def load_json(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"missing {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_sse(response) -> list[dict]:
    events: list[dict] = []
    buffer = ""
    for chunk in response.iter_content(chunk_size=None, decode_unicode=True):
        if not chunk:
            continue
        buffer += chunk
        while "\n\n" in buffer:
            raw, buffer = buffer.split("\n\n", 1)
            data_lines = []
            for line in raw.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            payload = json.loads("\n".join(data_lines))
            events.append(payload)
            kind = payload.get("type")
            if kind == "error":
                raise StackError(f"research stream error: {payload.get('message')}")
            if kind == "done":
                return events
            if kind == "intent":
                print(f"  intent action={payload.get('action')} summary={payload.get('summary')}", flush=True)
            elif kind == "status":
                print(f"  {payload.get('message')}", flush=True)
    if events and events[-1].get("type") == "done":
        return events
    raise StackError("research stream ended without a done event")


def hunt(stack: Stack, case_id: str, review_key: str, question: str) -> list[dict]:
    session = stack.post(
        "/research/sessions",
        {"case_id": case_id, "title": f"SYNERGY {review_key}"},
    )
    session_id = session["session_id"]
    query = STRATEGY["template"].format(question=question)
    print(f"  session {session_id}", flush=True)
    print(f"  query length {len(query)} characters", flush=True)
    response = stack.session.get(
        f"{stack.base}/research/stream",
        params={
            "case_id": case_id,
            "session_id": session_id,
            "query": query,
            "mode": STRATEGY["mode"],
            "top_k": STRATEGY["top_k"],
        },
        stream=True,
        timeout=3600,
    )
    if response.status_code != 200:
        raise StackError(f"research stream -> {response.status_code}: {response.text[:800]}")
    events = parse_sse(response)
    intents = [event for event in events if event.get("type") == "intent"]
    if not intents or intents[-1].get("action") != "hunt":
        action = intents[-1].get("action") if intents else None
        raise SystemExit(
            f"the turn did not build a set (action={action}). "
            "A number from an answer that did not hunt would not be the method under test."
        )
    return events, session_id


def latest_scope(stack: Stack, case_id: str, session_id: str | None = None) -> dict:
    payload = stack.get(f"/matters/{case_id}/scopes")
    scopes = payload.get("scopes") or []
    searches = [scope for scope in scopes if scope.get("source_type") == "ask_search"]
    if session_id:
        matched = [scope for scope in searches if scope.get("session_id") == session_id]
        if matched:
            searches = matched
    if not searches:
        raise SystemExit("hunt finished but the case has no ask_search scope")
    searches.sort(key=lambda scope: str(scope.get("updated_at") or scope.get("created_at") or ""))
    return searches[-1]


def all_members(stack: Stack, case_id: str, scope_id: str) -> list[dict]:
    collected: list[dict] = []
    offset = 0
    limit = 2000
    total = None
    while True:
        page = stack.get(
            f"/matters/{case_id}/scopes/{scope_id}/members",
            params={"offset": offset, "limit": limit},
        )
        members = page.get("members") or []
        total = int(page.get("document_count") or 0)
        collected.extend(members)
        offset += len(members)
        if not members or offset >= total:
            break
    if total is not None and len(collected) != total:
        raise SystemExit(f"scope member pages returned {len(collected)} of {total}")
    return collected


def write_members(path: Path, members: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["filename", "band", "score", "reason"])
        writer.writeheader()
        for row in members:
            writer.writerow(
                {
                    "filename": row.get("filename") or "",
                    "band": row.get("band") or "",
                    "score": "" if row.get("score") is None else row.get("score"),
                    "reason": (row.get("reason") or "").replace("\n", " ").replace("\r", " "),
                }
            )


def write_elusion(path: Path, nots: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["filename", "doc_id", "reason"])
        writer.writeheader()
        for row in nots:
            writer.writerow(
                {
                    "filename": row.get("filename") or "",
                    "doc_id": row.get("doc_id") or "",
                    "reason": (row.get("reason") or "").replace("\n", " ").replace("\r", " "),
                }
            )


def question_for(review_key: str, asked: bool) -> tuple[dict, str]:
    if not asked:
        info = load_json(CORPORA / review_key / "question.json")
        return info, str(info.get("question") or "").strip()
    payload = load_json(ROOT / "questions.json")
    match = next((row for row in payload["reviews"] if row["review_key"] == review_key), None)
    if match is None:
        raise SystemExit(f"questions.json has no entry for {review_key}")
    info = {
        "question": match["question"],
        "source": "questions.json",
        "source_quote": match.get("source_quote"),
        "review_title": match.get("review_title"),
        "reconstructed": False,
        "doi": match.get("doi"),
    }
    return info, str(match["question"]).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True)
    parser.add_argument("--api-base", default=None)
    parser.add_argument(
        "--asked",
        action="store_true",
        help="Use the committed research question and write results/asked/, leaving the title run in place",
    )
    args = parser.parse_args()

    question_info, question = question_for(args.review, args.asked)
    case_path = RESULTS / args.review / "case.json"
    case = load_json(case_path)
    if not question:
        raise SystemExit(f"no question for {args.review}")
    if question_info.get("reconstructed"):
        print("NOTE: question was reconstructed from the review title, not taken from SYNERGY metadata")
        print(f"  {question}")
    else:
        print(f"question: {question}")

    stack = Stack(args.api_base, timeout=120)
    events, session_id = hunt(stack, case["case_id"], args.review, question)
    scope = latest_scope(stack, case["case_id"], session_id)
    scope_id = scope["scope_id"]
    print(f"  scope {scope_id} hits={scope.get('hit_count')} maybes={scope.get('maybe_count')}", flush=True)

    sample = stack.get(f"/matters/{case['case_id']}/review/searches/{scope_id}")
    # The scope on the review payload is re-read after the queue is stored.
    scope = sample.get("scope") or scope
    methodology = ((scope.get("source_detail") or {}).get("methodology")) or {}
    if not methodology:
        raise SystemExit("scope has no methodology payload; the hunt did not record one")
    nots = sample.get("nots") or []
    plan = sample.get("sample_plan") or {}
    print(
        f"  elusion sample {len(nots)} (target {plan.get('elusion_target')}, "
        f"methodology elusion_sample_size {methodology.get('elusion_sample_size')})",
        flush=True,
    )

    members = all_members(stack, case["case_id"], scope_id)
    out = (RESULTS / "asked" / args.review) if args.asked else (RESULTS / args.review)
    out.mkdir(parents=True, exist_ok=True)
    write_members(out / "scope_members.csv", members)
    (out / "methodology.json").write_text(json.dumps(methodology, indent=2), encoding="utf-8")
    write_elusion(out / "elusion_sample.csv", nots)
    stored_ids = (scope.get("source_detail") or {}).get("reject_sample_ids") or []
    (out / "run.json").write_text(
        json.dumps(
            {
                "review_key": args.review,
                "case_id": case["case_id"],
                "scope_id": scope_id,
                "strategy": STRATEGY,
                "question": question,
                "question_source": question_info.get("source"),
                "question_reconstructed": bool(question_info.get("reconstructed")),
                "source_quote": question_info.get("source_quote"),
                "review_title": question_info.get("review_title"),
                "query": STRATEGY["template"].format(question=question),
                "member_rows": len(members),
                "elusion_rows": len(nots),
                "sample_plan": plan,
                "reject_sample_ids": stored_ids,
                "stream_event_types": [event.get("type") for event in events],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
