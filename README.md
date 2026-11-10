# fork-sync

Keeps every **public fork** in an account up to date with its upstream, once a day.

It is deliberately conservative. It fast-forwards forks that have no local commits, and refuses to touch anything that would lose work:

- forks with **commits ahead of upstream** are reported, not rewritten (`--diverged merge` opts in to merging upstream in, which preserves them);
- forks whose **default branch name differs** from upstream's are reported (renaming is not a sync decision);
- forks with an **archived upstream**, or with no recorded parent, are skipped;
- nothing is ever force-pushed. Every sync goes through GitHub's own `merge-upstream`, which is the same operation as the "Sync fork" button.

Current state on `DevvGwardo`: 18 public forks — 10 fast-forwardable, 6 diverged, 1 archived upstream, 1 current.

## Setup (one time)

1. Create a token that can write to your forks:
   - **Fine-grained**: resource owner = you, repository access = *All repositories*, permission *Contents: Read and write*.
   - or **classic**: `public_repo` scope (add `repo` only if you also want private forks synced).
2. Add it to this repo as the secret **`FORK_SYNC_TOKEN`** (Settings → Secrets and variables → Actions).
3. That is all. The workflow runs daily at 06:17 UTC, and can be triggered by hand with a dry-run option.

`GITHUB_TOKEN` cannot be used: it is scoped to this repository and cannot push to your other repos.

## Local use

```bash
export GITHUB_TOKEN=$(gh auth token)     # or any token with public_repo

python sync.py --owner DevvGwardo --dry-run     # report only
python sync.py --owner DevvGwardo               # fast-forward what is safe
python sync.py --owner DevvGwardo --diverged merge   # also merge behind-and-ahead forks
python sync.py --owner DevvGwardo --only peppy,townies --dry-run
```

Run the tests with `python -m unittest -v`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Everything is current, or was synced |
| 1 | Configuration error (no token, no owner) |
| 2 | At least one fork needs a human (diverged, conflict, or API error) |

A scheduled run that exits 2 fails the workflow, which is the point: GitHub notifies you and the job summary lists exactly which forks need a decision.

## What it does not do

- No tags, releases, or branches other than each fork's default branch.
- No submodule or LFS handling beyond what `merge-upstream` itself does.
- No force-push path, by design. If a fork has to be reset to upstream, do that by hand.

## How a run decides

```
list public, non-archived forks
  └─ for each: read parent + upstream default branch
       └─ compare upstream_default...fork_default
            ├─ behind 0                        → up-to-date
            ├─ ahead 0, behind > 0            → sync (fast-forward)
            ├─ ahead > 0, behind 0            → up-to-date (local commits only)
            ├─ ahead > 0, behind > 0          → needs-human  (or merge with --diverged merge)
            └─ branch names differ            → needs-human
```
