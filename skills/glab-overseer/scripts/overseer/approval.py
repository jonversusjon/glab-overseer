"""Plan approval, unit state and splits.

Files under `.overseer/plans/<plan_id>/`:
  plan.json            normalized approved plan (never rewritten except by re-approval)
  issues.json          {"drafts": {unit: {title, body}}, "filed": {unit: number}}; only
                       "drafts" enters the digest
  approval.json        approver, time, digest, boundary
  approval_history.json  superseded approvals
  revisions.json       splits; the effective plan is plan.json plus these revisions
  units/<unit>/unit.json operational facets; never part of the digest

Functions that write state expect the caller to hold `store.ProjectLock`.
"""
import copy
import hashlib
import json
import os

from . import paths, plan as P, store

CLOSED_STATES = ("merged", "closed", "abandoned", "split")
FACETS = ("authorization", "execution", "evidence", "closure", "tracker")


class PlanInvalid(Exception):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


class ApprovalError(Exception):
    pass


class PlanStateError(Exception):
    pass


class OwnerStop(Exception):
    """A change the approval does not cover. The orchestrator stops and asks the owner."""

    def __init__(self, message):
        super().__init__("OWNER_STOP: " + message)


def _p(root, plan_id, name):
    return os.path.join(paths.plan_dir(root, plan_id), name)


# ---- unit state -----------------------------------------------------------

def default_unit_state(plan_id, unit_id, digest=None):
    return {
        "plan_id": plan_id, "unit_id": unit_id,
        "authorization": {"state": "approved", "plan_digest": digest},
        "execution": {"state": "pending"},
        "evidence": {},
        "closure": {"state": "open"},
        "tracker": {"issue": None},
        "lineage": None,
        "counters": {"attempts": 0, "review_rounds": 0},
    }


def unit_state_path(root, plan_id, unit_id):
    return os.path.join(paths.unit_dir(root, plan_id, unit_id), "unit.json")


def load_unit_state(root, plan_id, unit_id):
    st = store.read_json(unit_state_path(root, plan_id, unit_id), None)
    base = default_unit_state(plan_id, unit_id)
    if st is None:
        return base
    for k, v in base.items():
        st.setdefault(k, v)
    st["counters"] = dict(base["counters"], **st["counters"])
    return st


def save_unit_state(root, plan_id, unit_id, state):
    paths.ensure_state_dir(root)
    store.write_json_atomic(unit_state_path(root, plan_id, unit_id), state)


# ---- approval -------------------------------------------------------------

def _load_stored(root, plan_id):
    plan = store.read_json(_p(root, plan_id, "plan.json"), None)
    issues = store.read_json(_p(root, plan_id, "issues.json"), None)
    approval = store.read_json(_p(root, plan_id, "approval.json"), None)
    return plan, issues, approval


def approve_plan(root, plan, approver):
    """Validate, normalize and approve. Returns a result dict; raises PlanInvalid."""
    approver = approver.strip() if isinstance(approver, str) else ""
    if not approver:
        raise ApprovalError("approver must not be blank")
    errors, warnings = P.validate_plan(plan)
    if errors:
        raise PlanInvalid(errors)
    norm = P.normalize_plan(plan)
    drafts = P.render_drafts(norm)
    digest = P.approval_digest(norm, drafts)
    pid = norm["plan_id"]
    old_plan, old_issues, prev = _load_stored(root, pid)
    result = {"plan_id": pid, "digest": digest, "warnings": warnings,
              "titles": [drafts[u["id"]]["title"] for u in norm["units"]], "status": "approved"}

    if prev and prev.get("digest") == digest and old_plan == norm \
            and (old_issues or {}).get("drafts") == drafts:
        result["status"] = "unchanged"
        return result

    paths.ensure_state_dir(root)
    filed = {}
    if prev and prev.get("digest") != digest:
        result["status"] = "updated"
        old_revs = store.read_json(_p(root, pid, "revisions.json"), [])
        hist = store.read_json(_p(root, pid, "approval_history.json"), [])
        if not hist or hist[-1]["approval"].get("digest") != prev.get("digest"):
            hist.append({"approval": prev, "superseded_at": store.utc_now(), "superseded_by": digest,
                         "filed": (old_issues or {}).get("filed", {}), "revisions": old_revs})
            store.write_json_atomic(_p(root, pid, "approval_history.json"), hist)
        ids = set(u["id"] for u in norm["units"])
        filed = dict((k, v) for k, v in (old_issues or {}).get("filed", {}).items() if k in ids)
        # Reset unit state first, so a crash later converges on retry.
        _archive_stale_units(root, pid, norm, prev, old_plan, old_revs)
        try:
            os.unlink(_p(root, pid, "revisions.json"))
        except FileNotFoundError:
            pass
    elif old_issues:
        filed = old_issues.get("filed", {})

    store.write_json_atomic(_p(root, pid, "plan.json"), norm)
    store.write_json_atomic(_p(root, pid, "issues.json"), {"drafts": drafts, "filed": filed})
    for u in norm["units"]:
        st = load_unit_state(root, pid, u["id"])
        st["authorization"] = {"state": "approved", "plan_digest": digest}
        save_unit_state(root, pid, u["id"], st)
    store.write_json_atomic(_p(root, pid, "approval.json"), {
        "plan_id": pid, "approver": approver, "approved_at": store.utc_now(),
        "digest": digest, "boundary": P.boundary(norm)})
    store.append_event(root, "plan_approved", plan_id=pid, approver=approver,
                       digest=digest, status=result["status"])
    return result


def _archive_stale_units(root, plan_id, norm, prev, old_plan, old_revs):
    """Archive (so they restart fresh) the state of every unit that cannot carry over.

    Everything is archived when repo or target_branch changed. Otherwise a unit is
    archived when it is gone, differs from (or is absent in) the previous effective
    plan (original plus revisions), or is still marked split. Idempotent.
    """
    udir = os.path.join(paths.plan_dir(root, plan_id), "units")
    present = sorted(os.listdir(udir)) if os.path.isdir(udir) else []
    b = (prev or {}).get("boundary") or {}
    if (b.get("repo"), b.get("target_branch")) != (norm["repo"], norm["target_branch"]):
        stale = present
    else:
        new = dict((u["id"], u) for u in norm["units"])
        old = old_plan or {"units": []}
        for r in old_revs:
            old = apply_revision(old, r)
        old = dict((u["id"], u) for u in old["units"])
        stale = [n for n in present if n not in new or old.get(n) != new[n]
                 or load_unit_state(root, plan_id, n)["closure"].get("state") == "split"]
    if not stale:
        return
    stamp = store.utc_now().replace(":", "").replace("-", "")
    dest = os.path.join(paths.plan_dir(root, plan_id), "archive", stamp)
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(paths.plan_dir(root, plan_id), "archive", "%s-%d" % (stamp, n))
    os.makedirs(dest)
    for name in stale:
        os.rename(os.path.join(udir, name), os.path.join(dest, name))


def require_approval(root, plan_id):
    """Return the approval record, or raise when plan or drafts differ from it."""
    paths.check_id(plan_id, "plan id")
    plan, issues, approval = _load_stored(root, plan_id)
    if approval is None or plan is None or issues is None:
        raise ApprovalError("plan %s has no complete approval" % plan_id)
    errors, _ = P.validate_plan(plan)
    if errors:
        raise ApprovalError("stored plan %s no longer validates: %s" % (plan_id, errors[0]))
    if P.approval_digest(plan, issues.get("drafts")) != approval.get("digest"):
        raise ApprovalError("stored plan or issue drafts for %s no longer match the approval digest; "
                            "re-run 'plan approve'" % plan_id)
    _verified_plan(root, plan_id, plan, approval)
    return approval


# ---- revisions and splits -------------------------------------------------

def load_revisions(root, plan_id):
    return store.read_json(_p(root, plan_id, "revisions.json"), [])


def revision_digest(approval_digest, rev):
    body = dict((k, v) for k, v in rev.items() if k != "revision_digest")
    return hashlib.sha256((approval_digest + P.canonical_json(body)).encode("utf-8")).hexdigest()


def apply_revision(plan, rev):
    child_ids = [c["id"] for c in rev["children"]]
    units = []
    for u in plan["units"]:
        if u["id"] == rev["parent"]:
            units.extend(copy.deepcopy(rev["children"]))
            continue
        u = copy.deepcopy(u)
        if rev["parent"] in u["needs"]:
            u["needs"] = sorted(set([n for n in u["needs"] if n != rev["parent"]] + child_ids))
        units.append(u)
    out = dict(plan)
    out["units"] = units
    return out


def _verified_plan(root, plan_id, plan, approval):
    """Apply every revision after recomputing its digest and re-running the split checks."""
    for rev in load_revisions(root, plan_id):
        if rev.get("revision_digest") != revision_digest(approval["digest"], rev):
            raise ApprovalError("revision %s of plan %s does not match its digest" % (rev.get("seq"), plan_id))
        parent = next((u for u in plan["units"] if u["id"] == rev.get("parent")), None)
        if parent is None:
            raise ApprovalError("revision %s of plan %s splits unknown unit %r"
                                % (rev.get("seq"), plan_id, rev.get("parent")))
        try:
            check_split(parent, rev["children"])
        except OwnerStop as e:
            raise ApprovalError("revision %s of plan %s fails its split checks: %s" % (rev.get("seq"), plan_id, e))
        plan = apply_revision(plan, rev)
        errors, _ = P.validate_plan(plan)
        if errors:
            raise ApprovalError("revision %s of plan %s yields an invalid plan: %s"
                                % (rev.get("seq"), plan_id, errors[0]))
    return plan


def effective_plan(root, plan_id):
    """The approved plan with all recorded splits applied (verified; see `_verified_plan`)."""
    plan = store.read_json(_p(root, plan_id, "plan.json"))
    approval = store.read_json(_p(root, plan_id, "approval.json"), None)
    if approval is None:
        raise ApprovalError("plan %s has no approval" % plan_id)
    return _verified_plan(root, plan_id, plan, approval)


def find_plan_for_unit(root, unit_id):
    pdir = paths.plans_dir(root)
    found = []
    for pid in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        if paths.valid_id(pid) and os.path.exists(_p(root, pid, "plan.json")):
            if any(u["id"] == unit_id for u in effective_plan(root, pid)["units"]):
                found.append(pid)
    if len(found) != 1:
        raise PlanStateError("unit %r is in %d plans; pass --plan PLAN_ID" % (unit_id, len(found)))
    return found[0]


_INHERIT = ("kind", "prompt", "claims", "test_command", "command", "slurm", "needs", "scope")


def _build_children(parent, spec):
    if parent["kind"] == "pr":
        raise PlanStateError("unit %s is a pr unit; a pr unit opens one pull request and cannot be split" % parent["id"])
    items = spec.get("children") if isinstance(spec, dict) else spec
    if not isinstance(items, list) or not items:
        raise PlanStateError("split spec needs a non-empty list of children")
    kids = []
    for item in items:
        if not isinstance(item, dict):
            raise PlanStateError("each child must be an object")
        cid = item.get("id")
        if not paths.valid_id(cid):
            raise PlanStateError("child unit id %r does not match %s" % (cid, paths.ID_PATTERN))
        if item.get("kind", parent["kind"]) != parent["kind"]:
            raise OwnerStop("child %s changes the unit kind of %s" % (cid, parent["id"]))
        if not (isinstance(item.get("outputs"), list) and item["outputs"]):
            raise PlanStateError("child %s must declare outputs as a non-empty list" % cid)
        ppred = dict((o["path"], o) for o in parent["outputs"])
        raw = dict((k, copy.deepcopy(parent[k])) for k in _INHERIT if k in parent)
        raw.update(copy.deepcopy(item))
        raw["title"] = item.get("title") or "%s (%s)" % (parent["title"], cid)
        outs = []
        for o in item["outputs"]:
            p = P.output_path(o)
            outs.append(dict(ppred[p]) if isinstance(o, str) and p in ppred else o)
        raw["outputs"] = outs
        errors = []
        known = P.UNIT_KEYS[parent["kind"]]  # a split spec's extra keys were always ignored; they stay ignored
        P._validate_unit(dict((k, v) for k, v in dict(raw, slurm=None).items() if k in known),
                         "child %s" % cid, [], None, errors)
        if errors:
            raise PlanStateError(errors[0])
        kids.append(P.normalize_unit(raw))
    return kids


def check_split(parent, kids):
    """Refuse (OwnerStop) any child set that grows or weakens the parent."""
    if parent["kind"] == "pr":
        raise OwnerStop("unit %s is a pr unit and cannot be split" % parent["id"])
    pout = dict((o["path"], o) for o in parent["outputs"])
    pclaims = parent.get("claims", [])
    covered, claims_covered = set(), set()
    for k in kids:
        kid = k["id"]
        if k["kind"] != parent["kind"]:
            raise OwnerStop("child %s changes the unit kind of %s" % (kid, parent["id"]))
        if k.get("test_command") != parent.get("test_command"):
            raise OwnerStop("child %s changes test_command of %s" % (kid, parent["id"]))
        if k.get("slurm") != parent.get("slurm"):
            raise OwnerStop("child %s changes the slurm block of %s" % (kid, parent["id"]))
        if k.get("command") != parent.get("command"):
            raise OwnerStop("child %s changes command of %s" % (kid, parent["id"]))
        dropped = sorted(set(parent["needs"]) - set(k["needs"]))
        if dropped:
            raise OwnerStop("child %s drops need(s) %s of %s" % (kid, ", ".join(dropped), parent["id"]))
        extra = [c for c in k.get("claims", []) if c not in pclaims]
        if extra:
            raise OwnerStop("child %s has claim(s) not in parent %s: %r" % (kid, parent["id"], extra))
        claims_covered.update(k.get("claims", []))
        for o in k["outputs"]:
            if o["path"] not in pout:
                raise OwnerStop("child %s adds output %r beyond parent %s" % (kid, o["path"], parent["id"]))
            if o != pout[o["path"]]:
                raise OwnerStop("child %s changes the predicate of output %r (parent %s)"
                                % (kid, o["path"], parent["id"]))
            covered.add(o["path"])
        for g in k.get("scope", []):
            if not P.scope_covers(parent.get("scope", []), g):
                raise OwnerStop("child %s adds scope %r beyond parent %s" % (kid, g, parent["id"]))
    lost = sorted(set(pout) - covered)
    if lost:
        raise OwnerStop("split of %s drops parent output(s) %s" % (parent["id"], ", ".join(map(repr, lost))))
    lost = [c for c in pclaims if c not in claims_covered]
    if lost:
        raise OwnerStop("split of %s drops parent claim(s) %r" % (parent["id"], lost))


def _apply_split_state(root, plan_id, rev, digest):
    """Create missing child states, then mark the parent split last. Idempotent."""
    pstate = load_unit_state(root, plan_id, rev["parent"])
    for k in rev["children"]:
        if not os.path.exists(unit_state_path(root, plan_id, k["id"])):
            st = default_unit_state(plan_id, k["id"], digest)
            st["lineage"] = {"parent": rev["parent"], "reason": rev["reason"], "time": rev["time"]}
            st["counters"] = dict(pstate["counters"])
            st["authorization"]["via"] = "split"
            save_unit_state(root, plan_id, k["id"], st)
    if pstate["closure"].get("state") != "split":
        pstate["closure"] = {"state": "split", "children": [k["id"] for k in rev["children"]],
                             "time": rev["time"]}
        save_unit_state(root, plan_id, rev["parent"], pstate)


def split_unit(root, plan_id, unit_id, spec, reason):
    """Replace an unclosed unit with children. Returns the revision record."""
    if not isinstance(reason, str) or not reason.strip():
        raise PlanStateError("split requires a non-blank reason")
    digest0 = require_approval(root, plan_id)["digest"]
    for r in load_revisions(root, plan_id):  # finish any interrupted split
        _apply_split_state(root, plan_id, r, digest0)
    eff = effective_plan(root, plan_id)
    parent = next((u for u in eff["units"] if u["id"] == unit_id), None)
    if parent is None:
        raise PlanStateError("unit %r is not in plan %s (already split?)" % (unit_id, plan_id))
    if parent["kind"] == "pr":
        raise PlanStateError("unit %s is a pr unit; a pr unit opens one pull request and cannot be split" % unit_id)
    pstate = load_unit_state(root, plan_id, unit_id)
    if pstate["closure"].get("state") == "split":  # marked split but no revision names it
        pstate["closure"] = {"state": "open"}
    if pstate["closure"].get("state") in CLOSED_STATES:
        raise PlanStateError("unit %s is closed (%s); only unclosed units can be split"
                             % (unit_id, pstate["closure"].get("state")))
    kids = _build_children(parent, spec)
    ever = set(u["id"] for u in store.read_json(_p(root, plan_id, "plan.json"))["units"])
    for r in load_revisions(root, plan_id):
        ever.update(c["id"] for c in r["children"])
    seen = set()
    for k in kids:
        if k["id"] in ever or k["id"] in seen:
            raise PlanStateError("child unit id %r is already used in plan %s" % (k["id"], plan_id))
        seen.add(k["id"])
    check_split(parent, kids)

    now = store.utc_now()
    rev = {"seq": len(load_revisions(root, plan_id)) + 1, "time": now, "parent": unit_id,
           "reason": reason.strip(), "children": kids,
           "pre_split_closure": pstate["closure"],
           "drafts": dict((k["id"], P.render_draft(plan_id, k)) for k in kids)}
    errors, _ = P.validate_plan(apply_revision(eff, rev))
    if errors:
        raise PlanStateError("split produces an invalid plan: " + "; ".join(errors))

    digest = require_approval(root, plan_id)["digest"]
    rev["revision_digest"] = revision_digest(digest, rev)
    store.write_json_atomic(_p(root, plan_id, "revisions.json"), load_revisions(root, plan_id) + [rev])
    _apply_split_state(root, plan_id, rev, digest)
    store.append_event(root, "unit_split", plan_id=plan_id, parent=unit_id,
                       children=[k["id"] for k in kids], reason=rev["reason"])
    return rev


# ---- show -----------------------------------------------------------------

def describe_plan(root, plan_id):
    paths.check_id(plan_id, "plan id")
    plan, issues, approval = _load_stored(root, plan_id)
    if plan is None or approval is None:
        raise PlanStateError("plan %s is not approved" % plan_id)
    try:
        require_approval(root, plan_id)
        verdict = "valid"
    except ApprovalError as e:
        verdict = "INVALID: %s" % e
    lines = ["plan: %s" % plan_id, "digest: %s" % approval["digest"],
             "approver: %s at %s" % (approval["approver"], approval["approved_at"]),
             "approval: %s" % verdict]
    try:
        units = effective_plan(root, plan_id)["units"]
    except ApprovalError:
        units = plan["units"]
    lines.append("units: %d" % len(units))
    for u in units:
        st = load_unit_state(root, plan_id, u["id"])
        lines.append("unit %s [%s] %s" % (u["id"], u["kind"], u["title"]))
        for f in FACETS + ("lineage", "counters"):
            lines.append("  %s: %s" % (f, json.dumps(st[f], sort_keys=True)))
    return lines
