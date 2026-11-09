"""Judgment: `judge` and `resolve`.

`judgment.json` of an attempt holds status DONE, FAILED or UNCERTAIN with the reasons.
Failure evidence always wins; an unanswered question is UNCERTAIN, never DONE.
A code judgment binds to the sealed candidate head and is valid only while the
worktree HEAD still equals it (`valid_judgment`). A job judgment binds to the token
of the invocation it judged. Writes expect the caller to hold `store.ProjectLock`.
Output validators and the test command run through `env.run_tool` with the worker
environment: they are plan content, approved with the plan.
"""
import hashlib
import json
import os
import shutil
import tempfile

from . import approval, env as E, execution as X, paths, store, workspace as W

TEST_TIMEOUT = 1800
VALIDATE_TIMEOUT = 300
RELEVANT = {"code": ("author", "fixer"), "job": ("job",)}
EXIT = {"DONE": 0, "FAILED": 1, "UNCERTAIN": 4}


class JudgeError(X.ExecutionError):
    pass


def _unit_attempt(root, plan_id, unit):
    X.refuse_pr_unit(unit, "has no attempt to judge")
    st = approval.load_unit_state(root, plan_id, unit["id"])
    n = st["counters"]["attempts"]
    if not X._int(n):
        raise JudgeError("unit %s has no attempt to judge" % unit["id"])
    return st, n, paths.attempt_dir(root, plan_id, unit["id"], n)


def _invocations(root, plan_id, unit, n):
    kinds = RELEVANT[unit["kind"]]
    return [dict(i, status=X.invocation_status(i["path"], True))
            for i in X.iter_invocations(root, plan_id, unit["id"]) if i["attempt"] == n and i["kind"] in kinds]


def execution_evidence(invs):
    """(class, evidence) from the attempt's invocations: class is success, failure or uncertain.

    A queued or running invocation refuses. Definite failure in any invocation wins; otherwise
    any UNCERTAIN or CANCELLING one makes the attempt uncertain; otherwise the latest decides.
    """
    if not invs or invs[-1]["status"]["state"] == "NOT_LAUNCHED":
        raise JudgeError("nothing was launched for this attempt")
    for i in invs:
        if i["status"]["state"] in ("QUEUED", "RUNNING"):
            raise JudgeError("invocation %s is %s; judge after it is terminal" % (i["seq"], i["status"]["state"]))
    last = invs[-1]
    s = last["status"]
    if s["state"] == "RESOLVED":
        raise JudgeError("invocation %s was resolved by the orchestrator; see judgment.json" % last["seq"])
    def detail(i):
        t = i["status"]
        return {"invocation": i["seq"], "state": t["state"], "rc": t.get("rc"), "signal": t.get("signal"),
                "verdict": t.get("verdict"), "evidence": t.get("evidence"), "reason": t.get("reason")}

    def clean(t):
        return t["state"] == "EXITED" and t.get("rc") == 0 and not t.get("signal") and t.get("verdict") in (None, "COMPLETED")
    # LAUNCH_FAILED means the worker never ran: not failure evidence (a retry reuses that attempt)
    failed = [i for i in invs if i["status"]["state"] == "CANCELLED"
              or (i["status"]["state"] == "EXITED" and not clean(i["status"]))]
    if failed:
        return "failure", detail(failed[-1])
    unsure = [i for i in invs if i["status"]["state"] in ("UNCERTAIN", "CANCELLING")]
    if unsure:
        return "uncertain", detail(unsure[-1])
    return ("success" if clean(s) else "failure"), detail(last)


# ---- output predicates -------------------------------------------------------

def _predicate(o, default_min, size, read, run_validator):
    """Problems of one output against min_bytes, sha256 and the validator."""
    problems = []
    mb = o.get("min_bytes", default_min)
    if not (type(mb) is int and mb >= 0):
        return ["invalid min_bytes"]
    if size < mb:
        problems.append("min_bytes: %d bytes, need %d" % (size, mb))
    sha = o.get("sha256")
    if sha is not None and hashlib.sha256(read()).hexdigest() != sha:
        problems.append("sha256 mismatch")
    v = o.get("validate")
    if v is not None and not problems:
        if not (isinstance(v, str) and v.strip()):
            problems.append("invalid validator")
        else:
            rc = run_validator(v)
            if rc != 0:
                problems.append("validator exited %s" % ("(timeout)" if rc is None else rc))
    return problems


def _validate_runner(cwd, file_path):
    def run(cmd):
        r = E.run_tool(["bash", "-c", cmd], E.worker_env(extras={"OVERSEER_OUTPUT": file_path}), VALIDATE_TIMEOUT, cwd=cwd)
        return r["rc"]
    return run


def _file_sha(f):
    try:
        with open(f, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def code_outputs(wt, candidate, unit):
    """Each declared output at the candidate commit: a regular blob, never a symlink or submodule."""
    results = []
    for o in unit["outputs"]:
        o = o if isinstance(o, dict) else {"path": o}
        p = o["path"]
        problems, rec = [], {}
        ls = W.git(wt, "--literal-pathspecs", "ls-tree", "-z", candidate, "--", p, raw=True)["stdout"]
        ent = [t for t in ls.split(b"\0") if t]
        meta = ent[0].split(b"\t", 1)[0].decode().split() if len(ent) == 1 else None
        if meta is None or ent[0].split(b"\t", 1)[1].decode("utf-8", "replace") != p:
            problems.append("missing at candidate")
        elif meta[1] != "blob" or meta[0] not in ("100644", "100755"):
            problems.append("not a regular file (mode %s, %s)" % (meta[0], meta[1]))
        else:
            sha = meta[2]
            size = int(W.git(wt, "cat-file", "-s", sha)["stdout"].strip())
            def blob(sha=sha):
                return W.git(wt, "cat-file", "blob", sha, raw=True)["stdout"]

            def run_v(cmd, p=p, blob=blob):  # the committed blob in a scratch directory, never the worktree
                tmp = tempfile.mkdtemp(prefix="ovr-validate-")
                try:
                    f = os.path.join(tmp, os.path.basename(p))
                    with open(f, "wb") as fh:
                        fh.write(blob())
                    before = _file_sha(f)
                    rc = _validate_runner(tmp, f)(cmd)
                    return rc if _file_sha(f) == before else "(modified its input)"
                finally:
                    shutil.rmtree(tmp, True)
            problems = _predicate(o, 0, size, blob, run_v)
            rec = {} if problems else {"size": size, "sha256": hashlib.sha256(blob()).hexdigest()}  # of the candidate blob
        results.append(dict({"path": p, "ok": not problems, "problems": problems}, **rec))
    return results


def _tree(out):
    """{relative path: (size, sha256) or ("link", target)} of every file under the outputs directory."""
    base, tree = os.path.realpath(out), {}
    for d, _, files in os.walk(base):
        for name in files:
            f = os.path.join(d, name)
            rel = os.path.relpath(f, base)
            if os.path.islink(f):
                tree[rel] = ("link", os.readlink(f))
            else:
                tree[rel] = (os.path.getsize(f), _file_sha(f))
    return tree


def job_outputs(adir, unit):
    """Each declared output under attempts/a<N>/outputs/ (symlinks must stay inside it).

    Existence, size and digest of every output are decided before any validator runs. Validators
    run on private copies, and afterwards the directory's files must be exactly as they were.
    """
    out = os.path.join(adir, "outputs")
    results = []
    for o in unit["outputs"]:
        o = o if isinstance(o, dict) else {"path": o}
        problems, full = [], None
        try:
            full = paths.safe_relpath(out, o["path"])
        except paths.UnsafePath as e:
            problems.append(str(e))
        if full is not None:
            if not os.path.isfile(full):
                problems.append("missing")
            else:
                def read(f=full):
                    with open(f, "rb") as fh:
                        return fh.read()
                problems = _predicate(dict((k, v) for k, v in o.items() if k != "validate"), 1,
                                      os.path.getsize(full), read, None)
        r = {"path": o["path"], "ok": not problems, "problems": problems, "_full": full, "_o": o}
        if not problems and full is not None:  # recorded at judgment time, so a report can tell a later change
            r["size"], r["sha256"] = os.path.getsize(full), _file_sha(full)
        results.append(r)
    todo = [r for r in results if r["ok"] and r["_o"].get("validate") is not None]
    before = _tree(out) if todo else None
    for r in todo:
        v, full = r["_o"]["validate"], r["_full"]
        if not (isinstance(v, str) and v.strip()):
            r["problems"].append("invalid validator")
            continue
        tmp = tempfile.mkdtemp(prefix="ovr-validate-")
        try:
            f = os.path.join(tmp, os.path.basename(full))
            shutil.copyfile(full, f)  # a private copy: the validator never sees the live output
            rc = _validate_runner(tmp, f)(v)
        finally:
            shutil.rmtree(tmp, True)
        if rc != 0:
            r["problems"].append("validator exited %s" % ("(timeout)" if rc is None else rc))
    if before is not None:
        after = _tree(out)
        changed = set(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        base = os.path.realpath(out)
        declared = set(os.path.relpath(r["_full"], base) for r in results if r["_full"])
        for r in results:
            if changed and (set(before) != set(after) or changed - declared or (r["_full"] and os.path.relpath(r["_full"], base) in changed)):
                r["problems"].append("outputs directory changed while validators ran: " + ",".join(sorted(changed)))
    for r in results:
        r.pop("_full"), r.pop("_o")
        r["ok"] = not r["problems"]
    return results


# ---- judgment ----------------------------------------------------------------

def _record(root, plan_id, unit, n, adir, j):
    j.update({"plan_id": plan_id, "unit_id": unit["id"], "attempt": n, "kind": unit["kind"], "time": store.utc_now()})
    j.setdefault("orchestrator_resolved", False)
    store.write_json_atomic(os.path.join(adir, "judgment.json"), j)
    st = approval.load_unit_state(root, plan_id, unit["id"])
    st["execution"] = {"state": "judged", "attempt": n, "status": j["status"]}
    st["evidence"] = {"judgment": {"attempt": n, "status": j["status"], "candidate": j.get("candidate"),
                                   "time": j["time"], "orchestrator_resolved": j["orchestrator_resolved"]}}
    approval.save_unit_state(root, plan_id, unit["id"], st)
    store.append_event(root, "judged", plan_id=plan_id, unit_id=unit["id"], attempt=n, status=j["status"],
                       candidate=j.get("candidate"), orchestrator_resolved=j["orchestrator_resolved"])
    return j


def _tracked_changes(wt):
    return [p for _, p, _ in W.parse_status(W.git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=no",
                                                  "--ignore-submodules=none", raw=True)["stdout"])]


def _worktree_problems(wt, cand, who):
    """HEAD must still be the candidate, no index flag may hide a change, and no tracked file may differ."""
    out = []
    if W.rev(wt, "HEAD") != cand:
        out.append("head_moved_by_%s" % who)
    flagged = W.index_flags(wt)
    if flagged:
        out.append("index_flags:" + ",".join(flagged))
    else:
        changed = _tracked_changes(wt)
        if changed or W.git(wt, "diff", "--no-ext-diff", "--quiet", "HEAD", "--", ok=(0, 1))["rc"] == 1:
            out.append("%s_modified_tracked:%s" % (who, ",".join(sorted(changed))))
    return out


def _judge_code(root, plan_id, unit, n, adir, cls, ev, timeout):
    j = {"status": "FAILED", "base": None, "candidate": None, "test_rc": None, "outputs": [], "reasons": [],
         "execution": ev, "out_of_scope_dirty": [], "invocation_token": None}
    if cls == "uncertain":  # the author may still be writing: the worktree is left alone
        j["status"] = "UNCERTAIN"
        j["reasons"].append("author execution is uncertain: %s" % (ev.get("reason") or ev.get("state")))
        return j
    if cls == "failure":
        j["reasons"].append("author_failed:state=%s rc=%s signal=%s" % (ev["state"], ev["rc"], ev["signal"]))
        return j
    norm = W.normalize(root, plan_id, unit, adir, n)
    j.update(base=norm["base"], candidate=norm["candidate"], reasons=list(norm["reasons"]),
             out_of_scope_dirty=norm["out_of_scope_dirty"], commits=norm["commits"])
    wt, cand = W.load_workspace(adir)["worktree"], norm["candidate"]
    if not norm["ok"]:
        if cand:  # per-output results are evidence even when the judgment fails
            j["outputs"] = code_outputs(wt, cand, unit)
        return j
    return _check_candidate(j, unit, adir, wt, cand, timeout)


def _check_candidate(j, unit, adir, wt, cand, timeout):
    """Run the unit's test command and output predicates against `cand` (same rules for author and integrated heads)."""
    # the test runs in a clean, disposable detached checkout of the candidate, so untracked or
    # ignored worktree-only files cannot make it pass; the live worktree is checked afterwards
    scratch = tempfile.mkdtemp(prefix="ovr-test-")
    try:
        W.git(wt, "worktree", "add", "--detach", scratch, cand)
        r = E.run_tool(["bash", "-c", unit["test_command"]], E.worker_env(), timeout, cwd=scratch)
        after = _worktree_problems(scratch, cand, "test")
    finally:
        try:
            W.git(wt, "worktree", "remove", "--force", scratch)
        except W.GitError:
            pass
        shutil.rmtree(scratch, True)
        W.git(wt, "worktree", "prune", ok=(0, 128))
    log = os.path.join(adir, "test.log")
    with open(log, "w", encoding="utf-8") as f:
        f.write(r["stdout"] + ("\n--- stderr ---\n" + r["stderr"] if r["stderr"] else ""))
    j["test_rc"], j["test_log"], j["test_clean_checkout"] = r["rc"], log, True
    if r["timed_out"]:
        j["reasons"].append("test_timeout:%gs" % timeout)
    elif r["rc"] != 0:
        j["reasons"].append("test_failed:rc=%s" % r["rc"])
    for why in after + _worktree_problems(wt, cand, "test"):
        if why not in j["reasons"]:
            j["reasons"].append(why)
    j["outputs"] = code_outputs(wt, cand, unit)
    j["reasons"] += ["output_failed:%s: %s" % (o["path"], "; ".join(o["problems"])) for o in j["outputs"] if not o["ok"]]
    j["reasons"] += _worktree_problems(wt, cand, "validator")
    if not j["reasons"]:
        j["status"] = "DONE"
    return j


def rejudge_integrated(root, plan_id, unit, integration_base, previous_candidate, previous_base,
                       test_timeout=TEST_TIMEOUT):
    """Judge the supervisor's merge of the target into the unit branch (never U5 normalization: it rejects merges).

    The worktree must be on the unit branch with no tracked change and HEAD must be a merge of exactly
    `previous_candidate` and `integration_base`. integration.json is written before anything runs; the new
    judgment binds to the merge commit and carries `integrated: true`. The caller holds the project lock.
    """
    if unit["kind"] != "code":
        raise JudgeError("only code units have an integrated head")
    st, n, adir = _unit_attempt(root, plan_id, unit)
    ws = W.load_workspace(adir)
    wt = ws["worktree"]
    if W.git(wt, "symbolic-ref", "-q", "HEAD", ok=(0, 1))["stdout"].strip() != "refs/heads/" + ws["branch"]:
        raise JudgeError("worktree is not on the unit branch %s" % ws["branch"])
    head = W.rev(wt, "HEAD")
    problems = _worktree_problems(wt, head, "supervisor")
    if problems:
        raise JudgeError("worktree is not clean: " + "; ".join(problems))
    if W.git(wt, "rev-list", "--parents", "-n", "1", head)["stdout"].split()[1:] != [previous_candidate, integration_base]:
        raise JudgeError("HEAD %s is not the merge of %s into %s" % (head[:12], integration_base[:12], previous_candidate[:12]))
    prev = X._load(os.path.join(adir, "judgment.json"))
    if not (isinstance(prev, dict) and prev.get("status") == "DONE" and prev.get("candidate") == previous_candidate):
        raise JudgeError("the previous DONE judgment does not bind to %s" % previous_candidate[:12])
    store.write_json_atomic(os.path.join(adir, "integration.json"), {
        "integration_base": integration_base, "previous_candidate": previous_candidate, "previous_base": previous_base,
        "integrated_candidate": head, "time": store.utc_now()})
    j = {"status": "FAILED", "base": prev.get("base"), "candidate": head, "integrated": True,
         "integration_base": integration_base, "previous_candidate": previous_candidate, "test_rc": None,
         "outputs": [], "reasons": [], "execution": prev.get("execution"), "out_of_scope_dirty": [],
         "invocation_token": prev.get("invocation_token")}
    changed = W.zsplit(W.git(wt, "diff", "--name-only", "-z", integration_base, head, raw=True)["stdout"])
    j["reasons"] += ["out_of_scope_after_integration:" + p for p in changed if not W.in_scope(p, unit["scope"])]
    if j["reasons"]:
        j["outputs"] = code_outputs(wt, head, unit)
        return _record(root, plan_id, unit, n, adir, j)
    store.append_event(root, "integrated", plan_id=plan_id, unit_id=unit["id"], attempt=n, previous=previous_candidate,
                       integration_base=integration_base, candidate=head)
    return _record(root, plan_id, unit, n, adir, _check_candidate(j, unit, adir, wt, head, test_timeout))


def _judge_job(unit, adir, cls, ev):
    j = {"status": "FAILED", "base": None, "candidate": None, "test_rc": None, "reasons": [], "execution": ev}
    j["outputs"] = job_outputs(adir, unit)
    failed = ["output_failed:%s: %s" % (o["path"], "; ".join(o["problems"])) for o in j["outputs"] if not o["ok"]]
    if cls == "failure":
        j["reasons"] = ["execution_failed:state=%s rc=%s signal=%s verdict=%s" % (ev["state"], ev["rc"], ev["signal"], ev["verdict"])] + failed
    elif cls == "uncertain":
        j["status"] = "UNCERTAIN"
        j["reasons"] = ["execution is uncertain: %s" % (ev.get("reason") or ev.get("state"))]
    elif failed:
        j["reasons"] = failed
    else:
        j["status"] = "DONE"
    return j


def judge_unit(root, plan_id, unit, test_timeout=TEST_TIMEOUT):
    st, n, adir = _unit_attempt(root, plan_id, unit)
    invs = _invocations(root, plan_id, unit, n)
    cls, ev = execution_evidence(invs)
    if unit["kind"] == "code":
        j = _judge_code(root, plan_id, unit, n, adir, cls, ev, test_timeout)
    else:
        j = _judge_job(unit, adir, cls, ev)
    j["invocation_token"] = (X._load(os.path.join(invs[-1]["path"], "intent.json")) or {}).get("token")
    return _record(root, plan_id, unit, n, adir, j)


def valid_judgment(root, plan_id, unit, n=None):
    """The attempt's judgment while it still binds to the evidence, else None.

    Code: the worktree HEAD must equal the sealed candidate. Job: the latest job invocation
    must be the one that was judged.
    """
    if n is None:
        n = approval.load_unit_state(root, plan_id, unit["id"])["counters"]["attempts"]
    adir = paths.attempt_dir(root, plan_id, unit["id"], n)
    j = X._load(os.path.join(adir, "judgment.json"))
    if not isinstance(j, dict) or j.get("status") not in EXIT:
        return None
    if unit["kind"] == "code":
        if not j.get("candidate"):
            return j if j["status"] != "DONE" else None
        try:
            head = W.rev(W.load_workspace(adir)["worktree"], "HEAD")
        except (X.ExecutionError, OSError):
            return None
        return j if head == j["candidate"] else None
    invs = _invocations(root, plan_id, unit, n)
    last = (X._load(os.path.join(invs[-1]["path"], "intent.json")) or {}).get("token") if invs else None
    return j if last and last == j.get("invocation_token") else None


# ---- resolve -----------------------------------------------------------------

def resolve_unit(root, plan_id, unit, as_, reason, evidence):
    if as_ not in ("done", "failed"):
        raise JudgeError("--as must be done or failed")
    for name, v in (("reason", reason), ("evidence", evidence)):
        if not (isinstance(v, str) and v.strip()):
            raise JudgeError("resolve requires a non-blank --%s" % name)
    st, n, adir = _unit_attempt(root, plan_id, unit)
    invs = _invocations(root, plan_id, unit, n)
    for i in invs:
        if i["status"]["state"] in ("QUEUED", "RUNNING"):
            raise JudgeError("invocation %s is %s; only an UNCERTAIN attempt can be resolved" % (i["seq"], i["status"]["state"]))
    targets = [i for i in invs if i["status"]["state"] in ("UNCERTAIN", "CANCELLING")]
    if not targets:
        states = ", ".join("%s=%s" % (i["seq"], i["status"]["state"]) for i in invs) or "no invocations"
        raise JudgeError("attempt a%d of %s is not UNCERTAIN (%s)" % (n, unit["id"], states))
    j = {"status": "DONE" if as_ == "done" else "FAILED", "base": None, "candidate": None, "test_rc": None,
         "outputs": [], "orchestrator_resolved": True, "reason": reason.strip(), "evidence": evidence.strip(),
         "reasons": ["orchestrator resolved as %s: %s" % (as_, reason.strip())], "resolved_at": store.utc_now(),
         "execution": {"state": "UNCERTAIN", "invocations": [i["seq"] for i in targets]},
         "invocation_token": (X._load(os.path.join(invs[-1]["path"], "intent.json")) or {}).get("token")}
    if as_ == "done":
        live = [i for i in invs if i["status"]["state"] != "RESOLVED"]
        if live and execution_evidence(live)[0] == "failure":
            raise JudgeError("refusing --as done; another invocation has definite failure evidence")
        if unit["kind"] != "job":
            raise JudgeError("code units cannot be resolved as done; resolve the author as failed, or judge a new attempt")
        j["outputs"] = job_outputs(adir, unit)
        bad = ["%s: %s" % (o["path"], "; ".join(o["problems"])) for o in j["outputs"] if not o["ok"]]
        if bad:
            raise JudgeError("refusing --as done; output predicates fail: " + " | ".join(bad))
    # commit order: judgment, unit facets and event first; resolved.json (the slot release) last,
    # so an interrupted resolve is still UNCERTAIN and simply repeats
    _record(root, plan_id, unit, n, adir, j)
    note = {"as": as_, "reason": j["reason"], "evidence": j["evidence"], "time": j["resolved_at"]}
    for i in targets:
        intent = X._load(os.path.join(i["path"], "intent.json"))
        cpath = os.path.join(i["path"], "cancel.json")
        cancel = X._load(cpath)
        if isinstance(cancel, dict) and cancel.get("token") == intent["token"]:
            store.write_json_atomic(cpath, dict(cancel, orchestrator_resolved=note))
        store.write_json_atomic(os.path.join(i["path"], "resolved.json"), dict(note, token=intent["token"]))
    return j


# ---- commands ----------------------------------------------------------------

def _unit_for(root, args):
    pid = args.plan or approval.find_plan_for_unit(root, args.unit)
    approval.require_approval(root, pid)
    return pid, X.refuse_pr_unit(X._unit_of(approval.effective_plan(root, pid), args.unit), "has no judgment")


def configure_judge(p):
    p.add_argument("unit", metavar="UNIT")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")
    p.add_argument("--test-timeout", type=float, default=TEST_TIMEOUT, metavar="S")


def run_judge(args):
    root = paths.find_root(args.root)
    if args.test_timeout <= 0:
        raise JudgeError("--test-timeout must be positive")
    with store.ProjectLock(root, "judge"):
        pid, unit = _unit_for(root, args)
        j = judge_unit(root, pid, unit, args.test_timeout)
    print(json.dumps(j, indent=2, sort_keys=True))
    return EXIT[j["status"]]


def configure_resolve(p):
    p.add_argument("unit", metavar="UNIT")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")
    p.add_argument("--as", dest="as_", required=True, choices=("done", "failed"))
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence", required=True)


def run_resolve(args):
    root = paths.find_root(args.root)
    with store.ProjectLock(root, "resolve"):
        pid, unit = _unit_for(root, args)
        j = resolve_unit(root, pid, unit, args.as_, args.reason, args.evidence)
    print(json.dumps({"unit": unit["id"], "attempt": j["attempt"], "status": j["status"],
                      "orchestrator_resolved": True}, sort_keys=True))
    return 0
