---
name: glab-overseer
description: Plan, run or resume a lab project with agents. Use when the user wants to start a project, turn a repository or an idea into a plan with units of work, dispatch coding agents or Slurm jobs, review and merge their pull requests, or pick up an overseer run that is already on disk (look for a .overseer/ directory). Works from Claude Code or Antigravity, on a laptop or on the lab's Slurm cluster.
---

# glab-overseer

You are the orchestrator. `overseer.py` is the supervisor: it holds state under `.overseer/`, launches workers, records evidence and refuses closures the evidence does not support. You hold the judgment. Every fact you act on is on disk, so a new session resumes with `status` and `next`.

Run the supervisor as `python3 <skill dir>/scripts/overseer.py COMMAND` from the project root (the skill dir is where this file lives). `--root DIR` overrides the root. Details live in `references/`: `plan-format.md`, `operating-loop.md`, `backends.md`, `review.md`, `install.md`.

Rules that never bend:
- No LLM API keys. If a key variable such as `ANTHROPIC_API_KEY` is set, `claude` bills the key instead of the subscription. The supervisor strips such variables for workers and `survey` warns about them. Tell the user once and do not set one.
- Never run an agent on a login node. On a cluster, work from an interactive allocation (see `references/backends.md`).
- Workers run as the same user. Worktrees and sandboxes are a cooperative boundary, not containment (C20). Do not describe them as isolation.
- Type-check what you pass in, never guess a missing value, and treat unknown as not done.

## 1. Front door (new project)

Run these in the project, in order, and read the output:

1. `overseer.py survey` records tools, Slurm, git origin and `gh` auth. Add `--probe-auth` to see how each CLI authenticates (one cheap model call each).
2. `overseer.py models --host claude` when you run in Claude Code, `overseer.py models --host google` in Antigravity. This is the host label for model roles (section 5). `--refresh` re-resolves.
3. `overseer.py probe-transport` measures stdin and `--json-schema` support of each CLI.
4. On a cluster only: from inside an interactive allocation run `overseer.py slurm-handshake --plan PLAN_ID`. It proves a batch job sees the project root. Use shared storage for the project, not `/tmp`.

Then interview the user. Ask only what inspection cannot answer: the repository (`owner/name`) and target branch, what done looks like, which paths may change, how to test, what needs a batch job. Write `plan.json` (format and a complete example: `references/plan-format.md`), then:

```
overseer.py plan validate plan.json
overseer.py plan approve plan.json --approver NAME
overseer.py issues file --plan PLAN_ID
```

Before `plan approve`, show the user the plan and every issue title (`validate` lists problems; `plan show PLAN_ID` prints an approved plan). Ask for ONE approval from a named person. `plan approve` binds the plan, the issue drafts and the boundary by digest. Use `issues file --dry-run` to preview. File issues only after approval.

## 2. Operating loop

`overseer.py next` prints the eligible actions derived from disk (no network); `next --json` is machine-readable. Act on its lines, one pass at a time:

| next says | you run |
|---|---|
| `dispatch` | `overseer.py dispatch` (every READY unit up to the free slots) or `dispatch --unit U` |
| `wait` | `overseer.py wait` (polls in Python, no model turns; exit 3 on timeout, so call it again) |
| `judge` | `overseer.py judge U` (exit 0 DONE, 1 FAILED, 4 UNCERTAIN) |
| `resolve` | section 6 |
| `retry` | `dispatch --unit U` starts the next attempt (cap: 2) |
| `review` | `overseer.py review U` (`--retry` once if the reviewer call failed) |
| `dispositions` | rule on every finding: `dispositions U FILE.json` |
| `fix` | write the fix prompt to a file, then `overseer.py fix U --prompt-file F` |
| `nonvacuous` | `overseer.py nonvacuous U` |
| `pr` | `overseer.py pr U` (a code unit: pushes the judged branch; a pr unit: opens the pull request for its existing branch) |
| `merge` | `overseer.py merge U` |
| `tracker` | rerun `merge U` (code) or `overseer.py issues close U --comment TEXT` (job, pr) |
| `stop` | the owner decides: section 4 |

Per code unit the path is `dispatch`, `wait`, `judge`, `review`; then, for each finding you might act on, `overseer.py reproduce U --finding ID --command CMD`, and `dispositions` to rule on all of them; when a fix is needed, `fix`, `judge` and `review` again; then `nonvacuous`, `pr`, `merge`. Job units close on a DONE judgment; no review or PR. A unit with `kind: "pr"` opens a pull request for a branch that already exists on origin and has no worker, attempt or review: `next` says `pr U`, you run `overseer.py pr U` (it reads origin, never pushes, and closes the unit once the pull request reads back open), then `issues close U` files the tracker step. The supervisor never merges that pull request; it is for a human or another process. A job runs in the project checkout: before dispatching one that needs merged code units, update the checkout to the target, or `dispatch` refuses it. `status` (or `status --json`) shows unit facets, active invocations and slots at any time. `abort U` stops a unit's active invocations. `plan split U --into SPEC.json --reason TEXT` replaces an unclosed unit with narrower children inside the approved scope. Exact semantics and exit codes: `references/operating-loop.md`. Review mechanics: `references/review.md`.

`merge` integrates the target branch into the unit branch (never rebase, never force push), retests, pushes, squash-merges the pull request and closes the unit. Exit codes: 3 ambiguous outcome (rerun to reconcile), 5 integration conflict (a stop for you to decide), 1 not ready.

## 3. What you do without asking (O3)

After the one approval you may, without asking: dispatch, retry within caps, split units within the approved scope, adjudicate review findings, push, open pull requests and merge them into the target branch. Record your reasoning in dispositions, resolve reasons and the PR text.

## 4. What always stops for the owner

Stop, tell the user plainly, and wait when any of these holds:
- scope growth: a unit or fix needs paths outside the approved scope;
- a shared write outside the project (another directory, a shared environment, another repository);
- destructive git: force push, history rewrite, deleting the target branch (the supervisor refuses these);
- raising a cap (see section 7);
- a plan whose digest changed since approval (`next` prints a `stop` line);
- an `INTEGRATION_CONFLICT`, an exhausted attempts cap, or a QUOTA or AUTH failure of the author (`dispatch --after-quota` only after the owner agrees).

## 5. Models (O4)

The host family writes code; the other family reviews. The supervisor resolves names and passes them; plans carry no model fields.

| | orchestrate | write code | review |
|---|---|---|---|
| Claude Code host | `opus` | `sonnet` | newest Google flash |
| Antigravity host | newest argon-tier | newest argon-tier | Claude `haiku` |

On a confirmed quota or auth failure of the reviewer, the supervisor retries once with the host family's small model in a fresh session and labels the round `same_family_fallback`. Authors never switch family. Say so in the report whenever a fallback occurred. Host-specific: in Claude Code the supervisor runs `claude -p`; in Antigravity it runs `agy -p`. Both are launched headless by the supervisor, never by you.

## 6. UNCERTAIN work (D4)

A job without terminal evidence (the scheduler cannot confirm how it ended) is UNCERTAIN and keeps its worker slot. Inspect the outputs and logs yourself. Then:

```
overseer.py resolve U --as done   --reason TEXT --evidence TEXT
overseer.py resolve U --as failed --reason TEXT --evidence TEXT
```

Use `--as done` only for a job whose output predicates pass and for which no failure evidence exists; the supervisor refuses otherwise. Both outcomes are labeled orchestrator-resolved in the report. Code units cannot be resolved as done.

## 7. Caps (O12, D1)

At most 3 concurrent workers per project. The cap counts background executions only: authors, fixers and job units, including queued and uncertain ones. Reviews, reproductions, tests and `nonvacuous` run one at a time and do not count. At most 2 attempts per unit and 3 review rounds per pull request. At the review cap, rule alone with `overseer.py dispositions U --orchestrator-decision TEXT`; it is recorded and labeled. Raising any cap needs the owner and a new approval.

## 8. Resuming

A new session, a new host or a new machine: `cd` into the project, run `overseer.py status` and `overseer.py next`, read `.overseer/events.jsonl` if you need the history, and continue. If another session holds the project lock, do not delete it: `overseer.py unlock --force --reason TEXT` is for a stale lock and the owner should know. Do not trust memory of what was running; trust `status`.

## 9. Reporting (O13)

Tell the user at each merged PR (unit, PR number, what changed, test result) and immediately when the owner is needed (what you need decided, your recommendation). Keep messages short. At the end run `overseer.py report --public` and give the user `report.md` (default `.overseer/report/public/`). `overseer.py report` without `--public` writes the private report with every record; do not share it. `--out DIR` chooses the directory. The public report lists every UNCERTAIN or incomplete item; never describe the run as complete when it does.

## 10. Failure and honesty

Report a failure with the command and its error text, then fix or ask. A worker that exits is not a unit that is done: only `judge` decides, and only evidence closes a unit. Do not edit `.overseer/` files by hand. Do not merge with `gh` yourself; `merge` writes the intents and read-backs that make an interrupted run recoverable.
