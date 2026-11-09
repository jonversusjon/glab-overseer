"""Cross-family review, reproduction, adjudication and non-vacuity checks.

Layout under an attempt: `reviews/r<R>/` holds `input.md`, `round.json` (state, candidate, calls),
`review.json` (the locally validated review), `dispositions.json`, and one `c<K>/` directory per
reviewer call (task file, argv, raw stdout and stderr, parsed result). `evidence/rep-<K>.json`
holds reproduction records; `nonvacuous.json` the non-vacuity record.

A review verdict means the named reviewer did not refute the claims; it does not establish
correctness. Every record binds to the candidate head. The reviewer runs synchronously with no
worker slot, in an empty scratch directory outside the project; the project lock is held only
around state writes (intent before the model call, result after). A round whose call outcome is
unknown is never re-run silently: it needs `--retry`, once.
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import tempfile

from . import adapters, approval, env as E, execution as X, judge as J, models, paths, store, workspace as W

DEFAULT_TIMEOUT = 900.0
MAX_INPUT = 150000
COUNTER_CLAIM = "This change cannot make an honest run fail."
RULINGS = ("dismissed", "fixed", "nonblocking", "blocked", "inconclusive")
NEEDS_EVIDENCE = ("dismissed", "fixed", "nonblocking")
DEFAULT_TEST_GLOBS = ("tests/**", "test_*.py", "*_test.py")
FINDING_KEYS = ("id", "severity", "claim", "file", "line", "description", "reproduction")
_STR = {"type": "string"}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": list(FINDING_KEYS),
            "properties": {"id": _STR, "severity": {"type": "string", "enum": ["blocking", "nonblocking"]},
                           "claim": _STR, "file": _STR, "line": {"type": ["integer", "null"]},
                           "description": _STR, "reproduction": _STR}}}}}
INSTRUCTIONS = """You are an adversarial code reviewer. Try to refute the claims below and the counter-claim.
Judge only what the diff and the stated test result show; do not assume tests that are not shown.
Report each concrete defect as a finding: `severity` is blocking when the change is wrong or breaks a claim,
nonblocking otherwise; give the file, the line (null if unknown), a description, and a command or steps that
reproduce it (empty string if none). Use verdict FAIL when you report any finding you consider blocking
or cannot rule out; PASS only when you found nothing that refutes the claims. A FAIL needs at least one finding.
Reply with a single JSON object {"verdict": "PASS"|"FAIL", "findings": [...]} and nothing else.

"""


class ReviewError(X.ExecutionError):
    pass


# ---- records -----------------------------------------------------------------

def _obj(path):
    d = X._load(path)
    if d is X.BAD:
        raise ReviewError("%s is unreadable or not a JSON object" % path)
    return d


def _aborted(r):
    return {"round": r, "state": "aborted", "candidate": None, "calls": [], "retried": False, "verdict": None,
            "same_family_fallback": False, "reason": "round directory had no round.json (interrupted before the reviewer ran)"}


def _rounds(adir):
    """[(R, directory, round.json)] in order; a round directory without a readable round.json is refused."""
    base = os.path.join(adir, "reviews")
    out = []
    for name in sorted(os.listdir(base) if os.path.isdir(base) else [], key=lambda s: (len(s), s)):
        m = re.match(r"^r([1-9][0-9]*)$", name)
        if m:
            rd = _obj(os.path.join(base, name, "round.json"))
            if rd is None:  # crashed after mkdir, before round.json: no reviewer was invoked
                rd = _aborted(int(m.group(1)))
            out.append((int(m.group(1)), os.path.join(base, name), rd))
    return out


def _current(root, plan_id, unit):
    """(judgment, attempt number, attempt dir, worktree) of a valid DONE code judgment, else refuse."""
    if unit["kind"] != "code":
        raise ReviewError("unit %s is not a code unit" % unit["id"])
    n = approval.load_unit_state(root, plan_id, unit["id"])["counters"]["attempts"]
    if not X._int(n):
        raise ReviewError("unit %s has no attempt" % unit["id"])
    j = J.valid_judgment(root, plan_id, unit, n)
    if not j or j.get("status") != "DONE" or not isinstance(j.get("candidate"), str):
        raise ReviewError("unit %s has no DONE judgment bound to the worktree HEAD; run `judge %s`" % (unit["id"], unit["id"]))
    adir = paths.attempt_dir(root, plan_id, unit["id"], n)
    return j, n, adir, W.load_workspace(adir)["worktree"]


def _cap(plan):
    return W.caps_of(plan)["review_rounds"]


def _latest_complete(adir):
    rounds = _rounds(adir)
    if not rounds or rounds[-1][2].get("state") != "complete":
        raise ReviewError("the latest review round has no verdict")
    R, rdir, rd = rounds[-1]
    rv = _obj(os.path.join(rdir, "review.json"))
    if not isinstance(rv, dict) or not isinstance(rv.get("findings"), list):
        raise ReviewError("round r%d has no usable review.json" % R)
    return R, rdir, rd, rv


# ---- input and validation ----------------------------------------------------

def validate_review(obj):
    """(clean review or None, errors). Types are checked, never coerced."""
    if not isinstance(obj, dict) or set(obj) != {"verdict", "findings"}:
        return None, ["review must be an object with exactly verdict and findings"]
    errs = []
    if obj["verdict"] not in ("PASS", "FAIL"):
        errs.append("verdict must be PASS or FAIL")
    fs = obj["findings"]
    if not isinstance(fs, list):
        return None, errs + ["findings must be a list"]
    seen = set()
    for i, f in enumerate(fs):
        tag = "findings[%d]" % i
        if not isinstance(f, dict) or set(f) != set(FINDING_KEYS):
            errs.append("%s must be an object with exactly %s" % (tag, ", ".join(FINDING_KEYS)))
            continue
        if not (isinstance(f["id"], str) and f["id"].strip()) or f["id"] in seen:
            errs.append("%s id must be a non-blank unique string" % tag)
        seen.add(f["id"] if isinstance(f["id"], str) else None)
        if f["severity"] not in ("blocking", "nonblocking"):
            errs.append("%s severity must be blocking or nonblocking" % tag)
        for k in ("claim", "file", "description", "reproduction"):
            if not isinstance(f[k], str):
                errs.append("%s %s must be a string" % (tag, k))
        if not (f["line"] is None or (type(f["line"]) is int and f["line"] >= 0)):
            errs.append("%s line must be a non-negative integer or null" % tag)
    if obj["verdict"] == "FAIL" and not fs and not errs:
        errs.append("a FAIL verdict needs at least one finding")
    return (None, errs) if errs else (obj, [])


def _diff_base(adir, j):
    """The integration base when U8 recorded one (integration.json: integration_base), else the unit's base."""
    rec = _obj(os.path.join(adir, "integration.json"))
    b = rec.get("integration_base") if rec else None
    return b if isinstance(b, str) and b else j["base"]


def build_input(unit, j, diff, earlier):
    lines = ["# Review input: %s" % unit["title"], "", "## Claims"] + ["- %s" % c for c in unit["claims"]]
    lines += ["- %s" % COUNTER_CLAIM, "", "## Test command", "`%s`" % unit["test_command"],
              "Exit code at the candidate: %s" % j.get("test_rc"), "", "## Earlier findings and dispositions"]
    lines += earlier or ["none"]
    lines += ["", "## Diff", "```diff", diff.rstrip("\n"), "```", ""]
    return "\n".join(lines)


def _earlier(rounds):
    out = []
    for R, rdir, rd in rounds:
        rv = _obj(os.path.join(rdir, "review.json")) if rd.get("state") == "complete" else None
        disp = (_obj(os.path.join(rdir, "dispositions.json")) or {}).get("dispositions", {})
        for f in (rv or {}).get("findings", []):
            d = disp.get(f["id"])
            out.append("- r%d finding %s" % (R, f["id"]))
            out += ["    %s: %s" % (k, f[k]) for k in FINDING_KEYS[1:]]
            out.append("    ruling: %s" % ("%s; note: %s; evidence: %s" % (d["ruling"], d.get("note", ""), ", ".join(d.get("evidence", [])))
                                           if d else "none"))
    return out


def author_sessions(root, plan_id, unit_id, n, family):
    """Session ids found in the stdout of the attempt's author and fixer invocations."""
    out = set()
    for i in X.iter_invocations(root, plan_id, unit_id):
        if i["attempt"] == n and i["kind"] in ("author", "fixer"):
            try:
                with open(os.path.join(i["path"], "stdout.log"), encoding="utf-8", errors="replace") as f:
                    sid = adapters.parse_result(family, 0, f.read(), "")["session_id"]
            except OSError:
                continue
            if sid:
                out.add(sid)
    return out


# ---- review ------------------------------------------------------------------

def _update_round(root, rdir, fn):
    with store.ProjectLock(root, "review"):
        path = os.path.join(rdir, "round.json")
        rd = _obj(path)
        fn(rd)
        store.write_json_atomic(path, rd)
        return rd


def _next_call_dir(rdir):
    nums = [int(m.group(1)) for m in (re.match(r"^c([0-9]+)$", x) for x in os.listdir(rdir)) if m]
    return max(nums or [0]) + 1


def _call(root, rdir, rd, wt, purpose, family, model, task, survey, timeout, sids_fn):
    """One reviewer call: intent recorded before it runs, result after. Returns the call record."""
    seq = _next_call_dir(rdir)
    cdir = os.path.join(rdir, "c%d" % seq)
    os.mkdir(cdir)
    call = {"seq": seq, "purpose": purpose, "family": family, "model": model, "dir": "c%d" % seq,
            "state": "started", "classification": None, "session_id": None, "reason": None}
    sroot = os.path.join(os.path.dirname(os.path.dirname(rdir)), "scratch")
    os.makedirs(sroot, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="review-", dir=sroot)
    try:
        rw, rs = os.path.realpath(wt), os.path.realpath(scratch)
        if os.path.commonpath([rw, rs]) == rw:
            raise ReviewError("scratch directory %s is inside the judged worktree" % scratch)
        try:
            inv = adapters.prepare(family, "reviewer", model, task, scratch, task_dir=cdir, survey=survey, schema=REVIEW_SCHEMA)
        except ValueError as e:
            call.update(state="failed", classification="CONFIG", reason="prepare failed: %s" % e)
            _update_round(root, rdir, lambda r: r["calls"].append(call))
            return call
        store.write_json_atomic(os.path.join(cdir, "argv.json"), {"argv": inv["argv"], "transport": inv["transport"],
                                                                 "schema_flag": inv["schema_flag"], "cwd": scratch})
        call["argv"] = inv["argv"]
        _update_round(root, rdir, lambda r: r["calls"].append(dict(call)))
        r = E.run_tool(inv["argv"], E.worker_env(), timeout, cwd=scratch, stdin=inv["stdin"])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    for name in ("stdout", "stderr"):
        with open(os.path.join(cdir, name + ".txt"), "w", encoding="utf-8") as f:
            f.write(r[name])
    res = adapters.parse_result(family, r["rc"], r["stdout"], r["stderr"])
    call.update(rc=r["rc"], timed_out=r["timed_out"], session_id=res["session_id"], classification=res["classification"],
                reason=res["reason"], schema_flag=inv["schema_flag"])
    review = None
    if res["ok"]:
        errs = []
        for cand in (res["structured"], adapters.extract_json(res["text"])):
            review, errs = validate_review(cand)
            if review:
                break
        if review is None:
            call.update(state="failed", classification="MALFORMED", reason="; ".join(errs[:5]))
        elif not res["session_id"]:
            review = None
            call.update(state="failed", classification="NO_SESSION", reason="the reviewer reported no session id")
        elif res["session_id"] in sids_fn():
            review = None
            call.update(state="failed", classification="SESSION_REUSE", reason="reviewer session id equals the author's")
        else:
            call.update(state="ok", classification="OK")
    else:
        call["state"] = "failed"
    store.write_json_atomic(os.path.join(cdir, "parsed.json"), {"call": call, "result": res, "review": review})

    def fin(rd2):
        rd2["calls"][-1] = call
    _update_round(root, rdir, fin)
    return dict(call, review=review)


@contextlib.contextmanager
def unit_review_lock(root, plan_id, unit):
    """Per-unit review lock for a whole review command, created atomically in the attempt directory."""
    n = approval.load_unit_state(root, plan_id, unit["id"])["counters"]["attempts"]
    if not X._int(n):
        raise ReviewError("unit %s has no attempt" % unit["id"])
    path = os.path.join(paths.attempt_dir(root, plan_id, unit["id"], n), "review.lock")
    holder = {"pid": os.getpid(), "host": socket.gethostname(), "time": store.utc_now()}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        cur = X._load(path)
        raise ReviewError("another review of %s is running or crashed (lock %s, holder %s); remove the file only "
                          "after confirming that holder is gone" % (unit["id"], path, cur if isinstance(cur, dict) else "unreadable"))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(holder, f)
        yield
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def run_review(root, plan_id, unit, retry=False, timeout=DEFAULT_TIMEOUT, max_chars=MAX_INPUT):
    with unit_review_lock(root, plan_id, unit):
        return _run_review(root, plan_id, unit, retry, timeout, max_chars)


def _run_review(root, plan_id, unit, retry, timeout, max_chars):
    with store.ProjectLock(root, "review"):
        plan = approval.effective_plan(root, plan_id)
        j, n, adir, wt = _current(root, plan_id, unit)
        cand = j["candidate"]
        cache = models.load_valid(root)
        if cache is None:
            raise ReviewError("models.json is missing or malformed; run `overseer.py models --host claude|google`")
        host = cache["host"]
        other = [f for f in models.FAMILIES if f != host][0]
        roles = models.roles_for(cache, other)
        if roles is None:
            raise ReviewError("the reviewer family %s is unavailable in models.json (%s)" % (other, cache["choices"][other].get("reason")))
        rounds = _rounds(adir)
        for R0, rdir0, rd0 in rounds:  # persist orphaned round directories as aborted records
            if not os.path.exists(os.path.join(rdir0, "round.json")):
                store.write_json_atomic(os.path.join(rdir0, "round.json"), rd0)
        last = rounds[-1] if rounds else None
        aborted = bool(last and last[2].get("state") == "aborted")
        reuse = None
        open_round = bool(last and last[2].get("state") in ("started", "failed") and last[2].get("candidate") == cand
                          and not last[2].get("retried"))
        if retry:
            if not open_round and not aborted:
                raise ReviewError("--retry needs a latest round for this candidate that failed or has an unknown outcome "
                                  "and has not been retried")
            reuse = last if open_round else None
        elif open_round:
            raise ReviewError("round r%d has no verdict; rerun with --retry (once), after which the round is consumed" % last[0])
        if reuse:
            R, rdir, rd = reuse
            with open(os.path.join(rdir, "input.md"), encoding="utf-8") as f:
                text = f.read()
        else:
            R = last[0] if (retry and aborted) else (last[0] if last else 0) + 1
            if R > _cap(plan):
                raise ReviewError("review rounds are capped at %d; the orchestrator rules alone with "
                                  "`dispositions %s --orchestrator-decision TEXT`" % (_cap(plan), unit["id"]))
            diff = W.git(wt, "diff", "--text", "--no-ext-diff", "--no-textconv", "--no-color", _diff_base(adir, j), cand)["stdout"]
            text = build_input(unit, j, diff, _earlier(rounds))
            if len(text) > max_chars:
                raise ReviewError("review input is %d characters, above the limit %d; it is not truncated. Split the unit "
                                  "(`plan split %s --into SPEC.json --reason TEXT`)" % (len(text), max_chars, unit["id"]))
            rdir = os.path.join(adir, "reviews", "r%d" % R)
            os.makedirs(rdir, exist_ok=True)
            with open(os.path.join(rdir, "input.md"), "w", encoding="utf-8") as f:
                f.write(text)
            rd = {"round": R, "candidate": cand, "base": j["base"], "host": host, "reviewer_family": other,
                  "model": roles["review"], "time": store.utc_now(), "state": "started", "retried": False,
                  "same_family_fallback": False, "calls": [], "verdict": None}
            store.write_json_atomic(os.path.join(rdir, "round.json"), rd)
            st = approval.load_unit_state(root, plan_id, unit["id"])
            st["counters"]["review_rounds"] = R
            approval.save_unit_state(root, plan_id, unit["id"], st)
            store.append_event(root, "review_round_started", plan_id=plan_id, unit_id=unit["id"], round=R, candidate=cand)
        if reuse:
            known = set(c.get("dir") for c in rd["calls"])
            for name in sorted(os.listdir(rdir), key=lambda x: (len(x), x)):
                if re.match(r"^c[0-9]+$", name) and name not in known:
                    rd["calls"].append({"seq": int(name[1:]), "purpose": "orphan", "dir": name, "state": "failed",
                                        "classification": "ORPHANED", "session_id": None, "family": other, "model": roles["review"],
                                        "reason": "call directory had no recorded call (interrupted before it was recorded)"})
            rd.update(retried=True, state="started")
            store.write_json_atomic(os.path.join(rdir, "round.json"), rd)
        sids_fn = lambda: author_sessions(root, plan_id, unit["id"], n, host)
        survey = W._survey(root)
    task = INSTRUCTIONS + text
    plans = [("retry" if reuse else "primary", other, roles["review"])]
    while plans:
        purpose, fam, model = plans.pop(0)
        c = _call(root, rdir, rd, wt, purpose, fam, model, task, survey, timeout, sids_fn)
        rd = _obj(os.path.join(rdir, "round.json"))
        if c["state"] == "ok":
            store.write_json_atomic(os.path.join(rdir, "review.json"), c["review"])
            rd = _update_round(root, rdir, lambda r: r.update(state="complete", verdict=c["review"]["verdict"]))
            break
        if c["classification"] in ("QUOTA", "AUTH") and fam != host and not rd["same_family_fallback"]:
            hroles = models.roles_for(cache, host)
            if hroles:
                why = "%s from %s %s" % (c["classification"], fam, model)
                rd = _update_round(root, rdir, lambda r: r.update(same_family_fallback=True, fallback_reason=why))
                plans.append(("fallback", host, hroles["review"]))
    else:
        rd = _update_round(root, rdir, lambda r: r.update(state="failed"))
    with store.ProjectLock(root, "review"):
        store.append_event(root, "review_round_finished", plan_id=plan_id, unit_id=unit["id"], round=R,
                           state=rd["state"], verdict=rd["verdict"], candidate=cand,
                           same_family_fallback=rd["same_family_fallback"])
    last_call = rd["calls"][-1]
    return {"round": R, "state": rd["state"], "verdict": rd["verdict"], "candidate": cand,
            "reviewer_family": last_call["family"], "model": last_call["model"],
            "session_id": last_call["session_id"], "same_family_fallback": rd["same_family_fallback"],
            "findings": len(_obj(os.path.join(rdir, "review.json"))["findings"]) if rd["state"] == "complete" else None,
            "reason": None if rd["state"] == "complete" else last_call.get("reason") or last_call.get("classification")}


# ---- dispositions and eligibility --------------------------------------------

def _evidence_ok(adir, ref):
    """An existing regular file under the attempt root; a reproduction record must parse with valid true."""
    try:
        full = paths.safe_relpath(adir, ref)
    except paths.UnsafePath:
        return False
    if not os.path.isfile(full):
        return False
    is_rep = re.match(r"^rep-[0-9]+\.json$", os.path.basename(full)) is not None
    rec = X._load(full) if full.endswith(".json") else None
    if is_rep or (isinstance(rec, dict) and rec.get("kind") == "reproduce"):
        return isinstance(rec, dict) and rec.get("valid") is True
    return True


def record_dispositions(root, plan_id, unit, data):
    """Validate and store rulings for every finding of the latest round, bound to the candidate head."""
    j, n, adir, wt = _current(root, plan_id, unit)
    R, rdir, rd, rv = _latest_complete(adir)
    if rd["candidate"] != j["candidate"]:
        raise ReviewError("round r%d reviewed %s but the candidate head is now %s; review the new candidate"
                          % (R, rd["candidate"][:12], j["candidate"][:12]))
    if not isinstance(data, dict):
        raise ReviewError("dispositions must be a JSON object keyed by finding id")
    sev = dict((f["id"], f["severity"]) for f in rv["findings"])
    errs = ["finding %s has no ruling" % i for i in sorted(sev) if i not in data]
    errs += ["unknown finding %r" % k for k in sorted(data) if k not in sev]
    clean = {}
    for fid, d in sorted(data.items()):
        if fid not in sev:
            continue
        if not (isinstance(d, dict) and set(d) <= {"ruling", "evidence", "note"} and d.get("ruling") in RULINGS):
            errs.append("%s: needs ruling (%s) and optional evidence list and note" % (fid, ", ".join(RULINGS)))
            continue
        ev, note = d.get("evidence", []), d.get("note", "")
        if not (isinstance(ev, list) and all(isinstance(e, str) for e in ev) and isinstance(note, str)):
            errs.append("%s: evidence must be a list of strings and note a string" % fid)
            continue
        missing = [e for e in ev if not _evidence_ok(adir, e)]
        if missing:
            errs.append("%s: evidence file not found under the attempt root: %s" % (fid, ", ".join(missing)))
        if sev[fid] == "blocking" and d["ruling"] in NEEDS_EVIDENCE and not ev:
            errs.append("%s: a blocking finding ruled %s needs at least one evidence reference" % (fid, d["ruling"]))
        clean[fid] = {"ruling": d["ruling"], "evidence": ev, "note": note}
    if errs:
        raise ReviewError("dispositions refused: " + "; ".join(errs))
    store.write_json_atomic(os.path.join(rdir, "dispositions.json"),
                            {"round": R, "candidate": j["candidate"], "time": store.utc_now(), "dispositions": clean})
    store.append_event(root, "dispositions_recorded", plan_id=plan_id, unit_id=unit["id"], round=R, candidate=j["candidate"])
    return review_state(root, plan_id, unit)


def record_decision(root, plan_id, unit, text):
    if not (isinstance(text, str) and text.strip()):
        raise ReviewError("--orchestrator-decision needs non-blank text")
    plan = approval.effective_plan(root, plan_id)
    j, n, adir, wt = _current(root, plan_id, unit)
    done = len(_rounds(adir))
    if done < _cap(plan):
        raise ReviewError("%d of %d review rounds used; an orchestrator decision is accepted only at the cap" % (done, _cap(plan)))
    try:  # binds the decision to the non-vacuity record that exists now (null when none does)
        with open(os.path.join(adir, "nonvacuous.json"), "rb") as f:
            nv_digest = hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        nv_digest = None
    store.write_json_atomic(os.path.join(adir, "reviews", "orchestrator_decision.json"),
                            {"orchestrator_decision": True, "text": text.strip(), "candidate": j["candidate"],
                             "rounds": done, "nonvacuous_sha256": nv_digest, "time": store.utc_now()})
    store.append_event(root, "orchestrator_decision", plan_id=plan_id, unit_id=unit["id"], candidate=j["candidate"])
    return review_state(root, plan_id, unit)


def equivalent_candidates(adir, cand):
    """The candidate plus every earlier candidate whose review was carried forward to it (carry.json)."""
    c = _obj(os.path.join(adir, "reviews", "carry.json"))
    if c and c.get("candidate") == cand and isinstance(c.get("candidates"), list):
        return set(c["candidates"]) | {cand}
    return {cand}


def carry_forward(root, plan_id, unit):
    """After an integration: carry the review (and non-vacuity) to the integrated candidate when the unit's own
    change is unchanged. Returns (carried, previous patch id, integrated patch id); nothing is written otherwise."""
    j, n, adir, wt = _current(root, plan_id, unit)
    integ = _obj(os.path.join(adir, "integration.json"))
    if not integ or integ.get("integrated_candidate") != j["candidate"]:
        raise ReviewError("the current candidate is not a recorded integration")
    prev = integ["previous_candidate"]
    p1 = patch_id(wt, integ["previous_base"], prev)
    p2 = patch_id(wt, integ["integration_base"], j["candidate"])
    if p1 is None or p1 != p2:
        return False, p1, p2
    chain = sorted(equivalent_candidates(adir, prev))
    store.write_json_atomic(os.path.join(adir, "reviews", "carry.json"), {
        "candidate": j["candidate"], "candidates": chain + [j["candidate"]], "time": store.utc_now(),
        "carried_from": {"candidate": prev, "patch_id": p1, "integrated_patch_id": p2}})
    store.append_event(root, "review_carried", plan_id=plan_id, unit_id=unit["id"], candidate=j["candidate"],
                       carried_from=prev, patch_id=p1)
    return True, p1, p2


def review_state(root, plan_id, unit):
    """{"state": ELIGIBLE|NOT_ELIGIBLE, "reasons": [...], "orchestrator_decision": bool}."""
    def no(*reasons):
        return {"state": "NOT_ELIGIBLE", "reasons": list(reasons), "orchestrator_decision": False}
    plan = approval.effective_plan(root, plan_id)
    try:
        j, n, adir, wt = _current(root, plan_id, unit)
    except ReviewError as e:
        return no(str(e))
    cand = j["candidate"]
    rounds = _rounds(adir)
    dec = _obj(os.path.join(adir, "reviews", "orchestrator_decision.json"))
    eq = equivalent_candidates(adir, cand)
    if dec and dec.get("candidate") in eq and len(rounds) >= _cap(plan):
        return {"state": "ELIGIBLE", "reasons": ["orchestrator decision recorded at the round cap"], "orchestrator_decision": True}
    if not rounds:
        return no("no review round")
    R, rdir, rd = rounds[-1]
    if rd.get("state") != "complete":
        return no("round r%d has no verdict (%s)" % (R, rd.get("state")))
    if rd.get("candidate") not in eq:
        return no("round r%d reviewed %s, not the current candidate" % (R, str(rd.get("candidate"))[:12]))
    sid = next((c.get("session_id") for c in rd["calls"] if c.get("state") == "ok"), None)
    if not sid or sid in author_sessions(root, plan_id, unit["id"], n, rd["host"]):
        return no("round r%d reviewer session id is missing or equals an author or fixer session id" % R)
    findings = _obj(os.path.join(rdir, "review.json"))["findings"]
    blocking = [f for f in findings if f["severity"] == "blocking"]
    if rd["verdict"] == "PASS" and not blocking:
        return {"state": "ELIGIBLE", "reasons": [], "orchestrator_decision": False}
    disp = _obj(os.path.join(rdir, "dispositions.json"))
    if not disp or disp.get("candidate") not in eq:
        return no("round r%d has findings without dispositions bound to the current candidate" % R)
    reasons = []
    for f in findings:
        d = disp["dispositions"].get(f["id"])
        if d is None:
            reasons.append("finding %s has no disposition" % f["id"])
        elif f["severity"] == "blocking":
            if d["ruling"] in ("inconclusive", "blocked"):
                reasons.append("blocking finding %s is %s" % (f["id"], d["ruling"]))
            elif d["ruling"] == "fixed":
                reasons.append("blocking finding %s is ruled fixed; the fix must be reviewed in a later round" % f["id"])
            elif not d["evidence"] or not all(_evidence_ok(adir, e) for e in d["evidence"]):
                reasons.append("blocking finding %s lacks existing evidence" % f["id"])
    return {"state": "NOT_ELIGIBLE" if reasons else "ELIGIBLE", "reasons": reasons, "orchestrator_decision": False}


# ---- disposable worktrees: reproduce and nonvacuous --------------------------

def _judged_state(wt):
    ref = W.git(wt, "symbolic-ref", "-q", "HEAD", ok=(0, 1))["stdout"].strip()
    h = hashlib.sha256()
    h.update(W.git(wt, "status", "--porcelain=v1", "-z", "--ignored", raw=True)["stdout"])
    h.update(b"\0diff\0")
    h.update(W.git(wt, "diff", "--no-ext-diff", "--binary", "HEAD", raw=True)["stdout"])
    return (W.rev(wt, "HEAD"), ref, W.rev(wt, ref) if ref else None, h.hexdigest())


@contextlib.contextmanager
def disposable(wt, commit):
    """A separate shared clone detached at `commit`, always removed; yields (directory, check).

    Nothing is shared with the judged repository except read-only objects, so a command cannot move its
    refs. `check()` returns a problem string when the judged HEAD or branch ref changed, else None.
    """
    parent = tempfile.mkdtemp(prefix="ovr-wt-")
    d = os.path.join(parent, "w")
    before = _judged_state(wt)
    try:
        W.git(parent, "clone", "--quiet", "--shared", "--no-checkout", wt, d)
        W.git(d, "checkout", "--quiet", "--detach", commit)
        yield d, lambda: None if _judged_state(wt) == before else "the judged worktree changed (HEAD, branch ref, status or tracked content)"
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def reproduce(root, plan_id, unit, finding, command, timeout=600.0):
    if not (isinstance(command, str) and command.strip()):
        raise ReviewError("--command must not be blank")
    j, n, adir, wt = _current(root, plan_id, unit)
    R, rdir, rd, rv = _latest_complete(adir)
    if finding not in [f["id"] for f in rv["findings"]]:
        raise ReviewError("finding %r is not in round r%d" % (finding, R))
    with disposable(wt, j["candidate"]) as (d, check):
        r = E.run_tool(["bash", "-c", command], E.worker_env(), timeout, cwd=d)
        problem = check()
    edir = os.path.join(adir, "evidence")
    os.makedirs(edir, exist_ok=True)
    k = 1 + len([x for x in os.listdir(edir) if re.match(r"^rep-[0-9]+\.json$", x)])
    rel = "evidence/rep-%d.json" % k
    store.write_json_atomic(os.path.join(adir, rel), {
        "kind": "reproduce", "finding": finding, "round": R, "command": command, "candidate": j["candidate"],
        "rc": r["rc"], "timed_out": r["timed_out"], "stdout": r["stdout"], "stderr": r["stderr"], "time": store.utc_now(),
        "valid": problem is None, "problem": problem})
    if problem:
        raise ReviewError("reproduction record %s is invalid: %s" % (rel, problem))
    store.append_event(root, "reproduced", plan_id=plan_id, unit_id=unit["id"], finding=finding, rc=r["rc"], evidence=rel)
    return {"evidence": rel, "rc": r["rc"], "timed_out": r["timed_out"]}


def is_test_file(path, globs):
    for g in globs:
        if (paths.glob_match(g, path) if "/" in g else paths.glob_match(g, path.split("/")[-1])):
            return True
    return False


def nonvacuous(root, plan_id, unit, timeout=J.TEST_TIMEOUT):
    """PASS when the tests fail once every non-test change is reverted; FAIL when they still pass."""
    plan = approval.effective_plan(root, plan_id)
    j, n, adir, wt = _current(root, plan_id, unit)
    cand, base = j["candidate"], _diff_base(adir, j)
    globs = plan.get("test_globs") or list(DEFAULT_TEST_GLOBS)
    toks = W.zsplit(W.git(wt, "diff", "--name-status", "-z", "--no-renames", base, cand, raw=True)["stdout"])
    changed = [(toks[i], toks[i + 1]) for i in range(0, len(toks) - 1, 2)]
    tests = [p for _, p in changed if is_test_file(p, globs)]
    others = [(s, p) for s, p in changed if not is_test_file(p, globs)]
    rec = {"candidate": cand, "base": base, "test_files": tests, "restored": [p for _, p in others], "test_rc": None,
           "time": store.utc_now()}
    if not tests or not others:
        rec.update(status="NOT_APPLICABLE", reason="no test file changed" if not tests else "no non-test file changed")
    else:
        def run(d):
            return E.run_tool(["bash", "-c", unit["test_command"]], E.worker_env(), timeout, cwd=d)
        with disposable(wt, cand) as (d, check):  # the baseline gets its own clone: its artifacts must not reach the revert run
            r0 = run(d)
            problem = check()
        if problem:
            raise ReviewError("%s; no non-vacuity record written" % problem)
        if r0["timed_out"] or r0["rc"] != 0:
            raise ReviewError("baseline does not pass at the candidate (%s); no non-vacuity record written"
                              % ("timeout" if r0["timed_out"] else "rc %s" % r0["rc"]))
        with disposable(wt, cand) as (d, check):
            todo = []
            for s_, p in others:
                if s_ == "A":  # added since base: drop it from the index and the tree together
                    W.git(d, "--literal-pathspecs", "rm", "-q", "-f", "-r", "--", p)
                else:
                    todo.append(p)
            for i in range(0, len(todo), 100):
                W.git(d, "--literal-pathspecs", "checkout", base, "--", *todo[i:i + 100])
            r = run(d)
            problem = check()
        if problem:
            raise ReviewError("%s; no non-vacuity record written" % problem)
        if r["timed_out"]:
            raise ReviewError("test command timed out after the revert; no non-vacuity record written")
        rec.update(status="PASS" if r["rc"] != 0 else "FAIL", test_rc=r["rc"], stdout_tail=r["stdout"][-2000:],
                   stderr_tail=r["stderr"][-2000:])
    store.write_json_atomic(os.path.join(adir, "nonvacuous.json"), rec)
    store.append_event(root, "nonvacuous", plan_id=plan_id, unit_id=unit["id"], status=rec["status"], candidate=cand)
    return rec


def patch_id(repo, a, b):
    """`git patch-id --stable` of the diff a..b (None for an empty diff). No shell pipe."""
    diff = W.git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color", a, b, raw=True)["stdout"]
    out = W.git(repo, "patch-id", "--stable", stdin=diff)["stdout"].split()
    return out[0] if out else None


# ---- commands ----------------------------------------------------------------

def _unit(root, args):
    pid = args.plan or approval.find_plan_for_unit(root, args.unit)
    approval.require_approval(root, pid)
    return pid, X.refuse_pr_unit(X._unit_of(approval.effective_plan(root, pid), args.unit), "has no review, fix or non-vacuity check")


def _show(d):
    print(json.dumps(d, indent=2, sort_keys=True))


def _plan_arg(p):
    p.add_argument("unit", metavar="UNIT")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")


def configure_review(p):
    _plan_arg(p)
    p.add_argument("--retry", action="store_true", help="retry a round whose reviewer call failed (once)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="S")
    p.add_argument("--max-input-chars", type=int, default=MAX_INPUT, metavar="N")


def run_review_cmd(args):
    root = paths.find_root(args.root)
    if args.timeout <= 0 or args.max_input_chars <= 0:
        raise ReviewError("--timeout and --max-input-chars must be positive")
    pid, unit = _unit(root, args)
    res = run_review(root, pid, unit, args.retry, args.timeout, args.max_input_chars)
    _show(res)
    return 0 if res["state"] == "complete" else 1


def configure_reproduce(p):
    _plan_arg(p)
    p.add_argument("--finding", required=True, metavar="ID")
    p.add_argument("--command", required=True, dest="repro_command", metavar="CMD",
                   help="runs with cwd set to a disposable checkout of the candidate; use relative paths, not your shell PWD")
    p.add_argument("--timeout", type=float, default=600.0, metavar="S")


def run_reproduce(args):
    root = paths.find_root(args.root)
    with store.ProjectLock(root, "reproduce"):
        pid, unit = _unit(root, args)
        _show(reproduce(root, pid, unit, args.finding, args.repro_command, args.timeout))
    return 0


def configure_dispositions(p):
    _plan_arg(p)
    p.add_argument("file", nargs="?", default=None, metavar="FILE")
    p.add_argument("--orchestrator-decision", default=None, metavar="TEXT")
    p.add_argument("--state", action="store_true", help="print review eligibility")


def run_dispositions(args):
    root = paths.find_root(args.root)
    if sum(1 for x in (args.file, args.orchestrator_decision, args.state or None) if x is not None) != 1:
        raise ReviewError("give exactly one of FILE, --orchestrator-decision TEXT, --state")
    with store.ProjectLock(root, "dispositions"):
        pid, unit = _unit(root, args)
        if args.file is not None:
            try:
                data = store.read_json(args.file)
            except ValueError as e:
                raise ReviewError("%s is not valid JSON: %s" % (args.file, e))
            res = record_dispositions(root, pid, unit, data)
        elif args.orchestrator_decision is not None:
            res = record_decision(root, pid, unit, args.orchestrator_decision)
        else:
            res = review_state(root, pid, unit)
    _show(res)
    return 0 if res["state"] == "ELIGIBLE" else 1


def configure_fix(p):
    _plan_arg(p)
    p.add_argument("--prompt-file", required=True, metavar="F")


def run_fix(args):
    root = paths.find_root(args.root)
    try:
        with open(args.prompt_file, "rb") as f:
            raw = f.read(W.MAX_FIX_PROMPT + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ReviewError("cannot read --prompt-file: %s" % e)
    if len(raw) > W.MAX_FIX_PROMPT or not text.strip():
        raise ReviewError("--prompt-file must be non-blank and at most %d bytes" % W.MAX_FIX_PROMPT)
    with store.ProjectLock(root, "fix"):
        pid, unit = _unit(root, args)
        if unit["kind"] != "code":
            raise ReviewError("unit %s is not a code unit" % unit["id"])
        rec = W.dispatch_fixer(root, pid, unit, text)
    print(json.dumps({"plan": pid, "unit": unit["id"], "attempt": rec["attempt"], "invocation": rec["seq"]}, sort_keys=True))
    return 0


def configure_nonvacuous(p):
    p.description = ("Run the tests with every non-test change reverted. Limit: a repository with an initialized, "
                     "unchanged submodule makes the clone baseline fail, so it cannot be evaluated; the orchestrator "
                     "records a decision at the round cap.")
    _plan_arg(p)
    p.add_argument("--timeout", type=float, default=J.TEST_TIMEOUT, metavar="S")


def run_nonvacuous(args):
    root = paths.find_root(args.root)
    with store.ProjectLock(root, "nonvacuous"):
        pid, unit = _unit(root, args)
        rec = nonvacuous(root, pid, unit, args.timeout)
    _show(rec)
    return 0
