"""Code-unit worktrees, author dispatch and candidate normalization.

A code attempt works in `attempts/a<N>/worktree`, a git worktree on the branch
`overseer/<plan>/<unit>/a<N>` created at the fetched `origin/<target>` commit
(`workspace.json` records the full sha as `base` before the worktree exists, so a
retry converges on the same base). Nothing is written into the worktree except by
the author and by the supervisor's own commit; the task file lives in the
invocation directory.

Normalization never resets, squashes or rebases worker commits. It checks every
commit in `base..HEAD` on its own, then stages the remaining in-scope changes by
explicit path and commits them once. Cache files (`__pycache__`, `*.pyc`,
`.pytest_cache`) that are untracked are neither staged nor deleted. The worker is
cooperative (C20): a worker with the same uid could tamper with `.git`.
"""
import os
import re

from . import adapters, approval, env as E, execution as X, models, paths, slurm, store

MAX_BLOB = 5 * 1024 * 1024
GIT_TIMEOUT = 60
FETCH_TIMEOUT = 300
EXCLUDED_SEGMENTS = ("__pycache__", ".pytest_cache")
SUPERVISOR = ("-c", "user.name=glab-overseer", "-c", "user.email=glab-overseer@localhost")
CFG = ("-c", "core.quotepath=off", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
       "-c", "core.autocrlf=false", "-c", "gc.auto=0")
GIT_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
             "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG",
             "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT", "GIT_PREFIX",
             "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class WorkspaceError(X.ExecutionError):
    pass


class GitError(WorkspaceError):
    pass


class DestructiveGit(approval.OwnerStop):
    """A git command that could lose or rewrite history; refused before it runs (never recorded as a git failure)."""


# Every git call of the supervisor goes through `git()`, which refuses these. The supervisor never needs them.
_HISTORY_VERBS = ("rebase", "filter-branch", "filter-repo")
_OPT_WITH_VALUE = ("-c", "-C", "--git-dir", "--work-tree", "--namespace")
# The git builtins the supervisor runs. An alias can never shadow a builtin, so any other subcommand (which git
# would resolve through an alias or an external `git-<name>` program) is refused, and so is any alias definition.
_ALLOWED_SUBCOMMANDS = frozenset((
    "add", "branch", "cat-file", "checkout", "clone", "commit", "diff", "diff-tree", "fetch", "ls-files", "ls-remote",
    "ls-tree", "merge", "merge-base", "patch-id", "push", "rev-list", "rev-parse", "rm", "status",
    "symbolic-ref", "worktree"))  # no update-ref or reset: the supervisor never moves a ref or resets a branch by hand
_FORCE_LONG = ("--force", "--force-with-lease", "--force-if-includes", "--mirror", "--prune", "--delete")


def destructive_reason(args):
    """Why `args` (git argv after the executable) is destructive, or None."""
    a = [x for x in args if isinstance(x, str)]
    i = 0
    while i < len(a) and a[i].startswith("-"):
        if a[i] in ("-c", "--config-env") or a[i].startswith("--config-env="):
            val = a[i + 1] if a[i] in ("-c", "--config-env") and i + 1 < len(a) else a[i].partition("=")[2]
            if val.strip().lower().startswith("alias."):
                return "alias definition (%s)" % val.split("=")[0][:80]
        i += 2 if a[i] in _OPT_WITH_VALUE or a[i] == "--config-env" else 1
    if i >= len(a):
        return None
    sub, rest = a[i], a[i + 1:]
    longs = set(x.split("=")[0] for x in rest if x.startswith("--"))
    shorts = "".join(x[1:] for x in rest if x.startswith("-") and not x.startswith("--"))
    refs = [x for x in rest if not x.startswith("-")]
    if sub in _HISTORY_VERBS:
        return "history rewriting (%s)" % sub
    if sub not in _ALLOWED_SUBCOMMANDS:
        return "subcommand %r is not a git builtin the supervisor uses (it could be an alias)" % sub[:40]
    if sub == "commit" and "--amend" in longs:
        return "commit --amend"
    if sub == "reset" and "--hard" in longs:
        return "reset --hard"
    if sub == "branch" and ("--delete" in longs or "d" in shorts or "D" in shorts):
        return "branch deletion"
    if sub == "update-ref" and ("-d" in rest or "--delete" in longs):
        return "ref deletion"
    if sub == "push" and (longs & set(_FORCE_LONG) or "f" in shorts or "d" in shorts
                          or any(r.startswith(("+", ":")) or ":+" in r for r in refs)):
        return "forced or deleting push"
    return None


# ---- git ---------------------------------------------------------------------

def git_env():
    env = E.supervisor_env()
    for k in GIT_SCRUB:
        env.pop(k, None)
    env.update({"LC_ALL": "C", "LANG": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                # Replace refs and grafts rewrite history locally: judge, merge and ancestry checks read real objects.
                "GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull})
    return env


def git(cwd, *args, stdin=None, timeout=GIT_TIMEOUT, raw=False, ok=(0,)):
    """Run git in `cwd` by absolute path with stable settings; returns run_tool's dict.

    A timeout or a return code outside `ok` raises GitError: an unanswered git question is never success.
    """
    why = destructive_reason(args)
    if why:
        raise DestructiveGit("destructive git refused (%s): git %s" % (why, " ".join(map(str, args))[:200]))
    env = git_env()
    exe = E.resolve_exe("git", env.get("PATH", ""))
    if exe is None:
        raise GitError("git not found on an absolute PATH entry")
    r = E.run_tool([exe] + list(CFG) + list(args), env, timeout, cwd=cwd, stdin=stdin, raw=raw)
    if r["timed_out"]:
        raise GitError("git %s timed out after %gs" % (args[0], timeout))
    if r["rc"] not in ok:
        raise GitError("git %s failed (rc %s): %s" % (args[0], r["rc"], r["stderr"].strip()[:300]))
    return r


def zsplit(data):
    """NUL-separated git output as strict UTF-8 strings; a path that is not UTF-8 is refused."""
    try:
        return [t.decode("utf-8") for t in data.split(b"\0") if t]
    except UnicodeDecodeError:
        raise WorkspaceError("a path in git output is not valid UTF-8")


def rev(cwd, ref):
    return git(cwd, "rev-parse", "--verify", "-q", ref + "^{commit}")["stdout"].strip()


def in_scope(path, scope):
    return any(paths.glob_match(g, path) for g in scope)


def excluded(path):
    segs = path.split("/")
    return any(s in EXCLUDED_SEGMENTS for s in segs) or segs[-1].endswith(".pyc")


def prohibited_name(path):
    segs = path.split("/")
    b = segs[-1]
    return (any(s.startswith(".overseer") for s in segs) or b == ".env" or b.endswith(".pem")
            or b.endswith(".key") or b.startswith("id_rsa"))


def caps_of(plan):
    """Caps with U1's defaults for any that the plan does not set."""
    return dict(X.P.CAP_MAX, **(plan.get("caps") or {}))


def declared_outputs(unit):
    return set(o["path"] if isinstance(o, dict) else o for o in unit.get("outputs") or [])


# ---- dispatch ----------------------------------------------------------------

def task_text(unit):
    """The author's task, built only from the approved unit (deterministic)."""
    lines = ["# Task: %s" % unit["title"], "", unit["prompt"], "", "## Claims to satisfy"]
    lines += ["- %s" % c for c in unit["claims"]]
    lines += ["", "## Scope", "Change only files matching these repository-relative globs:"]
    lines += ["- %s" % g for g in unit["scope"]]
    lines += ["", "## Test command", "Run it and make it pass before you finish:", "```", unit["test_command"], "```",
              "", "## Rules",
              "- Work only inside the current working directory, which is a git worktree. Touch nothing outside it.",
              "- Stay within the scope above; changes outside it are rejected.",
              "- Never push, never change git remotes, never touch other branches.",
              "- Leave your work committed or uncommitted; both are accepted. Do not rewrite or delete commits.", ""]
    return "\n".join(lines)


def check_branch_name(name, what="target branch"):
    if not (isinstance(name, str) and BRANCH_RE.match(name) and ".." not in name and "//" not in name
            and not name.endswith(("/", ".", ".lock")) and "@{" not in name):
        raise WorkspaceError("%s %r is not a safe branch name" % (what, name))
    return name


def workspace_path(adir):
    return os.path.join(adir, "workspace.json")


def load_workspace(adir):
    ws = X._load(workspace_path(adir))
    if not isinstance(ws, dict) or not all(isinstance(ws.get(k), str) and ws[k] for k in ("base", "branch", "worktree")):
        raise WorkspaceError("attempt has no usable workspace.json (not dispatched as a code unit?)")
    return ws


def _verify_existing(wt, ws):
    """A worktree left by an attempt that never launched must still be pristine."""
    head = git(wt, "rev-parse", "--verify", "-q", "HEAD")["stdout"].strip()
    ref = git(wt, "symbolic-ref", "-q", "HEAD", ok=(0, 1))["stdout"].strip()
    dirty = git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all", raw=True)["stdout"]
    if head != ws["base"] or ref != "refs/heads/" + ws["branch"] or dirty:
        raise WorkspaceError("worktree %s exists but is not a pristine checkout of %s at %s" % (wt, ws["branch"], ws["base"]))


def _survey(root):
    try:
        s = store.read_json(os.path.join(paths.state_dir(root), "survey.json"), None)
    except ValueError:
        return None
    return s if isinstance(s, dict) else None


def dispatch_code(root, plan_id, unit, after_quota=False):
    """Fetch the target, create the attempt worktree and launch the author with the host's code model."""
    plan = approval.effective_plan(root, plan_id)
    uid = unit["id"]
    cache = models.load_valid(root)
    if cache is None:
        raise WorkspaceError("models.json is missing or malformed; run `overseer.py models --host claude|google`")
    family = cache["host"]
    roles = models.roles_for(cache, family)
    if roles is None:
        raise WorkspaceError("the host family %s is unavailable in models.json; refresh it" % family)
    model = roles["code"]
    backend = slurm.choose_backend(root)
    if backend == "slurm":
        slurm.preflight(root, plan, unit)
    X.check_slots(root, plan_id, caps_of(plan)["workers"])
    target = check_branch_name(plan["target_branch"])
    try:
        git(root, "fetch", "origin", "+refs/heads/%s:refs/remotes/origin/%s" % (target, target), timeout=FETCH_TIMEOUT)
    except GitError as e:
        raise WorkspaceError("git fetch origin %s failed; refusing to dispatch: %s" % (target, e))
    base = rev(root, "refs/remotes/origin/" + target)
    n, adir = X.acquire_attempt(root, plan_id, uid, after_quota)
    branch = "overseer/%s/%s/a%d" % (plan_id, uid, n)
    wt = os.path.join(adir, "worktree")
    ws = X._load(workspace_path(adir))
    if ws is X.BAD or (ws and (ws.get("branch") != branch or ws.get("worktree") != wt)):
        raise WorkspaceError("workspace.json of attempt a%d is unusable" % n)
    if ws is None:  # recorded before the worktree exists, so a retry reuses this base
        ws = {"base": base, "base_ref": "origin/" + target, "target": target, "branch": branch,
              "worktree": wt, "time": store.utc_now()}
        store.write_json_atomic(workspace_path(adir), ws)
    if os.path.lexists(wt):
        _verify_existing(wt, ws)
    else:
        git(root, "worktree", "add", "-b", branch, wt, ws["base"])
    store.append_event(root, "worktree_ready", plan_id=plan_id, unit_id=uid, attempt=n, branch=branch, base=ws["base"])
    survey = _survey(root)

    def prepare(inv):
        p = adapters.prepare(family, "author", model, task_text(unit), wt, task_dir=inv, survey=survey)
        return p["argv"], p["stdin"]
    if backend == "slurm":
        return slurm.launch(root, plan_id, uid, n, "author", None, wt, prepare=prepare)
    return X.launch_invocation(root, plan_id, uid, n, "author", None, wt, prepare=prepare)


MAX_FIX_PROMPT = 100000


def fixer_text(unit, prompt):
    """The fixer's task: the orchestrator's prompt, then the unit's scope, test command and rules."""
    return "\n".join(["# Fix: %s" % unit["title"], "", prompt.strip(), "", "## Scope",
                      "Change only files matching these repository-relative globs:"]
                     + ["- %s" % g for g in unit["scope"]]
                     + ["", "## Test command", "Run it and make it pass before you finish:", "```", unit["test_command"], "```",
                        "", "## Rules",
                        "- Work only inside the current working directory, which is a git worktree.",
                        "- Stay within the scope above; never push, never change remotes, never rewrite or delete commits.", ""])


def dispatch_fixer(root, plan_id, unit, prompt):
    """Launch a fixer in the unit's current attempt worktree with the host family's code model."""
    approval.require_approval(root, plan_id)
    plan = approval.effective_plan(root, plan_id)
    uid = unit["id"]
    cache = models.load_valid(root)
    roles = models.roles_for(cache, cache["host"]) if cache else None
    if roles is None:
        raise WorkspaceError("models.json is missing, malformed or lacks the host family; run `overseer.py models --host claude|google`")
    st = approval.load_unit_state(root, plan_id, uid)
    n = st["counters"]["attempts"]
    if not X._int(n):
        raise WorkspaceError("unit %s has no attempt to fix" % uid)
    if st["closure"].get("state") in approval.CLOSED_STATES:
        raise WorkspaceError("unit %s is %s" % (uid, st["closure"]["state"]))
    adir = paths.attempt_dir(root, plan_id, uid, n)
    wt = load_workspace(adir)["worktree"]
    if not os.path.isdir(wt):
        raise WorkspaceError("worktree %s is missing" % wt)
    busy = X.active_invocations(root, plan_id, uid, True)
    if busy:
        raise WorkspaceError("invocation %s of %s is %s" % (busy[0]["seq"], uid, busy[0]["status"]["state"]))
    backend = slurm.choose_backend(root)
    if backend == "slurm":
        slurm.preflight(root, plan, unit)
    X.check_slots(root, plan_id, caps_of(plan)["workers"])
    survey = _survey(root)
    text = fixer_text(unit, prompt)

    def prepare(inv):
        p = adapters.prepare(cache["host"], "author", roles["code"], text, wt, task_dir=inv, survey=survey)
        return p["argv"], p["stdin"]
    launch = slurm.launch if backend == "slurm" else X.launch_invocation
    return launch(root, plan_id, uid, n, "fixer", None, wt, prepare=prepare)


# ---- normalization -----------------------------------------------------------

def commit_changes(wt, sha):
    """[(status, old mode, new mode, new blob, path)] of one commit against its parent, renames split."""
    toks = zsplit(git(wt, "diff-tree", "-r", "-z", "--raw", "--no-renames", "--no-abbrev", "--root",
                      "--no-commit-id", sha, raw=True)["stdout"])
    out, i = [], 0
    while i + 1 < len(toks):
        head = toks[i].lstrip(":").split()
        out.append((head[4][0], head[0], head[1], head[3], toks[i + 1]))
        i += 2
    return out


def blob_sizes(wt, shas):
    if not shas:
        return {}
    r = git(wt, "cat-file", "--batch-check", stdin="".join(s + "\n" for s in shas))["stdout"]
    sizes = {}
    for line in r.splitlines():
        p = line.split()
        if len(p) == 3 and p[1] == "blob":
            sizes[p[0]] = int(p[2])
    return sizes


def check_commit(wt, sha, scope, outputs):
    reasons = []
    changes = commit_changes(wt, sha)
    sizes = blob_sizes(wt, sorted(set(c[3] for c in changes if c[0] != "D" and c[2] not in ("160000", "120000"))))
    for st, om, nm, blob, path in changes:
        tag = "%s@%s" % (path, sha[:12])
        if not in_scope(path, scope):
            reasons.append("out_of_scope:" + tag)
        if st == "D":
            continue
        if nm == "120000":
            reasons.append("symlink:" + tag)
        elif nm == "160000":
            reasons.append("submodule:" + tag)
        elif sizes.get(blob, 0) > MAX_BLOB and path not in outputs:
            reasons.append("oversize:" + tag)
        if prohibited_name(path):
            reasons.append("prohibited_path:" + tag)
    return reasons


def parse_status(data):
    toks = zsplit(data)
    out, i = [], 0
    while i < len(toks):
        t = toks[i]
        i += 1
        origin = None
        if "R" in t[:2] or "C" in t[:2]:
            origin = toks[i]
            i += 1
        out.append((t[:2], t[3:], origin))
    return out


def classify_dirty(wt, scope, outputs):
    """(to_add, staged, reasons, out_of_scope) for everything the author left in the worktree."""
    entries = parse_status(git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                               "--ignore-submodules=none", raw=True)["stdout"])
    index = [l.split("\t", 1) for l in git(wt, "ls-files", "--stage", "-z", raw=False)["stdout"].split("\0") if "\t" in l]
    gitlinks = set(p for m, p in index if m.startswith("160000 "))
    tracked = set(p for _, p in index)
    add, staged, reasons, oos = [], False, [], []
    for xy, path, origin in entries:
        if xy == "??" and excluded(path):
            continue
        if "U" in xy or xy in ("AA", "DD"):
            reasons.append("unmerged:" + path)
            continue
        names = [path] + ([origin] if origin else [])
        bad = [p for p in names if not in_scope(p, scope)]
        if bad:
            oos.extend(bad)
            continue
        full = os.path.join(wt, path)
        if path in gitlinks or (os.path.isdir(full) and not os.path.islink(full)):
            reasons.append("submodule_or_directory:" + path)
        elif os.path.islink(full):
            reasons.append("symlink:" + path)
        elif os.path.lexists(full) and (prohibited_name(path) or (os.path.getsize(full) > MAX_BLOB and path not in outputs)):
            reasons.append("prohibited_or_oversize:" + path)
        if xy[0] not in " ?":
            staged = True
        if xy[1] != " ":  # both sides of a rename: the destination is added, the origin's deletion staged
            add.append(path)
            if origin and origin in tracked:
                add.append(origin)
    return add, staged, reasons, sorted(set(oos))


def index_flags(wt):
    """Tracked paths whose index entry is assume-unchanged (lowercase tag) or skip-worktree (S): git
    status hides their modifications. Never cleared here; the caller fails the judgment."""
    out = git(wt, "ls-files", "-v", "-z", raw=True)["stdout"]
    return sorted(t[2:] for t in zsplit(out) if t[0].islower() or t[0] == "S")


def ignored_outputs(wt, scope, outputs):
    """(paths, reasons): declared in-scope outputs that .gitignore hides from status.

    Only these are force-added, by explicit path; other ignored files are never staged.
    """
    found, reasons = [], []
    for p in sorted(outputs):
        if not in_scope(p, scope) or not os.path.lexists(os.path.join(wt, p)):
            continue
        ents = parse_status(git(wt, "--literal-pathspecs", "status", "--porcelain=v1", "-z", "--ignored",
                                "--", p, raw=True)["stdout"])
        if not any(xy == "!!" for xy, _, _ in ents):
            continue
        full = os.path.join(wt, p)
        if os.path.islink(full) or not os.path.isfile(full):
            reasons.append("symlink_or_not_file:" + p)
        elif prohibited_name(p):
            reasons.append("prohibited_or_oversize:" + p)
        else:
            found.append(p)
    return found, reasons


def ignored_in_scope(wt, scope, outputs):
    """Ignored untracked files under the scope that are neither cache files nor declared outputs.

    They are invisible to status and to the commit, yet a test could pass by reading them."""
    ents = parse_status(git(wt, "status", "--porcelain=v1", "-z", "--ignored", "--untracked-files=all",
                            raw=True)["stdout"])
    return sorted(p for xy, p, _ in ents
                  if xy == "!!" and not excluded(p) and in_scope(p.rstrip("/"), scope) and p not in outputs)


def normalize(root, plan_id, unit, adir, n):
    """Check the worktree and seal a candidate. Returns dict(ok, reasons, candidate, out_of_scope_dirty, commits)."""
    ws = load_workspace(adir)
    wt, base, scope = ws["worktree"], ws["base"], unit["scope"]
    res = {"ok": False, "reasons": [], "candidate": None, "out_of_scope_dirty": [], "commits": [], "base": base}
    if not os.path.isdir(wt):
        res["reasons"].append("worktree_missing")
        return res
    top = git(wt, "rev-parse", "--show-toplevel", ok=(0, 128))["stdout"].strip()
    if not top or os.path.realpath(top) != os.path.realpath(wt):
        res["reasons"].append("not_a_worktree_root")
        return res
    ref = git(wt, "symbolic-ref", "-q", "HEAD", ok=(0, 1))["stdout"].strip()
    if ref != "refs/heads/" + ws["branch"]:
        res["reasons"].append("wrong_branch:%s" % (ref or "detached"))
        return res
    if git(wt, "cat-file", "-e", base + "^{commit}", ok=(0, 128, 1))["rc"] != 0 \
            or git(wt, "merge-base", "--is-ancestor", base, "HEAD", ok=(0, 1))["rc"] != 0:
        res["reasons"].append("base_not_ancestor")
        return res
    outputs = declared_outputs(unit)
    flagged = index_flags(wt)
    if flagged:
        res["reasons"].append("index_flags:" + ",".join(flagged))
        return res
    if git(wt, "rev-list", "--min-parents=2", "%s..HEAD" % base)["stdout"].strip():
        res["reasons"].append("merge_commit_in_range")
        return res
    commits = git(wt, "rev-list", "--reverse", "%s..HEAD" % base)["stdout"].split()
    for c in commits:
        res["reasons"] += check_commit(wt, c, scope, outputs)
    res["commits"] = list(commits)
    add, staged, reasons, oos = classify_dirty(wt, scope, outputs)
    forced, freasons = ignored_outputs(wt, scope, outputs)
    hidden = ignored_in_scope(wt, scope, outputs)
    res["reasons"] += reasons + freasons + (["ignored_in_scope:" + ",".join(hidden)] if hidden else [])
    res["out_of_scope_dirty"] = oos
    res["reasons"] += ["out_of_scope_dirty:" + p for p in oos]
    if res["reasons"]:
        return res
    if add:
        git(wt, "--literal-pathspecs", "add", "--pathspec-from-file=-", "--pathspec-file-nul",
            stdin="".join(p + "\0" for p in add))
    if forced:
        git(wt, "--literal-pathspecs", "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul",
            stdin="".join(p + "\0" for p in forced))
    if git(wt, "diff", "--cached", "--quiet", ok=(0, 1))["rc"] == 1:
        git(wt, *(SUPERVISOR + ("commit", "--no-verify", "--no-gpg-sign", "-m",
                                "overseer: %s/%s attempt %d changes" % (plan_id, unit["id"], n))))
        mine = rev(wt, "HEAD")
        bad = check_commit(wt, mine, scope, outputs)
        res["commits"].append(mine)
        res["supervisor_commit"] = mine
        if bad:
            res["reasons"] += bad
            return res
    res["candidate"] = rev(wt, "HEAD")  # sealed
    if git(wt, "diff", "--quiet", base, res["candidate"], ok=(0, 1))["rc"] == 0:
        res["reasons"].append("no_change")
        return res
    res["ok"] = True
    return res
