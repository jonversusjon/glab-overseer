# Plan format

A plan is one JSON object. `overseer.py plan validate PLAN` checks it and prints every problem; `plan approve` stores it with an approver and a digest that covers the plan, the issue drafts and the boundary. After approval the plan is immutable except through `plan split`.

## Top level

| field | meaning |
|---|---|
| `plan_id` | matches `^[a-z0-9][a-z0-9_-]{0,63}$` (it becomes a directory name) |
| `repo` | GitHub `owner/name` |
| `target_branch` | branch that pull requests merge into |
| `scope` | repository-relative globs the plan may change; no absolute paths, no `..`. Required when the plan has a code unit; a plan without one may omit it or give `[]` |
| `caps` | optional: `workers` (max 3), `attempts` (max 2), `review_rounds` (max 3). A value above its maximum is refused |
| `test_globs` | optional: globs that mark test files for `nonvacuous` (default: `tests/**`, `test_*.py`, `*_test.py`) |
| `slurm` | optional defaults for batch execution: `partition`, `account`, `qos`, `gres`, `time` (finite walltime), `cpus`, and exactly one of `mem` or `mem_per_cpu` |
| `units` | list of units, at least one |

A glob in a unit `scope` must be covered by the plan `scope`: equal to it, a literal path it matches, or under a `dir/**` prefix of it. When unsure, list literal paths or widen the plan scope to `dir/**` before approval, because widening later stops for the owner.

Plans carry no model, provider, family, thinking or effort fields. A key such as `model` or `reviewer_family` is an error: roles are fixed by the host.

**Unknown keys are errors.** The top level accepts only the fields above, and each unit kind accepts only its own fields (listed below). `plan validate` names every unknown key and the kind, for example `unit u1: unknown key(s) 'draft', 'merge' for a code unit (allowed: ...)`. A key that is silently ignored is a misunderstanding waiting to happen, so a plan that carries one does not validate.

## Units

Unit ids follow the same pattern as `plan_id`. `needs` lists unit ids that must close first (a code unit closes when its pull request is merged, a job unit when its judgment is DONE, a pr unit when its pull request is open). Cycles and unknown ids are errors.

`kind: "code"` requires `title`, `prompt`, `claims` (list of testable statements; the reviewer checks them), `test_command`, `outputs` and `scope`. The author works in a per-attempt git worktree and is told the scope and the test command. Outputs are repository-relative files that must exist at the judged commit.

`kind: "job"` requires `title`, `command` and `outputs`. The command runs from the project root with `OVERSEER_OUT` set to the attempt's outputs directory; outputs are relative to it. Write results there, never to `/tmp` (a batch job may see a private `/tmp`). A job may carry its own `slurm` block that overrides the plan defaults.

Allowed keys: a code unit takes `id`, `kind`, `title`, `prompt`, `claims`, `test_command`, `outputs`, `scope`, `needs`, `slurm`; a job takes `id`, `kind`, `title`, `command`, `outputs`, `scope`, `needs`, `slurm`.

`kind: "pr"` opens a pull request for a branch that **already exists on origin**. It runs no worker and no command, and the supervisor never merges the pull request it opens.

| field | meaning |
|---|---|
| `id`, `title` | as for other units; `title` is the pull request title |
| `kind` | `"pr"` |
| `head_branch` | required: the branch on origin that the pull request proposes |
| `body` | required, non-empty: the free-text pull request body |
| `base_branch` | optional: the branch to merge into; default the plan `target_branch`. It must differ from `head_branch` |
| `head_sha` | optional, 40 lowercase hex digits: origin's `head_branch` must be exactly this commit |
| `draft` | optional boolean, default `false`: open the pull request as a draft |
| `needs` | optional, as for other units |

Branch names pass the same check as `target_branch`. `prompt`, `claims`, `test_command`, `command`, `outputs`, `scope` and `slurm` are errors on a pr unit, and so is any other unknown key. A pr unit needs no `slurm` block, handshake or Slurm. A plan with only pr units may omit the plan `scope`.

`pr U` reads origin with `git ls-remote` first and refuses, before any write, when `head_branch` or `base_branch` is missing or `head_sha` does not match. It never pushes. The pull request body is the unit marker, `Refs #N` when the unit's issue was filed (never `Closes`), and then `body` verbatim. It reads back state, head, base, draft flag, title and body, and closes the unit only when all of them match. A rerun reconciles the open pull request and never opens a second one. A pr unit cannot be split.

An output is a path string or an object `{"path", "min_bytes", "sha256", "validate"}`. `min_bytes` is a size floor, `sha256` an exact digest, `validate` a command that gets the file in `$OVERSEER_OUTPUT` and must exit 0. Two parallel units that declare the same output path produce a warning, not an error.

## Complete example

```json
{
  "plan_id": "fragment-counts",
  "repo": "your-org/your-repo",
  "target_branch": "main",
  "scope": ["src/**", "tests/**", "docs/*.md", "workflow/**"],
  "caps": {"workers": 3, "attempts": 2, "review_rounds": 3},
  "test_globs": ["tests/**", "test_*.py"],
  "slurm": {"partition": "batch-cpu", "account": "lab", "time": "02:00:00", "cpus": 4, "mem": "16G"},
  "units": [
    {
      "id": "counter",
      "kind": "code",
      "title": "Add the fragment counter",
      "prompt": "Implement count_fragments(path) in src/counter.py. It reads a BED file and returns a dict of chromosome to count. Skip comment lines.",
      "claims": ["count_fragments returns correct counts for a BED file", "comment and blank lines are ignored"],
      "test_command": "python -m unittest discover -s tests",
      "outputs": ["src/counter.py", {"path": "tests/test_counter.py", "min_bytes": 1}],
      "scope": ["src/counter.py", "tests/test_counter.py"],
      "needs": []
    },
    {
      "id": "cli",
      "kind": "code",
      "title": "Add a command line entry point",
      "prompt": "Add src/cli.py with main(argv) that prints the counts as tab-separated lines, and document it in docs/usage.md.",
      "claims": ["main prints one line per chromosome", "docs/usage.md shows an example"],
      "test_command": "python -m unittest discover -s tests",
      "outputs": ["src/cli.py", "docs/usage.md"],
      "scope": ["src/cli.py", "tests/test_cli.py", "docs/usage.md"],
      "needs": ["counter"]
    },
    {
      "id": "run-counts",
      "kind": "job",
      "title": "Count fragments on the sample set",
      "command": "python src/cli.py data/samples.bed > \"$OVERSEER_OUT/counts.tsv\"",
      "outputs": [{"path": "counts.tsv", "min_bytes": 10, "validate": "test $(wc -l < \"$OVERSEER_OUTPUT\") -ge 2"}],
      "needs": ["cli"],
      "slurm": {"time": "00:30:00", "mem": "8G"}
    }
  ]
}
```

The `slurm` block is used only when the supervisor chooses the batch backend (see `backends.md`). On a laptop it is ignored but still validated, so a plan written on a laptop runs on the cluster unchanged. Replace the partition and account with your site's values; they are the only site-specific part, and they belong in the plan, not in the skill.

## Stacked pull requests

Two pr units can stack: the second takes the first's head branch as its `base_branch` and needs it, so `next` offers it only once the first pull request is open.

```json
{
  "plan_id": "stacked-prs",
  "repo": "your-org/your-repo",
  "target_branch": "main",
  "units": [
    {
      "id": "pr-parser",
      "kind": "pr",
      "title": "Parser rewrite",
      "head_branch": "feat/parser",
      "body": "Replaces the hand-written parser. Review this one first.",
      "draft": true
    },
    {
      "id": "pr-cli",
      "kind": "pr",
      "title": "Command line on the new parser",
      "head_branch": "feat/cli",
      "base_branch": "feat/parser",
      "head_sha": "0123456789abcdef0123456789abcdef01234567",
      "body": "Stacked on the parser pull request.",
      "needs": ["pr-parser"]
    }
  ]
}
```

## Splitting a unit

`overseer.py plan split U --into SPEC.json --reason TEXT` replaces an unclosed unit with narrower children. Children must stay inside the unit's scope, cover every output of the parent, and keep the parent's counters, so a split never resets a cap. SPEC.json is a list of children, or an object with a `children` list. Each child needs an `id` and a non-empty `outputs` list, and inherits the parent's other fields unless it overrides them. A split that needs wider scope is a stop for the owner. A pr unit is refused.
