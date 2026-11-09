# glab-overseer

A Goodarzi Lab skill for Claude Code and Antigravity that takes a repository and an intention to planned, dispatched, reviewed and merged work. You describe what you want; the skill writes a plan with units of work, asks you for one approval, files the issues, runs coding agents and batch jobs in parallel, has every change reviewed by a model from the other family, merges what the evidence supports, and gives you a report.

It uses only what every lab member already has: a Claude Code subscription and an Antigravity subscription, called through the `claude` and `agy` command-line tools. There are no API keys, no server and no daemon. It runs the same plan on a laptop and on the lab's Slurm cluster.

## Why use it

- **Parallel work without babysitting.** Up to three agents or jobs run at once, each in its own git worktree or output directory. You approve once and are interrupted only for decisions that are yours.
- **Nothing closes on an agent's word.** A code change is done only when the supervisor has checked the evidence: the tests pass in a clean checkout of the exact commit, the change stays inside the approved paths, the declared outputs pass their checks, and the tests actually fail without the change. A job is done only when the scheduler or the job's own exit record shows success and every declared output passes its checks.
- **Cross-family review.** Code written by Claude is reviewed by Gemini, and code written by Gemini is reviewed by Claude. The orchestrator reproduces a finding before it acts on it.
- **Resumable.** Every fact lives on disk under `.overseer/`. Close the laptop, lose the allocation or switch hosts, then run `overseer.py status` and `overseer.py next` in a new session and carry on.
- **Shareable record.** `overseer.py report --public` writes a report built from an allowlist: what merged, which tests ran, who reviewed, and every item that is incomplete or uncertain.

## How it works

Two parts share the work.

The **orchestrator** is the Claude Code or Antigravity session you talk to. It interviews you, writes the plan, reads the evidence, rules on review findings and decides what to do next. It holds the judgment.

The **supervisor** is `overseer.py`, a Python program with no dependencies outside the standard library. It holds the state. It enforces the approved boundary, launches headless workers (`claude -p` or `agy -p`) locally or with `sbatch`, records what each worker did, judges attempts against their evidence, pushes branches, opens pull requests and merges them. It refuses any step the evidence does not support, and it refuses destructive git outright.

A run goes like this:

1. **Survey.** The supervisor records the tools, the git remote, `gh` login and whether Slurm is usable.
2. **Plan.** The orchestrator asks only what it cannot find out by inspection (target branch, what done means, which paths may change, how to test, what needs a batch job) and writes `plan.json`.
3. **Approve.** You see the plan and every issue title and approve once, by name. The approval binds the plan, the issue drafts and the boundary by digest.
4. **Issues.** One GitHub issue per unit is filed on the repository.
5. **Loop.** `overseer.py next` lists what can happen now, derived from disk. The orchestrator dispatches ready units, waits, judges, reviews, adjudicates, checks non-vacuity, opens pull requests and merges, until every unit is closed.
6. **Report.** A public report lists every unit, its evidence and anything left open.

A plan has three kinds of unit. A **code** unit is a change to the repository, written by an agent in its own worktree and closed by a merged pull request. A **job** unit is a command (an analysis, a Nextflow or Snakemake run) that writes declared outputs; it closes when its judgment is DONE. Units can depend on each other through `needs`, so a job can run on code that earlier units merged. A **pr** unit opens a pull request for a branch that already exists on origin (draft or not, against a base you name, with a body you write); it has no worker, closes when the pull request is open and is never merged by the supervisor. Plans accept only the documented keys: an unknown key is a validation error.

## Requirements

- `git` and `gh`, with `gh` logged in to an account that can push to the repository.
- Python 3.9 or newer. Standard library only.
- `claude` (Claude Code) and `agy` (Antigravity), both logged in with your subscription. Either one can be the orchestrator. Code written by one family is reviewed by the other, so the full loop needs both.
- Linux or macOS.

## Install

```
git clone https://github.com/goodarzilab/glab-overseer
cd glab-overseer
./install.sh
```

This copies the skill into `~/.claude/skills/glab-overseer` and `~/.gemini/config/skills/glab-overseer`. Run `./install.sh` again after a `git pull` to update. Options: `--only claude` or `--only agy` installs one; `--link` installs symlinks into the checkout so a `git pull` updates the skill directly; `--claude-dir` and `--agy-dir` choose other skill roots; `--dry-run` shows what would change; `--uninstall` removes the installed copies. The installer only touches directories it created. Details: `skills/glab-overseer/references/install.md`.

## Quick start

On a laptop, in a project that has a GitHub remote:

1. Check that no model key is set: `env | grep _API_KEY` should print nothing (see the warning below).
2. Start `claude` (or `agy`) in the project directory.
3. Ask for what you want, for example: "Use glab-overseer to plan this: add a BED fragment counter with tests, a command line entry point, then run it on data/samples.bed."
4. Answer the few questions it asks, read the plan and the issue titles, and approve by name.
5. Let it run. It tells you at each merged pull request and stops when it needs you.

To pick up a plan that someone already wrote, put `plan.json` in the project and ask: "Use the glab-overseer skill to run the existing plan.json in this directory to the end."

Behind the scenes the skill starts with:

```
overseer.py survey
overseer.py models --host claude          # --host google in Antigravity
overseer.py probe-transport
overseer.py plan validate plan.json
overseer.py plan approve plan.json --approver "Your Name"
overseer.py issues file --plan PLAN_ID
```

and then repeats `overseer.py next` and acts on each line. In this README, `overseer.py COMMAND` is short for `python3 ~/.claude/skills/glab-overseer/scripts/overseer.py COMMAND` (or the same path under `~/.gemini/config/skills/`), run from the project root. You can run any command yourself; `--help` lists them all.

### On the cluster

Never run an agent on a login node. Take an interactive allocation first, start the orchestrator there, and let the supervisor submit workers with `sbatch`.

- Keep the project on shared storage that compute nodes see at the same path. A project under `/tmp` is refused, because a batch job can have a private `/tmp` and report success while writing nothing you can see.
- Give the plan a `slurm` block with your partition, a finite walltime and memory (`mem` or `mem_per_cpu`, not both). Site values belong in the plan, never in this repository.
- Run `overseer.py slurm-handshake --plan PLAN_ID` once from inside the allocation. It proves a batch job sees the project root. Dispatch refuses without a recent handshake.

The same plan runs on a laptop unchanged: without Slurm the supervisor runs workers as local background processes. Details: `skills/glab-overseer/references/backends.md`.

## A plan

A plan is one JSON object. The orchestrator writes it for you; this is what it contains.

```json
{
  "plan_id": "fragment-counts",
  "repo": "your-org/your-repo",
  "target_branch": "main",
  "scope": ["src/**", "tests/**", "docs/*.md"],
  "units": [
    {
      "id": "counter", "kind": "code", "title": "Add the fragment counter",
      "prompt": "Implement count_fragments(path) in src/counter.py and make the module print the counts for a file given on the command line.",
      "claims": ["count_fragments returns correct counts for a BED file"],
      "test_command": "python -m unittest discover -s tests",
      "outputs": ["src/counter.py", "tests/test_counter.py"],
      "scope": ["src/counter.py", "tests/test_counter.py"],
      "needs": []
    },
    {
      "id": "run-counts", "kind": "job", "title": "Count fragments on the sample set",
      "command": "python src/counter.py data/samples.bed > \"$OVERSEER_OUT/counts.tsv\"",
      "outputs": [{"path": "counts.tsv", "min_bytes": 10}],
      "needs": ["counter"]
    }
  ]
}
```

`claims` are what the reviewer checks. `outputs` are files that must exist (with optional size, digest or validator command). Plans carry no model names: the host decides the models. Full format, caps and a complete example: `skills/glab-overseer/references/plan-format.md`.

## What the orchestrator does alone, and when it stops

After your one approval the orchestrator may dispatch, retry within the caps, split a unit into narrower ones inside the approved scope, rule on review findings, push branches, open pull requests and merge them into the target branch. It records its reasoning with each decision.

It stops and asks you when:

- a unit or fix needs paths outside the approved scope;
- something would write outside the project (another repository, a shared environment);
- an action would rewrite history, force push or delete the target branch (the supervisor refuses these anyway);
- a cap would be raised;
- the plan changed after approval;
- a merge hits a conflict, a unit runs out of attempts, or an author hits a quota or login failure.

Caps: 3 concurrent workers per project, 2 attempts per unit, 3 review rounds per pull request. At the review cap the orchestrator rules alone and the report labels that decision.

## Models

The host family writes the code; the other family reviews it.

| Host | Orchestrates | Writes code | Reviews |
|---|---|---|---|
| Claude Code | `opus` | `sonnet` | newest Gemini flash |
| Antigravity | newest Gemini argon tier | newest Gemini argon tier | Claude `haiku` |

Google model names are resolved from `agy models` and cached with a date (`overseer.py models --refresh` updates them). If the reviewer hits a confirmed quota or login failure, the supervisor retries once with the host family's small model and labels the round `same_family_fallback` in the report. Authors never switch family.

## When is a unit done

`overseer.py judge` decides, and a worker exiting decides nothing.

- **Code** is DONE when the worker has ended, the branch and its ancestry are intact, every commit stays inside the unit's scope with no prohibited files, the declared outputs pass their checks at that commit, and the test command passes in a clean checkout of it. Before merging, `nonvacuous` reverts the non-test changes in a scratch copy and requires the tests to fail, so a test that passes without the change does not count.
- **Job** is DONE when the scheduler or the worker's own exit record shows success and every output check passes. Failure evidence always wins. Logs alone never make a job DONE.
- **UNCERTAIN** means the evidence cannot say how a job ended (for example, the accounting database did not answer). The job keeps its worker slot until the orchestrator inspects the outputs and resolves it with a recorded reason; the report labels it orchestrator-resolved.

Merging is done by the supervisor, never by hand with `gh`. If the target branch moved, `merge` merges it into the unit branch (never a rebase), judges the result again, and reuses the earlier review only when `git patch-id` shows the unit's own change is unchanged. It then squash-merges bound to the reviewed head, and closes the issue. Every external step is recorded before it is taken, so a run interrupted mid-merge reconciles instead of repeating.

## Resuming

All state is in `.overseer/` in the project (it carries its own `.gitignore`). In any new session, on any machine with the same project directory:

```
overseer.py status
overseer.py next
```

`status` shows each unit's state, its attempts and the slots in use. `next` shows what to do. `.overseer/events.jsonl` is the full history. Only one orchestrator can act on a project at a time; a second one gets a lock error naming the holder.

## Reports

```
overseer.py report --public
```

writes `.overseer/report/public/report.md` and `report.json`: one row per unit with its state, attempts, pull request, issue, merge commits, workers, review rounds and output digests, followed by every incomplete or uncertain item. The public report is built from an allowlist of fields and is meant to be shared. `overseer.py report` without `--public` writes the private report with every record; keep that one to yourself.

## Warning: API keys

If an LLM API-key variable (`ANTHROPIC_API_KEY` and similar) is set in your environment, `claude` bills that key instead of your subscription. Do not set one. The supervisor removes these variables from every worker's environment and `survey` warns when one is present, but the session you start yourself inherits your shell, so unset any key before you start it.

## Limits

- Workers run as your Unix user. Worktrees, the Antigravity sandbox and the scrubbed environments prevent accidents; they do not contain a hostile process.
- Workers never push. Only the supervisor commits on their behalf, pushes and merges.
- GitHub Issues is the only tracker.
- It does not estimate remaining subscription quota.
- The public report's allowlist and the repository's hygiene tests are defense in depth, not a guarantee that nothing private can leak. Read a report before you post it.

## Repository layout

```
skills/glab-overseer/SKILL.md          what the orchestrator reads: front door, loop, authority, stops
skills/glab-overseer/references/       plan format, operating loop, backends, review, install
skills/glab-overseer/scripts/          overseer.py and its standard-library modules
install.sh                             installer for both skill roots
tests/                                 unit tests; external CLIs are replaced by shims on PATH
acceptance/                            end-to-end runbook, fixture generator, run checker, results
PLAN.md                                architecture and the decision ledger
```

## Development

Run the tests with `python3 -m unittest discover -s tests`. They need `git` and nothing else; `claude`, `agy`, `gh` and the Slurm tools are replaced by shims. The suite takes a few minutes on Linux and about 17 minutes on a Mac. CI runs it on Python 3.9, 3.12 and 3.14 on Linux and on Python 3.14 on macOS.

`acceptance/` holds the live end-to-end checks: `RUNBOOK.md` describes each run, `make_fixture.py` builds a small throwaway project, and `check_run.py` scores a finished run claim by claim. `acceptance/REPORT.md` has the results. Every run passed its applicable claims: Claude Code and Antigravity each as orchestrator, on Slurm and on the local backend, on Linux and macOS, plus a simulated accounting outage, a lost allocation and a kill in the middle of a merge.

Design decisions, the measured CLI behaviors that shaped them, and what is out of scope are in `PLAN.md`.

## License

MIT. See `LICENSE`.
