"""Controller: `next`, `dispatch` without a unit, `merge` with integration and closure, owner stops.

Operating assumption (not enforced): no external actor writes the target branch during a merge window.
`--match-head-commit` guards the pull request head, not the target.

Readiness comes from unit closure only: a code unit is closed when its pull request is merged, a job unit
when its judgment is DONE (or orchestrator-resolved done); a process exit never closes anything. `next`
derives every action from disk, with no network call and no state write, so its output is the same before
and after a restart.

`merge` holds the project lock throughout and writes intents before each effect (merge-in commit, push,
`gh pr merge`, issue close). Per unit: `attempts/a<N>/integration_intent.json` (merge-in in progress),
`attempts/a<N>/integration.json` (written by `judge.rejudge_integrated`), `reviews/carry.json` (a review carried
forward by patch id) and `units/<unit>/merge.json` (intent, then `pr_head` and `merge_commit` as separate fields).
Owner stops are raised as `approval.OwnerStop`; `cli.main` records each as an `OWNER_STOP` event.
"""
import hashlib
import json
import os
import sys
import time

from . import approval, execution as X, github as G, judge as J, paths, plan as P, review as R, store, workspace as W

POLL_SECONDS = 60.0
POLL_INTERVAL = 3.0
MERGE_TIMEOUT = 180
MAX_INTEGRATIONS = 3  # integrations per merge call when the target keeps advancing


class ControllerError(X.ExecutionError):
    pass


class NotReady(ControllerError):
    """The unit does not meet a merge requirement; nothing was merged."""


class Conflict(ControllerError):
    """The target does not merge cleanly into the unit branch (INTEGRATION_CONFLICT)."""


# ---- owner stops --------------------------------------------------------------

def owner_problems(root, plan_id):
    """Reasons the owner must decide: approval mismatch, a cap above the approved value, scope widening."""
    try:
        appr = approval.require_approval(root, plan_id)
        plan = approval.effective_plan(root, plan_id)
    except approval.ApprovalError as e:
        return [("plan digest mismatch: %s" if "digest" in str(e) else "approval invalid: %s") % e]
    b = appr.get("boundary") or {}
    approved = b.get("caps") if isinstance(b.get("caps"), dict) else {}
    out = []
    for k, v in sorted(plan["caps"].items()):
        if not (type(v) is int and v <= min(P.CAP_MAX.get(k, 0), approved.get(k, 0))):
            out.append("cap %s=%r is above the approved %r" % (k, v, approved.get(k)))
    for u in plan["units"]:
        for g in u.get("scope", []):
            if not P.scope_covers(b.get("scope") or [], g):
                out.append("unit %s scope %r widens the approved scope" % (u["id"], g))
        c = approval.load_unit_state(root, plan_id, u["id"])["counters"]
        for k in ("attempts", "review_rounds"):
            if type(c.get(k)) is not int or c[k] > plan["caps"][k]:
                out.append("unit %s has %s=%r above the cap %s" % (u["id"], k, c.get(k), plan["caps"][k]))
    if X.slots_in_use(root, False, plan_id) > plan["caps"]["workers"]:
        out.append("more worker slots in use than the approved workers cap")
    return out


def gate(root, plan_id):
    problems = owner_problems(root, plan_id)
    if problems:
        raise approval.OwnerStop(problems[0])


def inside_root(root, path):
    r, real = os.path.realpath(root), os.path.realpath(path)
    if os.path.commonpath([r, real]) != r:
        raise approval.OwnerStop("supervisor write outside the project root: %s" % path)
    return path


# ---- shared reads -------------------------------------------------------------

def is_closed(root, plan_id, unit, st):
    """A code unit is closed by a merged pull request; a job unit by a DONE judgment; a pr unit by an open pull
    request (closure `closed`). `abandoned` closes nothing."""
    state = st["closure"].get("state")
    if state == "merged" or (state == "closed" and unit["kind"] in ("job", "pr")):
        return True
    if unit["kind"] == "job" and X._int(st["counters"]["attempts"]):
        j = J.valid_judgment(root, plan_id, unit, st["counters"]["attempts"])
        return bool(j and j["status"] == "DONE")
    return False


def _issue(root, plan_id, uid):
    rec = store.read_json(G._issues_path(root, plan_id), {})
    return (rec.get("filed") or {}).get(uid)


def _dict(path):
    d = X._load(path)
    if d is X.BAD:
        raise ControllerError("%s is unreadable or not a JSON object" % path)
    return d


def _decision_after(adir, cand):
    """An orchestrator decision for exactly `cand` that was recorded against the current non-vacuity record.

    `record_decision` stores the SHA-256 of nonvacuous.json as it was then (null when none existed). A decision
    carried forward by patch id, or made before the FAIL (null or another digest), never accepts it."""
    dec = _dict(os.path.join(adir, "reviews", "orchestrator_decision.json"))
    if not (dec and dec.get("orchestrator_decision") is True and dec.get("candidate") == cand):
        return False
    digest = dec.get("nonvacuous_sha256")
    try:
        with open(os.path.join(adir, "nonvacuous.json"), "rb") as f:
            return isinstance(digest, str) and digest == hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return False


def nonvacuity_problem(adir, cand, review_state):
    nv = _dict(os.path.join(adir, "nonvacuous.json"))
    if nv is None:
        return "no non-vacuity record (run `nonvacuous`)"
    if nv.get("candidate") != cand:  # never carried across an integration: the target may have changed what the tests need
        return "the non-vacuity record is for another candidate"
    if nv.get("status") in ("PASS", "NOT_APPLICABLE") or (
            nv.get("status") == "FAIL" and review_state["orchestrator_decision"] and _decision_after(adir, cand)):
        return None
    return "non-vacuity %s: the tests pass without the change" % nv.get("status")


def gate_problems(root, plan_id, unit, fresh=False):
    """(valid DONE judgment or None, reasons the unit cannot merge yet).

    With `fresh`, an integrated candidate whose review is eligible gets a non-vacuity run of its own when the
    stored record is for another candidate (merge only; `next` never writes)."""
    j = J.valid_judgment(root, plan_id, unit)
    if not j or j.get("status") != "DONE" or not isinstance(j.get("candidate"), str):
        return None, ["no DONE judgment bound to the worktree HEAD"]
    rs = R.review_state(root, plan_id, unit)
    n = approval.load_unit_state(root, plan_id, unit["id"])["counters"]["attempts"]
    problems = list(rs["reasons"]) if rs["state"] != "ELIGIBLE" else []
    if fresh and j.get("integrated") is True and not problems:
        nv = _dict(os.path.join(paths.attempt_dir(root, plan_id, unit["id"], n), "nonvacuous.json"))
        if nv is None or nv.get("candidate") != j["candidate"]:
            try:
                R.nonvacuous(root, plan_id, unit)
            except R.ReviewError as e:
                raise NotReady("non-vacuity could not run on the integrated candidate: %s" % e)
    nv = nonvacuity_problem(paths.attempt_dir(root, plan_id, unit["id"], n), j["candidate"], rs)
    return j, problems + ([nv] if nv else [])


# ---- integration --------------------------------------------------------------

def _fetch_target(wt, target):
    try:
        W.git(wt, "fetch", "origin", "+refs/heads/%s:refs/remotes/origin/%s" % (target, target), timeout=G.FETCH_TIMEOUT)
    except W.GitError as e:
        raise ControllerError("git fetch origin %s failed: %s" % (target, e))
    return W.rev(wt, "refs/remotes/origin/" + target)


def _merge_in(root, plan_id, unit, wt, adir, target, pend):
    if W.git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD", ok=(0, 1))["rc"] == 0:
        W.git(wt, "merge", "--abort")  # our own interrupted merge: the tree was clean when it began
    # the commit recorded in the intent, never the mutable origin/<target> ref
    r = W.git(wt, *(W.SUPERVISOR + ("merge", "--no-edit", "--no-ff", pend["integration_base"])), ok=(0, 1))
    if r["rc"] == 0:
        return
    if W.git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD", ok=(0, 1))["rc"] != 0:
        raise ControllerError("git merge of %s failed without a merge in progress: %s" % (pend["integration_base"][:12], r["stderr"].strip()[:200]))
    files = W.zsplit(W.git(wt, "diff", "--name-only", "--diff-filter=U", "-z", raw=True)["stdout"])
    W.git(wt, "merge", "--abort")
    os.unlink(os.path.join(adir, "integration_intent.json"))
    detail = {"candidate": pend["previous_candidate"], "integration_base": pend["integration_base"], "files": files}
    store.write_json_atomic(os.path.join(paths.unit_dir(root, plan_id, unit["id"]), "stop.json"),
                            dict(detail, kind="INTEGRATION_CONFLICT", time=store.utc_now()))
    store.append_event(root, "INTEGRATION_CONFLICT", plan_id=plan_id, unit_id=unit["id"], **detail)
    raise Conflict("INTEGRATION_CONFLICT: origin/%s (%s) does not merge into %s (%s); the merge was aborted"
                   % (target, pend["integration_base"][:12], unit["id"], ", ".join(files) or "no file list"))


def _integrate(root, plan_id, unit, wt, adir, target, j):
    """Merge the fetched target into the unit branch when the candidate lacks it. Returns (judgment, base, pend)."""
    ipath = os.path.join(adir, "integration_intent.json")
    base = _fetch_target(wt, target)  # on every entry, first or resumed
    pend = _dict(ipath)
    if pend is not None:
        for k in ("integration_base", "previous_candidate", "previous_base"):
            if not isinstance(pend.get(k), str) or not pend[k]:
                raise ControllerError("%s: field %s is missing or not a string" % (ipath, k))
        if W.git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD", ok=(0, 1))["rc"] == 0:
            W.git(wt, "merge", "--abort")  # our own interrupted merge-in
        dirty = J._worktree_problems(wt, W.rev(wt, "HEAD"), "supervisor")  # same cleanliness rule as first entry
        if dirty:
            raise NotReady("the worktree is not clean: " + "; ".join(dirty))
        if W.rev(wt, "HEAD") == pend["previous_candidate"] and pend["integration_base"] != base:
            pend = dict(pend, integration_base=base, time=store.utc_now())  # merge-in not done: retarget the intent
            store.write_json_atomic(ipath, pend)
            store.append_event(root, "integration_intent", plan_id=plan_id, unit_id=unit["id"], **pend)
    else:
        if W.git(wt, "merge-base", "--is-ancestor", base, j["candidate"], ok=(0, 1))["rc"] == 0:
            return j, base, None
        dirty = J._worktree_problems(wt, j["candidate"], "supervisor")
        if dirty:
            raise NotReady("the worktree is not clean: " + "; ".join(dirty))
        pend = {"integration_base": base, "previous_candidate": j["candidate"],
                "previous_base": R._diff_base(adir, j), "time": store.utc_now()}
        store.write_json_atomic(ipath, pend)  # intent before the merge-in commit
        store.append_event(root, "integration_intent", plan_id=plan_id, unit_id=unit["id"], **pend)
    base = pend["integration_base"]
    if W.rev(wt, "HEAD") == pend["previous_candidate"]:
        _merge_in(root, plan_id, unit, wt, adir, target, pend)
    head = W.rev(wt, "HEAD")
    cur = _dict(os.path.join(adir, "judgment.json"))
    if not (cur and cur.get("integrated") and cur.get("candidate") == head and cur.get("integration_base") == base):
        cur = J.rejudge_integrated(root, plan_id, unit, base, pend["previous_candidate"], pend["previous_base"])
    if cur["status"] != "DONE":
        raise NotReady("the integrated candidate %s is %s: %s" % (head[:12], cur["status"], "; ".join(cur["reasons"][:3])))
    carried, p1, p2 = R.carry_forward(root, plan_id, unit)
    store.append_event(root, "integration_review", plan_id=plan_id, unit_id=unit["id"], candidate=head,
                       carried=carried, patch_id=p1, integrated_patch_id=p2)
    os.unlink(os.path.join(adir, "integration_intent.json"))
    return cur, base, pend


# ---- merge --------------------------------------------------------------------

def _record_merge(root, plan_id, unit, intent, back):
    rec = dict(intent, state="merged", pr_head=back["head_oid"], merge_commit=back["merge_oid"], merged_at=store.utc_now())
    if back["head_oid"] != intent["candidate"] or not back["merge_oid"]:
        store.write_json_atomic(os.path.join(paths.unit_dir(root, plan_id, unit["id"]), "merge.json"),
                                dict(rec, state="unverified"))
        raise ControllerError("pull request #%s is MERGED at head %s with merge commit %s, not the judged candidate %s; "
                              "closure not set" % (intent["number"], back["head_oid"][:12], back["merge_oid"], intent["candidate"][:12]))
    store.write_json_atomic(os.path.join(paths.unit_dir(root, plan_id, unit["id"]), "merge.json"), rec)  # merged record first
    _close(root, plan_id, unit, rec)  # closure last; a rerun that finds the record but no closure runs only this
    return rec


def _close(root, plan_id, unit, rec):
    """Record closure from a verified merged record. Idempotent: only the first call writes and logs."""
    st = approval.load_unit_state(root, plan_id, unit["id"])
    if st["closure"].get("state") == "merged":
        return
    st["closure"] = {"state": "merged", "pr": rec["number"], "merge_commit": rec["merge_commit"], "time": rec["merged_at"]}
    approval.save_unit_state(root, plan_id, unit["id"], st)
    store.append_event(root, "merged", plan_id=plan_id, unit_id=unit["id"], number=rec["number"],
                       pr_head=rec["pr_head"], merge_commit=rec["merge_commit"])


def _merged_record(mrec, path):
    """A merged.json that carries every field closure needs, each of the right type."""
    ok = (type(mrec.get("number")) is int and all(isinstance(mrec.get(k), str) and mrec[k]
                                                  for k in ("candidate", "pr_head", "merge_commit", "merged_at")))
    if not ok:
        raise ControllerError("%s is malformed: a merged record needs number, candidate, pr_head, merge_commit and merged_at" % path)
    return mrec


def sync_tracker(root, plan_id, unit, repo, target):
    """Verify (default branch) or perform (any other target) the issue closure; failure leaves tracker pending."""
    number = _issue(root, plan_id, unit["id"])
    if number is None:
        G.set_tracker(root, plan_id, unit["id"], issue=None, state="none")
        return "none"
    try:
        if G.default_branch(repo) == target:
            end = time.monotonic() + POLL_SECONDS
            while G.view_issue(repo, number)["state"] != "CLOSED":
                if time.monotonic() >= end:
                    raise ControllerError("issue #%d is not CLOSED %gs after the merge" % (number, POLL_SECONDS))
                time.sleep(POLL_INTERVAL)
            via = "merge"
        else:
            G.close_issue(number, "Merged into %s by pull request for unit %s." % (target, unit["id"]), repo, root)
            via = "explicit close"
    except (G.GithubError, ControllerError) as e:
        G.set_tracker(root, plan_id, unit["id"], issue=number, state="pending", error=str(e)[:200])
        store.append_event(root, "tracker_pending", plan_id=plan_id, unit_id=unit["id"], issue=number, error=str(e)[:200])
        return "pending"
    G.set_tracker(root, plan_id, unit["id"], issue=number, state="closed", via=via)
    return "closed"


def _issue_merge(root, plan_id, unit, repo, intent):
    argv = ["pr", "merge", str(intent["number"]), "-R", repo, "--squash", "--match-head-commit", intent["candidate"]]
    r = G.gh(argv, timeout=MERGE_TIMEOUT)
    amb = G.transient(r)
    if amb:
        store.append_event(root, "github_ambiguous", op="pr_merge", number=intent["number"], error=(r["stderr"] or "").strip()[:200])
    try:
        back = G.readback_pr(intent["number"], repo)
    except G.GithubError as e:
        if amb or r["rc"] == 0:  # the merge may have happened and cannot be read back
            raise G.Ambiguous("pull request #%d: unknown outcome of the merge call (%s); rerun 'merge %s'" % (intent["number"], e, unit["id"]))
        raise
    if back["state"] == "MERGED":
        return back
    if amb or r["rc"] == 0:
        raise G.Ambiguous("pull request #%d reads %s after the merge call; rerun 'merge %s' to reconcile"
                          % (intent["number"], back["state"], unit["id"]))
    os.unlink(os.path.join(paths.unit_dir(root, plan_id, unit["id"]), "merge.json"))  # a definite refusal: no intent stands
    raise ControllerError("gh pr merge refused (rc %s): %s" % (r["rc"], (r["stderr"] or "").strip()[:300]))


def merge_unit(root, plan_id, unit):
    gate(root, plan_id)
    if unit["kind"] == "pr":
        raise ControllerError("unit %s is a pr unit; the supervisor never merges its pull request" % unit["id"])
    if unit["kind"] != "code":
        raise ControllerError("unit %s is a job unit; it closes on a DONE judgment (`issues close` files the tracker step)" % unit["id"])
    plan = approval.effective_plan(root, plan_id)
    repo, target = plan["repo"], W.check_branch_name(plan["target_branch"])
    uid = unit["id"]
    st = approval.load_unit_state(root, plan_id, uid)
    mpath = os.path.join(paths.unit_dir(root, plan_id, uid), "merge.json")
    mrec = _dict(mpath)
    if mrec and mrec.get("state") == "merged":  # reconcile a crash between this record and closure: no integrate, merge or review
        _merged_record(mrec, mpath)
        _close(root, plan_id, unit, mrec)
        done = approval.load_unit_state(root, plan_id, uid)["tracker"].get("state") in ("closed", "none")
        return dict(mrec, tracker="closed" if done else sync_tracker(root, plan_id, unit, repo, target))
    if mrec and mrec.get("state") == "intent":  # interrupted: the read-back decides, a MERGED pull request is never merged again
        if type(mrec.get("number")) is not int:
            raise ControllerError("%s is malformed: an intent needs an integer number" % mpath)
        back = G.readback_pr(mrec["number"], repo)
        if back["state"] == "MERGED":
            if not (type(mrec.get("number")) is int and isinstance(mrec.get("candidate"), str)):
                raise ControllerError("%s is malformed: an intent needs number and candidate" % mpath)
            rec = _record_merge(root, plan_id, unit, mrec, back)
            return dict(rec, tracker=sync_tracker(root, plan_id, unit, repo, target))
        if back["state"] == "CLOSED":
            raise ControllerError("pull request #%s is CLOSED without a merge" % mrec["number"])
    n = st["counters"]["attempts"]
    if not X._int(n):
        raise NotReady("unit %s has no attempt" % uid)
    adir = paths.attempt_dir(root, plan_id, uid, n)
    wt = inside_root(root, W.load_workspace(adir)["worktree"])
    pend = _dict(os.path.join(adir, "integration_intent.json"))
    j, problems = gate_problems(root, plan_id, unit, fresh=True)
    if pend is None and problems:
        raise NotReady("; ".join(problems))
    base = None
    if pend is not None or j:
        for _ in range(MAX_INTEGRATIONS):  # each pass fetches; stop once the candidate contains the fetched target
            j, base, pend = _integrate(root, plan_id, unit, wt, adir, target, j or {})
            if pend is None:
                break
            j, problems = gate_problems(root, plan_id, unit, fresh=True)
            if problems:
                raise NotReady("after integration: " + "; ".join(problems))
        else:
            raise NotReady("origin/%s advanced during %d integrations; rerun merge" % (target, MAX_INTEGRATIONS))
    rec = G.open_pr(root, plan_id, unit)  # plain push, create or re-body, and the read-back head must equal the candidate
    cand = j["candidate"]
    latest = _fetch_target(wt, target)  # the race between this fetch and GitHub's merge is an accepted limit
    if W.git(wt, "merge-base", "--is-ancestor", latest, cand, ok=(0, 1))["rc"] != 0:
        raise NotReady("origin/%s advanced to %s after the integration; rerun merge to integrate it" % (target, latest[:12]))
    intent = {"state": "intent", "number": rec["number"], "candidate": cand, "pr_head": rec["head_oid"],
              "integration_base": base, "target": target, "time": store.utc_now()}
    store.write_json_atomic(mpath, intent)
    store.append_event(root, "merge_intent", plan_id=plan_id, unit_id=uid, number=rec["number"], candidate=cand)
    back = _issue_merge(root, plan_id, unit, repo, intent)
    done = _record_merge(root, plan_id, unit, intent, back)
    return dict(done, tracker=sync_tracker(root, plan_id, unit, repo, target))


# ---- next ---------------------------------------------------------------------

def _review_action(adir, plan, cand, reasons=()):
    rounds = R._rounds(adir)
    cap = plan["caps"]["review_rounds"]
    if rounds and any("reviewer session id" in r for r in reasons):  # dispositions cannot repair an invalid reviewer session
        return ("review", "") if len(rounds) < cap else ("stop", "review round r%d used a non-independent reviewer session at the cap" % rounds[-1][0])
    if not rounds:
        return "review", ""
    num, rdir, rd = rounds[-1]
    if rd.get("state") != "complete":
        return ("review", "--retry") if not rd.get("retried") else (
            ("review", "") if len(rounds) < cap else ("stop", "review round r%d has no verdict at the cap" % num))
    if rd.get("candidate") not in R.equivalent_candidates(adir, cand):
        return ("review", "") if len(rounds) < cap else ("stop", "review cap reached: an orchestrator decision is needed")
    disp = _dict(os.path.join(rdir, "dispositions.json"))
    if not disp or disp.get("candidate") not in R.equivalent_candidates(adir, cand):
        return "dispositions", "round r%d" % num
    return ("fix", "round r%d" % num) if len(rounds) < cap else ("stop", "review cap reached with blocking findings: an orchestrator decision is needed")


def _unit_action(root, plan, u, st, free, after_quota=False):
    """(action, detail) for one unit or None."""
    pid, uid, n = plan["plan_id"], u["id"], st["counters"]["attempts"]
    if st["closure"].get("state") == "abandoned":
        return None
    blocked = [d for d in u["needs"] if approval.load_unit_state(root, pid, d)["closure"].get("state") == "abandoned"]
    if blocked:
        return "stop", "needs %s, which is abandoned and never merged; an orchestrator decision is needed" % ", ".join(blocked)
    if is_closed(root, pid, u, st):
        if _issue(root, pid, uid) and st["tracker"].get("state") not in ("closed", "none"):
            return "tracker", ("run `issues close`" if u["kind"] in ("job", "pr") else "rerun `merge` to retry the tracker step")
        return None
    if u["kind"] == "pr":  # no worker, attempt or slot: ready once its needs are closed
        if not all(is_closed(root, pid, X._unit_of(plan, d), approval.load_unit_state(root, pid, d)) for d in u["needs"]):
            return None
        return "pr", "open the pull request for %s into %s" % (u["head_branch"], u["base_branch"])
    active = X.active_invocations(root, pid, uid, False)
    if active:
        return "wait", "%s %s" % (active[0]["seq"], active[0]["status"]["state"])
    adir = paths.attempt_dir(root, pid, uid, n) if X._int(n) else None
    invs = [X.invocation_status(i["path"], False)["state"] for i in X.iter_invocations(root, pid, uid) if i["attempt"] == n]
    if not X._int(n) or all(s in ("NOT_LAUNCHED", "LAUNCH_FAILED") for s in invs):
        if not all(is_closed(root, pid, X._unit_of(plan, d), approval.load_unit_state(root, pid, d)) for d in u["needs"]):
            return None
        cls = _dict(os.path.join(adir, "classification.json")) if adir else None
        if cls and cls.get("class") in X.BLOCKING_CLASSES and not after_quota:
            return "stop", "attempt a%d ended with %s; an orchestrator decision is needed" % (n, cls["class"])
        return ("dispatch", "") if free[0] > 0 and free[1] > 0 else None
    j = J.valid_judgment(root, pid, u, n)
    if os.path.exists(os.path.join(adir, "integration_intent.json")) and (j is None or j["status"] == "DONE"):
        return "merge", "resume the integration"
    if j is None:
        return "judge", "attempt a%d" % n
    if j["status"] == "UNCERTAIN":
        return "resolve", "attempt a%d is UNCERTAIN" % n
    if j["status"] == "FAILED":
        if n >= plan["caps"]["attempts"]:
            return "stop", "attempt a%d FAILED at the attempts cap" % n
        if not (free[0] > 0 and free[1] > 0):  # a retry needs a plan slot and a project slot, like a dispatch
            return None
        return "retry", "attempt a%d FAILED: %s" % (n, (j.get("reasons") or ["no reason"])[0])
    if u["kind"] == "job":
        return None
    stop = _dict(os.path.join(paths.unit_dir(root, pid, uid), "stop.json"))
    if stop and stop.get("candidate") == j["candidate"]:
        return "stop", "%s: %s" % (stop.get("kind"), ", ".join(stop.get("files") or []))
    rs = R.review_state(root, pid, u)
    if rs["state"] != "ELIGIBLE":
        return _review_action(adir, plan, j["candidate"], rs["reasons"])
    nv = nonvacuity_problem(adir, j["candidate"], rs)
    if nv:
        return ("nonvacuous", "") if "no non-vacuity" in nv or "another candidate" in nv else ("stop", nv)
    pr = _dict(os.path.join(paths.unit_dir(root, pid, uid), "pr.json"))
    if not pr or pr.get("candidate") != j["candidate"]:
        return "pr", ""
    return "merge", "PR #%s" % pr.get("number")


def compute_actions(root, only_plan=None, write=False, after_quota=False, exclude=()):
    acts = []
    pdir = paths.plans_dir(root)
    total = [X.slots_in_use(root, write)]
    for pid in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        if not (paths.valid_id(pid) and os.path.exists(os.path.join(pdir, pid, "plan.json"))) or only_plan not in (None, pid):
            continue
        problems = owner_problems(root, pid)
        if problems:
            acts += [{"plan": pid, "unit": None, "action": "stop", "detail": p} for p in problems]
            continue
        plan = approval.effective_plan(root, pid)
        free = [plan["caps"]["workers"] - X.slots_in_use(root, write, pid), X.PROJECT_SLOTS - total[0]]
        for u in plan["units"]:
            if (pid, u["id"]) in exclude:  # a launch already skipped in this dispatch: its slot goes to the next unit
                continue
            a = _unit_action(root, plan, u, approval.load_unit_state(root, pid, u["id"]), free, after_quota)
            if a:
                acts.append({"plan": pid, "unit": u["id"], "action": a[0], "detail": a[1]})
                if a[0] in ("dispatch", "retry"):
                    free[0] -= 1
                    free[1] -= 1
                    total[0] += 1
    return acts


def format_action(a):
    line = "%s %s" % (a["action"], a["unit"] or "plan:" + a["plan"])
    return line + (": " + a["detail"] if a["detail"] else "")


def configure_next(p):
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")


def run_next(args):
    acts = compute_actions(paths.find_root(args.root), args.plan)
    if args.as_json:
        print(json.dumps(acts, indent=2, sort_keys=True))
    else:
        for a in acts:
            print(format_action(a))
    return 0


# ---- dispatch -----------------------------------------------------------------

def report_owner_stops(root, command, plan_id, problems):
    for text in problems:
        sys.stderr.write("OWNER_STOP: %s: %s\n" % (plan_id, text))
        store.append_event(root, "OWNER_STOP", command=command, plan_id=plan_id, reason=text)


def dispatch_ready(root, plan_id=None, after_quota=False):
    """Dispatch every READY unit and every retryable failed unit up to the free slots (plan cap and the project limit).

    A retry stops at the attempts cap (acquire_attempt refuses a new attempt past it). Each owner stop is recorded
    as an OWNER_STOP event and printed on stderr; the exit code is non-zero when any exists. A skipped launch
    (slots full, or an error) is excluded and the actions are recomputed from disk, so the slot it had reserved
    goes to the next ready unit; passes continue until one launches or skips nothing new."""
    rc = 0
    skipped = set()
    stops_seen = set()
    with store.ProjectLock(root, "dispatch"):
        for _ in range(_unit_count(root, plan_id) + 1):
            again = False
            for a in compute_actions(root, plan_id, True, after_quota, skipped):
                if a["action"] == "stop" and a["unit"] is None:  # a plan-level owner stop (unit-level stops are `next` output only)
                    if (a["plan"], a["detail"]) not in stops_seen:
                        stops_seen.add((a["plan"], a["detail"]))
                        report_owner_stops(root, "dispatch", a["plan"], [a["detail"]])
                    rc = 1
                    continue
                if a["action"] not in ("dispatch", "retry"):
                    continue
                plan = approval.effective_plan(root, a["plan"])
                u = X._unit_of(plan, a["unit"])
                try:
                    if u["kind"] == "code":
                        rec = W.dispatch_code(root, a["plan"], u, after_quota)
                    elif u["kind"] == "pr":
                        continue  # unreachable: no pr unit yields a dispatch or retry action
                    else:
                        rec = X.dispatch_job(root, a["plan"], u, after_quota)
                except X.SlotsFull as e:  # skip this unit; other plans or units may still have a slot
                    sys.stderr.write("skipped %s: %s\n" % (a["unit"], e))
                    skipped.add((a["plan"], a["unit"]))
                    again = True
                    continue
                except X.ExecutionError as e:
                    sys.stderr.write("error: %s: %s\n" % (a["unit"], e))
                    rc = 1
                    skipped.add((a["plan"], a["unit"]))
                    again = True
                    continue
                print(json.dumps({"plan": a["plan"], "unit": a["unit"], "attempt": rec["attempt"], "invocation": rec["seq"]}, sort_keys=True))
            if not again:
                break
    return rc


def _unit_count(root, only_plan):
    n = 0
    for p in _plan_ids(root, only_plan):
        try:
            n += len(approval.effective_plan(root, p)["units"])
        except approval.ApprovalError:  # an owner stop: compute_actions reports it and launches nothing for the plan
            pass
    return n


def _plan_ids(root, only_plan):
    pdir = paths.plans_dir(root)
    return [p for p in (sorted(os.listdir(pdir)) if os.path.isdir(pdir) else [])
            if paths.valid_id(p) and os.path.exists(os.path.join(pdir, p, "plan.json")) and only_plan in (None, p)]


# ---- merge command ------------------------------------------------------------

def configure_merge(p):
    p.add_argument("unit", metavar="UNIT")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")


def run_merge(args):
    root = paths.find_root(args.root)
    try:
        with store.ProjectLock(root, "merge"):
            pid = args.plan or approval.find_plan_for_unit(root, args.unit)
            res = merge_unit(root, pid, X._unit_of(approval.effective_plan(root, pid), args.unit))
    except G.Ambiguous as e:
        sys.stderr.write("ambiguous: %s\n" % e)
        return 3
    except Conflict as e:
        sys.stderr.write(str(e) + "\n")
        return 5
    except (NotReady, G.GithubError, ControllerError) as e:
        sys.stderr.write("not merged: %s\n" % e)
        return 1
    print(json.dumps(dict((k, res.get(k)) for k in ("number", "candidate", "pr_head", "merge_commit", "tracker")), sort_keys=True))
    return 0
