#!/usr/bin/env python3
"""Fetch and summarize public AtCoder submissions without external dependencies."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

USER_ID = "usausamikann"
API_URL = "https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions"
RESOURCE_URL = "https://kenkoooo.com/atcoder/resources"
PAGE_SIZE = 500
JST = ZoneInfo("Asia/Tokyo")


def write_json_if_changed(path: Path, obj: object) -> bool:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return True


def read_json(path: Path, default: object) -> object:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def utc_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def jst_date(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=JST).date().isoformat()


def validate_submission(item: object, user_id: str) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Submission must be a JSON object")
    for key in ("id", "epoch_second"):
        if type(item.get(key)) is not int or item[key] < 0:
            raise ValueError(f"Invalid {key}: {item!r}")
    if item.get("user_id") != user_id:
        raise ValueError(f"Unexpected user: {item.get('user_id')!r}")
    for key in ("problem_id", "contest_id", "result"):
        if not isinstance(item.get(key), str) or not item[key]:
            raise ValueError(f"Invalid {key}: {item!r}")
    return item


def load_history(data_dir: Path, user_id: str = USER_ID) -> list[dict]:
    by_id: dict[int, dict] = {}
    for path in sorted((data_dir / "submissions").glob("*.json")):
        rows = read_json(path, [])
        if not isinstance(rows, list):
            raise ValueError(f"Invalid history file {path}")
        for raw in rows:
            item = validate_submission(raw, user_id)
            if item["id"] in by_id and by_id[item["id"]] != item:
                raise ValueError(f"Conflicting submission ID: {item['id']}")
            by_id[item["id"]] = item
    return sorted(by_id.values(), key=lambda x: (x["epoch_second"], x["id"]))


class JsonClient:
    """Throttle all API requests, including retries and metadata downloads."""

    def __init__(self, min_interval: float = 1.3, attempts: int = 4):
        self.min_interval = min_interval
        self.attempts = attempts
        self.last_request_at: float | None = None

    def get(self, url: str) -> object:
        for attempt in range(self.attempts):
            if self.last_request_at is not None:
                wait = self.min_interval - (time.monotonic() - self.last_request_at)
                if wait > 0:
                    time.sleep(wait)
            self.last_request_at = time.monotonic()
            try:
                req = Request(url, headers={"User-Agent": "atcoder-progress/1.0 (GitHub Actions)"})
                with urlopen(req, timeout=35) as response:
                    return json.load(response)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                # A missing endpoint or forbidden request should not be retried.
                if isinstance(exc, HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                    raise
                if attempt + 1 >= self.attempts:
                    raise
                time.sleep(min(2 ** attempt * 2, 20))
        raise AssertionError("unreachable")


def get_new_history(
    existing: list[dict],
    fetch_page,
    user_id: str = USER_ID,
    max_pages: int = 10000,
) -> list[dict]:
    """Fetch through the final partial page, deduplicating overlapping seconds by ID.

    The API caps results at 500 and filters on from_second. Querying one second
    before the last timestamp avoids losing normal same-second boundary entries.
    If >=500 submissions occur at one timestamp, offset-free pagination cannot
    guarantee completeness; fail explicitly instead of silently losing records.
    """
    by_id = {row["id"]: validate_submission(row, user_id) for row in existing}
    last_time = max((x["epoch_second"] for x in existing), default=None)
    cursor = max(0, last_time - 1) if last_time is not None else 0

    for _ in range(max_pages):
        batch = fetch_page(cursor)
        if not isinstance(batch, list) or len(batch) > PAGE_SIZE:
            raise ValueError("Unexpected API page format or size")
        page = [validate_submission(x, user_id) for x in batch]
        # An API error must never replace an existing record with older data.
        for item in page:
            by_id[item["id"]] = item
        if len(page) < PAGE_SIZE:
            return sorted(by_id.values(), key=lambda x: (x["epoch_second"], x["id"]))
        highest = max(x["epoch_second"] for x in page)
        next_cursor = max(0, highest - 1)
        if next_cursor <= cursor:
            raise RuntimeError("Cannot paginate: >=500 results at the same second")
        cursor = next_cursor
    raise RuntimeError("Exceeded maximum API pages")


def fetch_history(existing: list[dict], client: JsonClient, user_id: str) -> list[dict]:
    def page(from_second: int) -> object:
        url = API_URL + "?" + urlencode({"user": user_id, "from_second": from_second})
        return client.get(url)
    return get_new_history(existing, page, user_id=user_id)


def is_training_contest(contest_id: str) -> bool:
    return contest_id == "abs" or contest_id.startswith("adt_")


def preferred_contest(problem_id: str, options: set[str], observed: list[str]) -> str | None:
    """Use the contest where the user actually submitted, not an ADT reprint.

    AtCoder Problems metadata contains repeated IDs in training contests.
    Picking the last metadata row silently changes ABC C into ADT B, etc.
    """
    original_prefix = problem_id.rsplit("_", 1)[0]
    for cid in observed:
        if not is_training_contest(cid) and cid in options:
            return cid
    if original_prefix in options:
        return original_prefix
    for cid in observed:
        if not is_training_contest(cid):
            return cid
    originals = sorted(x for x in options if not is_training_contest(x))
    if originals:
        return originals[0]
    return observed[0] if observed else (sorted(options)[0] if options else None)


def fetch_catalog(
    problem_ids: set[str],
    client: JsonClient,
    submissions: list[dict] | None = None,
) -> dict[str, dict]:
    """Join catalog records by canonical contest (not the arbitrary last match)."""
    problems = client.get(RESOURCE_URL + "/problems.json")
    links = client.get(RESOURCE_URL + "/contest-problem.json")
    models = client.get(RESOURCE_URL + "/problem-models.json")
    if not isinstance(problems, list) or not isinstance(links, list) or not isinstance(models, dict):
        raise ValueError("Unexpected AtCoder Problems resource format")

    records: dict[str, list[dict]] = defaultdict(list)
    pair_records: dict[str, list[dict]] = defaultdict(list)
    submissions_by_problem: dict[str, list[str]] = defaultdict(list)
    for sub in sorted(submissions or [], key=lambda x: (x["epoch_second"], x["id"])):
        if sub["problem_id"] in problem_ids:
            cid = sub["contest_id"]
            if cid not in submissions_by_problem[sub["problem_id"]]:
                submissions_by_problem[sub["problem_id"]].append(cid)
    for p in problems:
        if isinstance(p, dict) and p.get("id") in problem_ids:
            records[p["id"]].append(p)
    for link in links:
        if isinstance(link, dict) and link.get("problem_id") in problem_ids:
            pair_records[link["problem_id"]].append(link)

    result: dict[str, dict] = {}
    for pid in sorted(problem_ids):
        options = {
            x["contest_id"] for x in records[pid] + pair_records[pid]
            if isinstance(x.get("contest_id"), str)
        }
        cid = preferred_contest(pid, options, submissions_by_problem[pid])
        match = next((p for p in records[pid] if p.get("contest_id") == cid), None)
        if match is None and records[pid]:
            match = records[pid][0]
        link = next((p for p in pair_records[pid] if p.get("contest_id") == cid), None)
        info = {
            "contest_id": cid,
            "name": match.get("name") if match else None,
            "index": link.get("problem_index") if link else None,
        }
        # Some older problems have no contest-problem entry.
        if info["index"] is None and cid == pid.rsplit("_", 1)[0]:
            suffix = pid.rsplit("_", 1)[-1].upper()
            if suffix in "ABCDEFGH" and len(suffix) == 1:
                info["index"] = suffix
        model = models.get(pid)
        if isinstance(model, dict):
            info["difficulty"] = model.get("difficulty")
            info["is_experimental"] = model.get("is_experimental")
        result[pid] = info
    return result


def build_views(history: list[dict], catalog: dict, user_id: str = USER_ID) -> dict[str, object]:
    history = sorted(history, key=lambda x: (x["epoch_second"], x["id"]))
    groups: dict[str, list[dict]] = defaultdict(list)
    verdicts: Counter[str] = Counter()
    year_counts: Counter[str] = Counter()
    activity: Counter[str] = Counter()
    for item in history:
        groups[item["problem_id"]].append(item)
        verdicts[item["result"]] += 1
        year_counts[str(datetime.fromtimestamp(item["epoch_second"], tz=timezone.utc).year)] += 1
        activity[jst_date(item["epoch_second"])] += 1

    statuses: list[dict] = []
    for pid, rows in sorted(groups.items()):
        info = catalog.get(pid, {})
        best_ac = next((x for x in rows if x["result"] == "AC"), None)
        last = rows[-1]
        original_contest = info.get("contest_id") or rows[0]["contest_id"]
        statuses.append({
            "problem_id": pid,
            "contest_id": original_contest,
            "index": info.get("index"),
            "title": info.get("name"),
            "difficulty": info.get("difficulty"),
            "is_experimental": info.get("is_experimental"),
            "url": f"https://atcoder.jp/contests/{original_contest}/tasks/{pid}",
            "solved": best_ac is not None,
            "submission_count": len(rows),
            "verdict_counts": dict(sorted(Counter(x["result"] for x in rows).items())),
            "first_submission_epoch": rows[0]["epoch_second"],
            "first_ac_epoch": best_ac["epoch_second"] if best_ac else None,
            "last_submission_epoch": last["epoch_second"],
            "last_result": last["result"],
            "last_submission_id": last["id"],
        })

    abc_indices: dict[str, dict[str, int]] = {}
    for row in statuses:
        if not str(row["contest_id"]).startswith("abc"):
            continue
        idx = row["index"]
        if idx not in list("ABCDEFGH"):
            continue
        counter = abc_indices.setdefault(idx, {"attempted": 0, "solved": 0})
        counter["attempted"] += 1
        counter["solved"] += int(row["solved"])

    # Distinct solved problems in order of their first AC, not merely recent resubmits.
    recently_solved = sorted(
        (p for p in statuses if p["solved"]),
        key=lambda p: (p["first_ac_epoch"], p["problem_id"]),
        reverse=True,
    )[:30]
    latest = history[-1] if history else None
    summary = {
        "schema_version": 1,
        "user_id": user_id,
        "submission_count": len(history),
        "attempted_problem_count": len(statuses),
        "solved_problem_count": sum(p["solved"] for p in statuses),
        "unresolved_problem_count": sum(not p["solved"] for p in statuses),
        "verdict_counts": dict(sorted(verdicts.items())),
        "submissions_by_utc_year": dict(sorted(year_counts.items())),
        "submissions_by_jst_day_recent_90": dict(sorted(activity.items())[-90:]),
        "abc_index": dict(sorted(abc_indices.items())),
        "latest_submission_epoch": latest["epoch_second"] if latest else None,
        "latest_submission_utc": utc_iso(latest["epoch_second"]) if latest else None,
        "latest_submission_id": latest["id"] if latest else None,
        "recently_solved_problems": [
            {key: p[key] for key in ("problem_id", "contest_id", "index", "title",
                                      "difficulty", "first_ac_epoch", "url")}
            for p in recently_solved
        ],
    }
    recent = [
        {**row, "task_url": next(
            (p["url"] for p in statuses if p["problem_id"] == row["problem_id"]), None
        ), "submission_url":
            f"https://atcoder.jp/contests/{row['contest_id']}/submissions/{row['id']}"}
        for row in history[-100:][::-1]
    ]
    return {"summary": summary, "problems": statuses, "recent": recent}


def save_data(data_dir: Path, history: list[dict], catalog: dict, user_id: str) -> list[str]:
    changes: list[str] = []
    years: dict[str, list[dict]] = defaultdict(list)
    for row in history:
        year = str(datetime.fromtimestamp(row["epoch_second"], tz=timezone.utc).year)
        years[year].append(row)
    for year, items in sorted(years.items()):
        path = data_dir / "submissions" / f"{year}.json"
        if write_json_if_changed(path, items):
            changes.append(str(path))
    if write_json_if_changed(data_dir / "catalog.json", catalog):
        changes.append(str(data_dir / "catalog.json"))
    for name, value in build_views(history, catalog, user_id).items():
        path = data_dir / f"{name}.json"
        if write_json_if_changed(path, value):
            changes.append(str(path))
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=USER_ID)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--refresh-catalog", action="store_true")
    args = parser.parse_args()
    existing = load_history(args.data_dir, args.user)
    client = JsonClient()
    updated = fetch_history(existing, client, args.user)
    catalog_path = args.data_dir / "catalog.json"
    catalog = read_json(catalog_path, {})
    if not isinstance(catalog, dict):
        raise ValueError("Malformed catalog.json")
    current_ids = {x["problem_id"] for x in updated}
    if updated != existing or args.refresh_catalog or not catalog_path.exists():
        catalog = fetch_catalog(current_ids, client, updated)
    changed = save_data(args.data_dir, updated, catalog, args.user)
    print(f"Submissions: {len(updated)}; new/updated: {len(updated) - len(existing)}")
    print(f"Changed files: {len(changed)}")
    for path in changed:
        print(path)


if __name__ == "__main__":
    main()
