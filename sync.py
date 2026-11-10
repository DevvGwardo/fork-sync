#!/usr/bin/env python3
"""Sync every public fork in an account with its upstream.

Fast-forwards forks that have no local commits, and refuses to touch forks that
diverged (commits ahead of upstream) or whose default branch names do not match
upstream's. Safe by default: --dry-run changes nothing, and even a real run never
forces a diverged fork.

Usage:
    GITHUB_TOKEN=... python sync.py --owner DevvGwardo --dry-run
    GITHUB_TOKEN=... python sync.py --owner DevvGwardo

Exit codes: 0 = every fork is in sync or was synced; 2 = something needs a human.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UA = "fork-sync/1.0"


def q(ref: str) -> str:
    """Quote a git ref for a URL path, keeping ':' literal so owner:branch survives."""
    return urllib.parse.quote(ref, safe="")


class Api:
    """Minimal GitHub REST client with retries and rate-limit handling."""

    def __init__(self, token: str, sleep=time.sleep):
        self.token = token
        self.sleep = sleep
        self.calls = 0

    def request(self, method: str, path: str, body: dict | None = None,
                allow: tuple[int, ...] = ()) -> tuple[int, dict]:
        url = path if path.startswith("http") else API + path
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            req.add_header("User-Agent", UA)
            if data:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=45) as r:
                    self.calls += 1
                    raw = r.read()
                    return r.status, (json.loads(raw) if raw else {})
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    payload = {"message": raw.decode("utf-8", "replace")[:300]}
                if e.code in allow:
                    return e.code, payload
                if e.code in (403, 429) and attempt < 3 and "rate limit" in str(payload).lower():
                    self.sleep(int(e.headers.get("Retry-After") or 30))
                    continue
                return e.code, payload
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 3:
                    return 0, {"message": f"network error: {e}"}
                self.sleep(2 ** attempt)
        return 0, {"message": "exhausted retries"}

    def list_pages(self, path: str) -> list[dict]:
        """Walk pages of 100 until a short page comes back."""
        out: list[dict] = []
        page = 1
        sep = "&" if "?" in path else "?"
        while True:
            status, payload = self.request("GET", f"{API}{path}{sep}per_page=100&page={page}")
            if status != 200 or not isinstance(payload, list):
                raise SystemExit(f"listing failed ({status}): {payload.get('message', payload)}")
            out.extend(payload)
            if len(payload) < 100 or page >= 50:
                return out
            page += 1


def public_forks(api: Api, owner: str) -> list[dict]:
    repos = api.list_pages(f"/users/{owner}/repos?type=owner")
    return [r for r in repos if r.get("fork") and not r.get("private") and not r.get("archived")]


def plan_one(api: Api, repo: dict, diverged: str = "skip") -> dict:
    """Decide what to do with one fork. Read-only.

    diverged="skip" refuses any fork with commits ahead of upstream (default).
    diverged="merge" allows merge-upstream on those forks, which preserves the
    local commits by creating a merge commit; a conflict still lands in needs-human.
    """
    full = repo["full_name"]
    fork_branch = repo["default_branch"]
    row = {"repo": full, "branch": fork_branch, "action": "", "detail": "", "upstream": ""}

    status, meta = api.request("GET", f"/repos/{full}")
    if status != 200:
        row.update(action="error", detail=f"metadata {status}: {meta.get('message', '')}")
        return row

    parent = meta.get("parent") or meta.get("source") or {}
    upstream = parent.get("full_name")
    upstream_branch = parent.get("default_branch")
    row["upstream"] = upstream or ""
    if not upstream or not upstream_branch:
        row.update(action="skip", detail="not a fork (no parent recorded)")
        return row
    if parent.get("archived"):
        row.update(action="skip", detail="upstream archived")
        return row

    upstream_owner = upstream.split("/")[0]
    base = f"{q(upstream_owner)}:{q(upstream_branch)}"
    head = q(fork_branch)
    status, cmp = api.request("GET", f"/repos/{full}/compare/{base}...{head}", allow=(404, 422))
    if status == 404:
        row.update(action="skip", detail="branch not comparable (renamed or deleted upstream)")
        return row
    if status != 200:
        row.update(action="error", detail=f"compare {status}: {cmp.get('message', '')[:120]}")
        return row

    ahead = cmp.get("ahead_by", 0)
    behind = cmp.get("behind_by", 0)
    if ahead > 0 and behind == 0:
        row.update(action="up-to-date", detail=f"nothing to pull ({ahead} local commit(s) only)")
        return row
    if ahead > 0:
        if diverged == "merge" and fork_branch == upstream_branch:
            row.update(action="merge", detail=f"merges upstream, keeps {ahead} local commit(s)")
        else:
            row.update(action="needs-human",
                       detail=f"diverged: {ahead} local commit(s) ahead, {behind} behind")
        return row
    if behind == 0:
        row.update(action="up-to-date", detail="already current")
        return row
    if fork_branch != upstream_branch:
        row.update(action="needs-human",
                   detail=f"default branch {fork_branch!r} != upstream {upstream_branch!r}; {behind} behind")
        return row
    row.update(action="sync", detail=f"{behind} commit(s) behind")
    return row


def apply(api: Api, row: dict) -> None:
    """Fast-forward one fork via merge-upstream. Only called for action == 'sync'."""
    status, payload = api.request("POST", f"/repos/{row['repo']}/merge-upstream",
                                  body={"branch": row["branch"]}, allow=(409, 422))
    if status == 200:
        row["action"] = "synced"
        row["detail"] = payload.get("merge_type") or "merged"
    elif status == 409:
        row["action"] = "needs-human"
        row["detail"] = "merge conflict: upstream changed files this fork also changed"
    else:
        row["action"] = "error"
        row["detail"] = f"{status}: {payload.get('message', '')[:160]}"


def render(rows: list[dict], dry_run: bool) -> str:
    icon = {"synced": "✅", "sync": "⬆️", "merge": "🔀", "up-to-date": "✅", "skip": "⏭️",
            "needs-human": "⚠️", "error": "❌"}
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    title = ("Fork sync (dry run)" if dry_run else "Fork sync") + f" — {len(rows)} public forks"
    table = "| Fork | Branch | Result | Detail |\n|---|---|---|---|" + "\n" + "\n".join(
        f"| `{r['repo']}` | `{r['branch']}` | {icon.get(r['action'], '?')} {r['action']} | {r['detail']} |"
        for r in rows)
    return f"### {title}\n\n{summary}\n\n{table}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fast-forward public forks from their upstreams.")
    ap.add_argument("--owner", default=os.environ.get("FORK_OWNER") or os.environ.get("GITHUB_REPOSITORY_OWNER"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--diverged", choices=("skip", "merge"), default="skip",
                    help="what to do with forks that have local commits ahead of upstream "
                         "(skip = never touch them, the default; merge = merge upstream in, keeping them)")
    ap.add_argument("--only", default="", help="comma-separated repo names to restrict to")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("error: set GITHUB_TOKEN (public_repo is enough for public forks)", file=sys.stderr)
        return 1
    if not args.owner:
        print("error: pass --owner or set FORK_OWNER", file=sys.stderr)
        return 1

    api = Api(token)
    forks = public_forks(api, args.owner)
    only = {s.strip().lower() for s in args.only.split(",") if s.strip()}
    if only:
        forks = [f for f in forks if f["name"].lower() in only or f["full_name"].lower() in only]

    rows = [plan_one(api, f, diverged=args.diverged) for f in forks]
    if not args.dry_run:
        for row in rows:
            if row["action"] in ("sync", "merge"):
                apply(api, row)

    print(render(rows, args.dry_run))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(render(rows, args.dry_run) + "\n")

    stuck = [r for r in rows if r["action"] in ("needs-human", "error")]
    if stuck and not args.dry_run:
        print(f"\n{len(stuck)} fork(s) need a human: " + ", ".join(r["repo"] for r in stuck), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
