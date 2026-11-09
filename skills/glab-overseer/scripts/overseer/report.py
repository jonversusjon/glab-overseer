"""`report [--public] [--out DIR]`: report.md and report.json built only from `.overseer/`.

The public report is an allowlist. `public_report` reads each record and copies named, type-checked fields
into new objects (ids, shas, small integers, enumerated words, relative output paths); it never receives a
private dump and never filters one. A field that fails its check is omitted and listed as an incomplete
item, never guessed. The private report is the public structure plus a `private` section with every state
file. Both files are rendered from one dict, so they agree.
"""
import hashlib
import base64
import json
import os
import re
import shutil
import stat

from . import approval, controller, execution as X, judge as J, paths, plan as P, review as R, store, workspace as W

SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
WORD_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
# Only the model shapes the supervisor resolves (models.py); anything else, including a token-shaped string, is dropped.
MODEL_RE = re.compile(r"^(?:opus|sonnet|haiku|claude-(?:opus|sonnet|haiku)-[0-9][0-9.-]{0,20}|gemini-[0-9]+(?:\.[0-9]+)*-[a-z]+-[a-z]+)$")
FAMILY = {"claude": "claude", "agy": "google", "google": "google"}
BACKENDS = ("local", "slurm")
INLINE_MAX = 16 * 1024 * 1024  # a larger file is copied by streaming into <out>/files/ instead of inlined
LIST_ONLY_DIRS = ("worktree", ".git")  # committed worktree files and .git: listed with size and digest, recoverable from commits


def _int(v):
    return v if type(v) is int and v >= 0 else None


def _word(v):
    return v if isinstance(v, str) and WORD_RE.match(v) else None


def _sha(v):
    return v if isinstance(v, str) and SHA_RE.match(v) else None


def _bool(v):
    return v if isinstance(v, bool) else None


def _model(v):
    return v if isinstance(v, str) and MODEL_RE.match(v) else None


class _Gaps(object):
    """Incomplete items: a record that is missing, unreadable or malformed is reported, never skipped."""

    def __init__(self):
        self.items = []

    def add(self, unit, what):
        item = {"unit": unit, "item": what}
        if item not in self.items:
            self.items.append(item)

    def obj(self, path, unit, what, required=False):
        d = X._load(path)
        if d is X.BAD:
            self.add(unit, "%s unreadable" % what)
            return None
        if d is None and required:
            self.add(unit, "%s missing" % what)
        return d


def _url(repo, kind, number):
    return "https://github.com/%s/%s/%d" % (repo, kind, number) if _int(number) is not None else None


def _stat_digest(f):
    """(size, sha256) of a regular file, or (None, None)."""
    try:
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return os.path.getsize(f), h.hexdigest()
    except OSError:
        return None, None


def _outputs(root, wt, adir, unit, j, gaps):
    """Declared outputs of a judgment: relative path, ok, size and sha256 (null when not computable).

    A job output reports the size and digest recorded in the judgment, plus the current ones; a file that
    changed after it was judged is marked and the unit is listed as incomplete.
    """
    out = []
    for o in j.get("outputs") if isinstance(j.get("outputs"), list) else []:
        p = o.get("path") if isinstance(o, dict) else None
        if not isinstance(p, str) or P.relpath_problem(p):
            gaps.add(unit["id"], "an output record has an invalid path")
            continue
        ok = o.get("ok") is True
        size = digest = None
        entry = {"path": p, "ok": ok}
        if ok and unit["kind"] == "job":
            size, digest = _int(o.get("size")), _sha(o.get("sha256"))
            cur_size = cur_digest = None
            try:
                cur_size, cur_digest = _stat_digest(paths.safe_relpath(os.path.join(adir, "outputs"), p))
            except (OSError, ValueError):
                pass
            entry.update(size=size, sha256=digest, current_size=cur_size, current_sha256=cur_digest)
            if size is None or digest is None:
                gaps.add(unit["id"], "output %s has no size and digest recorded in its judgment" % p)
                entry["changed_since_judgment"] = None
            else:
                changed = (cur_size, cur_digest) != (size, digest)
                entry["changed_since_judgment"] = changed
                if changed:
                    gaps.add(unit["id"], "output %s changed since judgment" % p)
            out.append(entry)
            continue
        rec_size, rec_digest = _int(o.get("size")), _sha(o.get("sha256"))
        cur = None  # (size, digest) of the candidate blob, when the worktree is still here
        try:
            if ok and isinstance(j.get("candidate"), str) and wt:
                blob = W.git(wt, "--literal-pathspecs", "cat-file", "blob", "%s:%s" % (j["candidate"], p), raw=True)["stdout"]
                cur = (len(blob), hashlib.sha256(blob).hexdigest())
        except (OSError, ValueError, W.GitError, X.ExecutionError):
            pass
        if ok and rec_size is not None and rec_digest is not None:
            size, digest = rec_size, rec_digest  # recorded at judgment time: no worktree needed
            if cur is not None:
                entry["changed_since_judgment"] = cur != (rec_size, rec_digest)
                if entry["changed_since_judgment"]:
                    gaps.add(unit["id"], "output %s changed since judgment" % p)
        else:
            if cur is not None:
                size, digest = cur
            if ok and digest is None:
                gaps.add(unit["id"], "output %s size and digest unavailable" % p)
        entry.update(size=size, sha256=digest)
        out.append(entry)
    return out


def _author(inv, gaps, uid):
    intent = gaps.obj(os.path.join(inv, "intent.json"), uid, "invocation intent", True)
    if not intent:
        return None, None, None
    argv = intent.get("argv") if isinstance(intent.get("argv"), list) else []
    fam = FAMILY.get(os.path.basename(argv[0])) if argv and isinstance(argv[0], str) else None
    model = None
    if "--model" in argv and argv.index("--model") + 1 < len(argv):
        model = _model(argv[argv.index("--model") + 1])
    backend = intent.get("backend") if intent.get("backend") in BACKENDS else None
    return backend, fam, model


def _reviews(adir, uid, gaps):
    rounds = []
    base = os.path.join(adir, "reviews")
    names = sorted((x for x in os.listdir(base) if re.match(r"^r[1-9][0-9]*$", x)) if os.path.isdir(base) else [],
                   key=lambda s: (len(s), s))
    for name in names:
        rdir = os.path.join(base, name)
        rd = gaps.obj(os.path.join(rdir, "round.json"), uid, "review round " + name, True)
        if not rd:
            continue
        calls = [c for c in rd.get("calls") or [] if isinstance(c, dict)]
        last = ([c for c in calls if c.get("state") == "ok"] or calls or [{}])[-1]
        rv = gaps.obj(os.path.join(rdir, "review.json"), uid, "review " + name)
        disp = gaps.obj(os.path.join(rdir, "dispositions.json"), uid, "dispositions " + name)
        counts = {}
        for d in ((disp or {}).get("dispositions") or {}).values():
            if isinstance(d, dict) and d.get("ruling") in R.RULINGS:
                counts[d["ruling"]] = counts.get(d["ruling"], 0) + 1
        rounds.append({
            "round": _int(rd.get("round")), "state": _word(rd.get("state")), "verdict": _word(rd.get("verdict")),
            "candidate": _sha(rd.get("candidate")),
            "reviewer_family": last.get("family") if last.get("family") in FAMILY.values() else _word(rd.get("reviewer_family")),
            "reviewer_model": _model(last.get("model") or rd.get("model")),
            "same_family_fallback": rd.get("same_family_fallback") is True,
            "findings": len(rv["findings"]) if isinstance(rv, dict) and isinstance(rv.get("findings"), list) else None,
            "dispositions": counts})
    return rounds


def _attempt(root, pid, u, n, repo, gaps, need_judgment=False):
    uid = u["id"]
    adir = paths.attempt_dir(root, pid, uid, n)
    invs = [i for i in X.iter_invocations(root, pid, uid) if i["attempt"] == n]
    kinds, backends, models = {}, [], []
    for i in invs:
        kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        b, fam, model = _author(i["path"], gaps, uid)
        if b and b not in backends:
            backends.append(b)
        if i["kind"] in ("author", "fixer") and {"family": fam, "model": model} not in models:
            models.append({"family": fam, "model": model})
    ws = gaps.obj(os.path.join(adir, "workspace.json"), uid, "workspace") if u["kind"] == "code" else None
    wt = ws.get("worktree") if isinstance(ws, dict) and isinstance(ws.get("worktree"), str) and os.path.isdir(ws["worktree"]) else None
    j = gaps.obj(os.path.join(adir, "judgment.json"), uid, "judgment", need_judgment) or {}
    integ = gaps.obj(os.path.join(adir, "integration.json"), uid, "integration record") or {}
    dec = gaps.obj(os.path.join(adir, "reviews", "orchestrator_decision.json"), uid, "orchestrator decision")
    return {
        "attempt": n, "backends": backends, "invocations": kinds, "workers": models,
        "base": _sha((ws or {}).get("base")),
        "integration_base": _sha(integ.get("integration_base")),
        "judgment": {"status": _word(j.get("status")), "candidate": _sha(j.get("candidate")),
                     "test_rc": j.get("test_rc") if type(j.get("test_rc")) is int else None,
                     "orchestrator_resolved": j.get("orchestrator_resolved") is True,
                     "integrated": j.get("integrated") is True} if j else None,
        "outputs": _outputs(root, wt, adir, u, j, gaps) if j else [],
        "reviews": _reviews(adir, uid, gaps),
        "orchestrator_decision": bool(dec and dec.get("orchestrator_decision") is True),
    }


def _state(root, pid, u, st, gaps):
    c = st["closure"].get("state")
    if c in approval.CLOSED_STATES:
        return c
    n = st["counters"].get("attempts")
    if not X._int(n) or n == 0:
        return "pending"
    try:
        j = J.valid_judgment(root, pid, u, n)
        if j and j.get("status") == "DONE" and controller.is_closed(root, pid, u, st):
            return "done"
        if j and j.get("status") in ("UNCERTAIN", "FAILED"):
            return j["status"] if j["status"] == "UNCERTAIN" else "failed"
        if j:
            return "judged_done"
        states = [X.invocation_status(i["path"], False)["state"] for i in X.iter_invocations(root, pid, u["id"]) if i["attempt"] == n]
    except (X.ExecutionError, OSError, ValueError):
        gaps.add(u["id"], "state could not be determined")
        return "unknown"
    return "UNCERTAIN" if "UNCERTAIN" in states else ("running" if set(states) & set(X.ACTIVE) else "unjudged")


def _closure(u, st, state):
    """The stored closure facet, except a job: its DONE judgment closes it without a write (controller.is_closed)."""
    c = _word(st["closure"].get("state"))
    return "closed" if u.get("kind") == "job" and state == "done" and c not in approval.CLOSED_STATES else c


def _unit(root, pid, u, st, repo, gaps, effective):
    uid = u["id"]
    n = st["counters"].get("attempts")
    state = _state(root, pid, u, st, gaps) if effective else "split"
    udir = paths.unit_dir(root, pid, uid)
    pr = gaps.obj(os.path.join(udir, "pr.json"), uid, "pull request record")
    mg = gaps.obj(os.path.join(udir, "merge.json"), uid, "merge record")
    issues = gaps.obj(os.path.join(paths.plan_dir(root, pid), "issues.json"), uid, "issues record") or {}
    issue = (issues.get("filed") or {}).get(uid) if isinstance(issues.get("filed"), dict) else None
    attempts = []
    if X._int(n):
        for k in range(1, n + 1):
            if os.path.isdir(paths.attempt_dir(root, pid, uid, k)):
                attempts.append(_attempt(root, pid, u, k, repo, gaps, k == n and state in approval.CLOSED_STATES))
            else:
                gaps.add(uid, "attempt a%d directory missing" % k)
    if state in ("UNCERTAIN", "failed", "unjudged", "unknown", "running", "pending", "judged_done"):
        gaps.add(uid, "state " + state)
    if u.get("kind") == "pr" and state == "closed" and not pr:
        gaps.add(uid, "pull request record missing")
    if state in ("merged", "done") and st["tracker"].get("state") not in ("closed", "none"):
        gaps.add(uid, "tracker %s" % (_word(st["tracker"].get("state")) or "unknown"))
    return {
        "id": uid, "title": u.get("title") if isinstance(u.get("title"), str) else None, "kind": _word(u.get("kind")),
        "state": state, "attempts_used": _int(n),
        "facets": {"authorization": _word(st["authorization"].get("state")), "execution": _word(st["execution"].get("state")),
                   "closure": _closure(u, st, state), "tracker": _word(st["tracker"].get("state"))},
        "attempts": attempts,
        "issue": {"number": _int(issue), "url": _url(repo, "issues", issue)} if _int(issue) is not None else None,
        "pr": {"number": _int(pr.get("number")), "url": _url(repo, "pull", pr.get("number")),
               "head": _sha(pr.get("head_oid")), "candidate": _sha(pr.get("candidate")),
               **({"opened": True, "merged": False, "draft": pr.get("draft") is True} if u.get("kind") == "pr" else {})}
        if pr else None,
        "merge": {"state": _word(mg.get("state")), "pr_head": _sha(mg.get("pr_head")), "merge_commit": _sha(mg.get("merge_commit")),
                  "integration_base": _sha(mg.get("integration_base")), "candidate": _sha(mg.get("candidate"))} if mg else None,
    }


def _orphan_units(root, pid, gaps):
    """Without a readable plan, still list every unit whose newest judgment is not DONE (or is missing)."""
    udir = os.path.join(paths.plan_dir(root, pid), "units")
    for uid in sorted(os.listdir(udir)) if os.path.isdir(udir) else []:
        if not paths.valid_id(uid):
            continue
        adir = os.path.join(udir, uid, "attempts")
        nums = [int(m.group(1)) for m in (X.ATTEMPT_RE.match(x) for x in (os.listdir(adir) if os.path.isdir(adir) else [])) if m]
        if not nums:
            gaps.add(uid, "state pending")
            continue
        j = gaps.obj(os.path.join(adir, "a%d" % max(nums), "judgment.json"), uid, "judgment")
        status = _word(j.get("status")) if isinstance(j, dict) else None
        if status != "DONE":
            gaps.add(uid, "state %s" % (status if status in ("UNCERTAIN", "FAILED") else "unjudged"))
        elif j.get("kind") != "job":  # a code unit is complete only when merged (or otherwise closed)
            st = approval.load_unit_state(root, pid, uid)
            if st["closure"].get("state") not in approval.CLOSED_STATES:
                gaps.add(uid, "state DONE, not merged")


def public_report(root):
    gaps = _Gaps()
    plans = []
    pdir = paths.plans_dir(root)
    for pid in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        if not (paths.valid_id(pid) and os.path.isdir(os.path.join(pdir, pid))):
            continue
        ppath = os.path.join(pdir, pid, "plan.json")
        stored = X._load(ppath)
        if not isinstance(stored, dict) or not isinstance(stored.get("units"), list):
            problem = "plan.json is missing" if stored is None else "plan.json is unreadable"
            gaps.add(None, "plan %s: %s" % (pid, problem))
            _orphan_units(root, pid, gaps)
            plans.append({"plan_id": pid, "units": [], "problem": problem})
            continue
        repo = stored.get("repo") if isinstance(stored.get("repo"), str) and P.REPO_RE.match(stored["repo"]) else None
        try:
            approval.require_approval(root, pid)
            eff = approval.effective_plan(root, pid)["units"]
        except approval.ApprovalError:
            gaps.add(None, "plan %s approval is invalid or its digest changed" % pid)
            eff = stored["units"]
        live = dict((u["id"], u) for u in eff if isinstance(u, dict) and paths.valid_id(u.get("id")))
        units, seen = [], set()
        for u in list(stored["units"]) + eff:  # stored units first; split children follow
            if not (isinstance(u, dict) and paths.valid_id(u.get("id"))) or u["id"] in seen:
                continue
            seen.add(u["id"])
            units.append(_unit(root, pid, live.get(u["id"], u), approval.load_unit_state(root, pid, u["id"]),
                               repo, gaps, u["id"] in live))
        plans.append({"plan_id": pid, "units": units})
    return {"public": True, "plans": plans, "incomplete": gaps.items}


# ---- private -----------------------------------------------------------------

def _file_entry(f, rel, listed_only, out_dir):
    """One regular file: size and sha256 (streamed), and its whole content.

    Content up to INLINE_MAX is inlined (text, parsed JSON or base64). A larger file is copied by streaming
    to <out_dir>/files/<rel> and recorded as `copied_to`; memory stays bounded either way.
    """
    try:
        h, n = hashlib.sha256(), 0
        with open(f, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
                n += len(block)
        e = {"size": n, "sha256": h.hexdigest()}
        if listed_only:
            e["listed_only"] = True
            return e
        if n > INLINE_MAX:
            if out_dir is None:
                e["copied_to"] = None
                e["not_copied"] = "no output directory"
                return e
            dest = os.path.join(out_dir, "files", rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".tmp"
            h2, n2 = hashlib.sha256(), 0
            with open(f, "rb") as src, open(tmp, "wb") as dst:
                for block in iter(lambda: src.read(1 << 20), b""):
                    h2.update(block)
                    n2 += len(block)
                    dst.write(block)
            os.replace(tmp, dest)
            e.update(size=n2, sha256=h2.hexdigest(), copied_to=os.path.join("files", rel))
            return e
        with open(f, "rb") as fh:
            data = fh.read(n + 1)  # n <= INLINE_MAX; never preallocate the limit
    except OSError as ex:
        return {"unreadable": str(ex)}
    e.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        e.update(encoding="base64", content=base64.b64encode(data).decode("ascii"))
        return e
    if f.endswith(".json"):
        try:
            e["json"] = json.loads(text)
            return e
        except ValueError:
            pass
    e["text"] = text
    return e


def _worktree_root(rel_d):
    """The attempt worktree directory (relative, as a parts list) that rel_d is inside, or None."""
    parts = rel_d.split(os.sep)
    for i, part in enumerate(parts):
        if part == "worktree" and i >= 2 and parts[i - 2] == "attempts":
            return parts[:i + 1]
    return None


def _dirty_files(wt):
    """Relative paths of modified or untracked files in a worktree, or an error string."""
    if not os.path.exists(os.path.join(wt, ".git")):
        return None, "not a git checkout (no .git)"
    try:
        out = W.git(wt, "status", "--porcelain", "-z", "--untracked-files=all", raw=True)["stdout"]
    except (OSError, ValueError, W.GitError, X.ExecutionError) as ex:
        return None, str(ex)
    dirty, entries, i = set(), out.split(b"\0"), 0
    while i < len(entries):
        ent = entries[i]
        i += 1
        if len(ent) < 4:
            continue
        xy, path = ent[:2].decode("ascii", "replace"), ent[3:].decode("utf-8", "replace")
        if "R" in xy or "C" in xy:
            i += 1  # the original path follows a rename or copy
        dirty.add(path)
    return dirty, None


def private_section(root, out_dir=None):
    """Every regular file under .overseer/ (except the report directory itself) with relative path, size and digest.

    Committed files in an attempt worktree and anything under .git are listed with size and digest only; the
    modified and untracked files of a worktree are included in full, since no commit holds them.
    """
    files = {}
    base = paths.state_dir(root)
    dirty = {}  # worktree rel dir -> set of dirty relative paths (or None)
    for d, dirs, names in os.walk(base):
        rel_d = os.path.relpath(d, base)
        dirs[:] = sorted(x for x in dirs if not (rel_d == "." and x == "report"))
        listonly = any(part in LIST_ONLY_DIRS for part in rel_d.split(os.sep))
        wroot = _worktree_root(rel_d)
        if wroot is not None:
            key = os.sep.join(wroot)
            if key not in dirty:
                dirty[key], err = _dirty_files(os.path.join(base, key))
                if err is not None:
                    files[key] = {"unreadable": err}
        for x in list(dirs):  # a symlinked directory is recorded, never followed
            if os.path.islink(os.path.join(d, x)):
                dirs.remove(x)
                names.append(x)
        for n in sorted(names):
            f = os.path.join(d, n)
            rel = os.path.relpath(f, base)
            if rel_d == "." and n == "report":
                continue
            try:
                mode = os.lstat(f).st_mode
            except OSError as ex:
                files[rel] = {"unreadable": str(ex)}
                continue
            if stat.S_ISLNK(mode):
                try:
                    files[rel] = {"symlink": os.readlink(f)}
                except OSError as ex:
                    files[rel] = {"unreadable": str(ex)}
            elif stat.S_ISREG(mode):
                lo = listonly
                if wroot is not None and ".git" not in rel_d.split(os.sep) and n != ".git":
                    sub = os.path.relpath(f, os.path.join(base, os.sep.join(wroot)))
                    if dirty.get(os.sep.join(wroot)) and sub in dirty[os.sep.join(wroot)]:
                        lo = False
                files[rel] = _file_entry(f, rel, lo, out_dir)
            else:
                files[rel] = {"special": "not a regular file"}
    try:
        events = store.read_events(root)
    except (OSError, ValueError) as e:
        events = {"unreadable": str(e)}
    return {"files": files, "events": events}


def build(root, public, out_dir=None):
    rep = public_report(root)
    if not public:
        rep["public"] = False
        rep["private"] = private_section(root, out_dir)
    return rep


# ---- markdown ----------------------------------------------------------------

def _cell(v):
    if v is None:
        return "-"
    return re.sub(r"\s+", " ", str(v)).replace("|", "\\|") or "-"


def _short(sha):
    return sha[:12] if sha else "-"


def render(rep):
    out = ["# glab-overseer report (%s)" % ("public" if rep["public"] else "private"), ""]
    for p in rep["plans"]:
        if p.get("problem"):
            out += ["## Plan %s" % p["plan_id"], "", "%s; its units cannot be listed." % p["problem"], ""]
            continue
        out += ["## Plan %s" % p["plan_id"], "", "| unit | kind | state | attempts | closure | tracker | PR | issue |",
                "|---|---|---|---|---|---|---|---|"]
        for u in p["units"]:
            f = u["facets"]
            out.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % tuple(_cell(x) for x in (
                u["id"], u["kind"], u["state"], u["attempts_used"], f["closure"], f["tracker"],
                (u["pr"] or {}).get("number"), (u["issue"] or {}).get("number"))))
        out.append("")
        for u in p["units"]:
            out.append("### %s: %s" % (u["id"], _cell(u["title"])))
            if u["pr"] and u["kind"] == "pr":
                out.append("- pull request %s opened, not merged%s, at %s" % (
                    _cell(u["pr"]["url"]), " (draft)" if u["pr"].get("draft") else "", _short(u["pr"]["head"])))
            elif u["pr"]:
                out.append("- pull request %s at %s" % (_cell(u["pr"]["url"]), _short(u["pr"]["head"])))
            if u["issue"]:
                out.append("- issue %s" % _cell(u["issue"]["url"]))
            if u["merge"]:
                m = u["merge"]
                out.append("- merge %s: candidate %s, PR head %s, integration base %s, merge commit %s" % (
                    _cell(m["state"]), _short(m["candidate"]), _short(m["pr_head"]), _short(m["integration_base"]), _short(m["merge_commit"])))
            for a in u["attempts"]:
                j = a["judgment"] or {}
                out.append("- attempt a%d: backend %s, workers %s, base %s, candidate %s, judgment %s%s, test rc %s" % (
                    a["attempt"], "/".join(a["backends"]) or "-",
                    ", ".join("%s %s" % (w["family"] or "?", w["model"] or "?") for w in a["workers"]) or "-",
                    _short(a["base"]), _short(j.get("candidate")), _cell(j.get("status")),
                    " (orchestrator-resolved)" if j.get("orchestrator_resolved") else "", _cell(j.get("test_rc"))))
                for o in a["outputs"]:
                    out.append("  - output `%s` ok=%s size=%s sha256=%s%s" % (
                        o["path"], str(o["ok"]).lower(), _cell(o["size"]), _cell(o["sha256"]),
                        " CHANGED SINCE JUDGMENT (now size=%s sha256=%s)" % (_cell(o.get("current_size")), _cell(o.get("current_sha256")))
                        if o.get("changed_since_judgment") else ""))
                for r in a["reviews"]:
                    out.append("  - review r%s by %s %s: %s%s, %s findings, dispositions %s" % (
                        _cell(r["round"]), _cell(r["reviewer_family"]), _cell(r["reviewer_model"]), _cell(r["verdict"]),
                        " (same-family fallback)" if r["same_family_fallback"] else "", _cell(r["findings"]),
                        json.dumps(r["dispositions"], sort_keys=True)))
                if a["orchestrator_decision"]:
                    out.append("  - orchestrator decision recorded at the review cap")
            out.append("")
    out += ["## Incomplete or uncertain items", ""]
    out += ["- %s: %s" % (i["unit"] or "plan", i["item"]) for i in rep["incomplete"]] or ["none"]
    if not rep["public"]:
        pv = rep["private"]
        out += ["", "## Private state", "", "%d state files, %d events." % (len(pv["files"]), len(pv["events"]) if isinstance(pv["events"], list) else 0),
                "The full records are in report.json under `private`.",
                "Committed files inside an attempt `worktree/` and everything under any `.git/` are listed with size and digest only (recoverable from the recorded commits); modified and untracked worktree files are included in full. Files over %d MiB are copied to `files/` next to this report." % (INLINE_MAX >> 20)]
    return "\n".join(out) + "\n"


def write_report(root, out_dir, public):
    os.makedirs(out_dir, exist_ok=True)
    rep = build(root, public, out_dir)
    store.write_json_atomic(os.path.join(out_dir, "report.json"), rep)
    text = render(rep)
    tmp = os.path.join(out_dir, ".report.md.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, os.path.join(out_dir, "report.md"))
    return rep


def configure(p):
    p.add_argument("--public", action="store_true", help="allowlisted report safe to share")
    p.add_argument("--out", default=None, metavar="DIR", help="default: .overseer/report/<private|public>")


def run(args):
    root = paths.find_root(args.root)
    out = args.out or os.path.join(paths.state_dir(root), "report", "public" if args.public else "private")
    if os.path.exists(out) and not os.path.isdir(out):
        raise ValueError("--out %s exists and is not a directory" % out)
    rep = write_report(root, os.path.abspath(out), args.public)
    n = sum(len(p["units"]) for p in rep["plans"])
    print("wrote %s and %s (%d units, %d incomplete items)" % (
        os.path.join(out, "report.md"), os.path.join(out, "report.json"), n, len(rep["incomplete"])))
    return 0
