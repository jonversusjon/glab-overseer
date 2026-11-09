"""GitHub transport: issues, native blocked-by links, pull requests, close and read-back.

Every remote write records an intent event first and is reconciled by a read-back before
any retry. A timeout or connection error is AMBIGUOUS (the write may have happened); it is
never counted as a definite failure and never repeated blindly. Issues are matched by the
hidden marker in their body over one full listing (no search API, which lags).

State: `issues.json` of the plan holds `filed` {unit: number}, `filed_at` {unit: read time}
and `links` {"<unit><-<need>": {...}} next to U1's `drafts`; `units/<unit>/pr.json` holds the
pull request record. Writers expect the caller to hold `store.ProjectLock`.

Nothing here merges (`controller`). Worker-side modules must not import this module.
"""
import contextlib
import json
import os
import re
import secrets
import sys
import tempfile
import time

from . import approval, env as E, execution as X, judge as J, paths, plan as P, store, workspace as W

POLL_WINDOW = 120.0  # after an ambiguous write, poll this long for its marker before writing again
POLL_INTERVAL = 10.0
READ_TRIES = 3
RETRY_DELAY = 2.0
FETCH_TIMEOUT = 300
PUSH_TIMEOUT = 300
TRANSIENT_RE = re.compile(
    r"i/o timeout|timed? ?out|tls handshake|error connecting|dial tcp|connection (reset|refused|closed)"
    r"|temporary failure|no such host|unexpected eof|network is unreachable|HTTP 5\d\d|context deadline", re.I)
ISSUE_URL_RE = re.compile(r"https?://[^\s]+/issues/(\d+)\s*$")
PR_URL_RE = re.compile(r"https?://[^\s]+/pull/(\d+)\s*$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GithubError(Exception):
    def __init__(self, message, http404=False):
        super().__init__(message)
        self.http404 = http404


class Ambiguous(GithubError):
    """A remote write may or may not have happened; rerun after the read-back shows it."""


# ---- gh ----------------------------------------------------------------------

def gh(args, timeout=60, cwd=None):
    """Run `gh` with the supervisor environment; returns dict(rc, stdout, stderr, timed_out)."""
    env = E.supervisor_env()
    env.update({"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1", "GH_PAGER": "cat",
                "LC_ALL": "C", "LANG": "C"})
    return E.run_tool(["gh"] + list(args), env, timeout, cwd=cwd)


def transient(r):
    """True for a timeout or a connection-level failure: the outcome of a write is then unknown."""
    return bool(r["timed_out"] or (r["rc"] not in (0, 127) and TRANSIENT_RE.search(r["stderr"] or "")))


DEFINITE_RE = re.compile(r"Validation Failed|HTTP (?!408\b|429\b)4\d\d|Resource not accessible|Not Found|Bad credentials"
                         r"|could not resolve to a|not permitted", re.I)


def definite_rejection(r):
    """A write is known not to have happened only when it never ran or stderr shows a definite rejection."""
    return r["rc"] == 127 or bool(not r["timed_out"] and r["rc"] is not None and DEFINITE_RE.search(r["stderr"] or ""))


def _fail(what, r):
    return GithubError("gh %s failed (rc %s): %s" % (what, r["rc"], ((r["stderr"] or "").strip()[:300] or "no output")),
                       http404="HTTP 404" in (r["stderr"] or ""))


def _read(argv, what, want=None):
    """A read: retried on transient failure; the parsed JSON must be of type `want`."""
    for attempt in range(READ_TRIES):
        r = gh(argv)
        if r["rc"] == 0:
            try:
                data = json.loads(r["stdout"])
            except ValueError:
                raise GithubError("gh %s returned output that is not JSON" % what)
            if want is not None and not isinstance(data, want):
                raise GithubError("gh %s returned %s, expected %s" % (what, type(data).__name__, want.__name__))
            return data
        if not transient(r) or attempt == READ_TRIES - 1:
            raise _fail(what, r)
        time.sleep(RETRY_DELAY)


def _read_text(argv, what):
    """stdout of a read, retried on transient failure."""
    for attempt in range(READ_TRIES):
        r = gh(argv)
        if r["rc"] == 0:
            return r["stdout"]
        if not transient(r) or attempt == READ_TRIES - 1:
            raise _fail(what, r)
        time.sleep(RETRY_DELAY)


def _write(root, op, argv, **fields):
    """Record the intent, then run the write. Returns (result, ambiguous); a definite failure raises."""
    store.append_event(root, "github_intent", op=op, **fields)
    r = gh(argv)
    if r["rc"] == 0:
        return r, False
    if not definite_rejection(r):  # inverted like sbatch: any other non-zero exit may have been applied
        store.append_event(root, "github_ambiguous", op=op, error=(r["stderr"] or "").strip()[:200], **fields)
        return r, True
    raise _fail(op, r)


@contextlib.contextmanager
def _bodyfile(root, text):
    d = os.path.join(paths.ensure_state_dir(root), "tmp")
    os.makedirs(d, exist_ok=True)
    fd, p = tempfile.mkstemp(dir=d, prefix="body-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        yield p
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass


def _is_int(v):
    return type(v) is int and v > 0


# ---- issues ------------------------------------------------------------------

def list_issues(repo):
    """Every issue (any state, no cap) through the paginated REST API; pull requests are skipped."""
    text = _read_text(["api", "--paginate", "repos/%s/issues?state=all&per_page=100" % repo], "issue list")
    dec, pos, out = json.JSONDecoder(), 0, []
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        try:
            page, pos = dec.raw_decode(text, pos)
        except ValueError:
            raise GithubError("issue list returned output that is not JSON pages")
        if not isinstance(page, list):
            raise GithubError("issue list page is %s, expected a list" % type(page).__name__)
        for i in page:
            if not (isinstance(i, dict) and _is_int(i.get("number")) and isinstance(i.get("title"), str)
                    and (i.get("body") is None or isinstance(i["body"], str)) and i.get("state") in ("open", "closed")):
                raise GithubError("issue list returned a malformed entry: %.100r" % (i,))
            if "pull_request" not in i:
                out.append({"number": i["number"], "title": i["title"], "body": i["body"] or "",
                            "state": i["state"].upper()})
    return out


def has_marker(body, marker):
    return any(line.strip() == marker for line in body.splitlines())


def find_by_marker(issues, marker, title=None):
    return [i for i in issues if has_marker(i["body"], marker) and (title is None or i["title"] == title)]


def project_id(root):
    """Random 12-hex id of this project root, created once (atomically) and never regenerated."""
    path = os.path.join(paths.ensure_state_dir(root), "project.json")
    for _ in range(2):
        rec = store.read_json(path, None)
        if rec is not None:
            if not (isinstance(rec, dict) and isinstance(rec.get("project_id"), str)
                    and re.fullmatch(r"[0-9a-f]{12}", rec["project_id"])):
                raise GithubError("%s is malformed; refusing to regenerate the project id" % path)
            return rec["project_id"]
        pid = secrets.token_hex(6)
        try:
            store.publish_record(path, {"token": pid, "project_id": pid})
        except FileExistsError:
            pass
    raise GithubError("could not read or create %s" % path)


def marker_for(root, plan_id, unit_id):
    return "<!-- overseer project=%s plan=%s unit=%s -->" % (project_id(root), plan_id, unit_id)


def view_issue(repo, number, fields="number,title,body,state"):
    d = _read(["issue", "view", str(number), "-R", repo, "--json", fields], "issue view", dict)
    if d.get("number") != number:
        raise GithubError("issue view returned number %r, expected %s" % (d.get("number"), number))
    return d


def _issues_path(root, plan_id):
    return os.path.join(paths.plan_dir(root, plan_id), "issues.json")


def _drafts(root, plan_id, rec):
    drafts = dict(rec.get("drafts") or {})
    for r in approval.load_revisions(root, plan_id):
        drafts.update(r.get("drafts") or {})
    return drafts


def _record_filed(root, plan_id, rec, uid, number, read_at):
    rec.setdefault("filed", {})[uid] = number
    rec.setdefault("filed_at", {})[uid] = read_at
    store.write_json_atomic(_issues_path(root, plan_id), rec)


def _pending_path(root):
    return os.path.join(paths.state_dir(root), "github_pending.json")


def _pend(root, marker, op):
    """Keep a write's intent as ambiguous until its read-back confirms it (survives a crash or timeout)."""
    paths.ensure_state_dir(root)
    d = store.read_json(_pending_path(root), {})
    d.setdefault(marker, {"op": op, "time": time.time()})
    store.write_json_atomic(_pending_path(root), d)


def _unpend(root, marker):
    d = store.read_json(_pending_path(root), {})
    if d.pop(marker, None) is not None:
        store.write_json_atomic(_pending_path(root), d)


def _settle(root, marker, find):
    """Matches of `marker`; when an earlier write is still ambiguous, poll until the window since it closes."""
    rec = store.read_json(_pending_path(root), {}).get(marker)
    deadline = rec["time"] + POLL_WINDOW if isinstance(rec, dict) and isinstance(rec.get("time"), (int, float)) else 0
    while True:
        found = find()
        if found or time.time() >= deadline:
            return found
        time.sleep(max(0.0, min(POLL_INTERVAL, deadline - time.time())))


def _create_issue(root, repo, uid, draft, marker):
    _pend(root, marker, "issue_create")
    try:
        with _bodyfile(root, draft["body"]) as bf:
            r, amb = _write(root, "issue_create", ["issue", "create", "-R", repo, "--title", draft["title"],
                                                   "--body-file", bf], unit=uid, marker=marker)
    except GithubError:
        _unpend(root, marker)  # a definite failure: nothing was created
        raise
    m = ISSUE_URL_RE.search(r["stdout"]) if not amb else None
    if m:
        return int(m.group(1))
    found = find_by_marker(list_issues(repo), marker, draft["title"])  # reconcile by read-back, never by repeating the create
    if len(found) == 1:
        return found[0]["number"]
    if found:
        raise GithubError("marker %s matches %d issues after an uncertain create" % (marker, len(found)))
    raise Ambiguous("creating the issue for %s has an unknown outcome and the listing does not show it; "
                    "rerun 'issues file' to reconcile" % uid)


def file_issues(root, plan_id, dry_run=False, out=print):
    """File every unit's draft once. Returns {unit: number} (units still unfiled are absent on dry run)."""
    approval.require_approval(root, plan_id)
    plan = approval.effective_plan(root, plan_id)
    repo, units = plan["repo"], plan["units"]
    rec = store.read_json(_issues_path(root, plan_id))
    drafts = _drafts(root, plan_id, rec)
    listed = list_issues(repo)
    markers = {}
    read_at = store.utc_now()
    plan_matches = {}
    for u in units:
        d = drafts.get(u["id"])
        old = P.issue_marker(plan_id, u["id"])
        if not (isinstance(d, dict) and isinstance(d.get("title"), str) and isinstance(d.get("body"), str)
                and has_marker(d["body"], old)):
            raise GithubError("unit %s has no usable issue draft with its marker" % u["id"])
        marker = markers[u["id"]] = marker_for(root, plan_id, u["id"])
        drafts[u["id"]] = {"title": d["title"], "body": "\n".join(marker if ln.strip() == old else ln
                                                                    for ln in d["body"].split("\n"))}
        found = find_by_marker(listed, marker, d["title"])
        if len(found) > 1:
            raise GithubError("ambiguous: issues %s all carry the marker of unit %s"
                              % (", ".join("#%d" % i["number"] for i in found), u["id"]))
        recorded = (rec.get("filed") or {}).get(u["id"])
        if recorded is not None and (len(found) != 1 or found[0]["number"] != recorded):
            raise GithubError("issues.json records #%s for unit %s but the repository listing disagrees"
                              % (recorded, u["id"]))
        if not found and not dry_run and recorded is None:
            found = _settle(root, marker, lambda: find_by_marker(list_issues(repo), marker, d["title"]))
            if len(found) > 1:
                raise GithubError("ambiguous: issues %s all carry the marker of unit %s"
                                  % (", ".join("#%d" % i["number"] for i in found), u["id"]))
        plan_matches[u["id"]] = found
    numbers = {}
    for u in units:
        uid, found = u["id"], plan_matches[u["id"]]
        if found:
            numbers[uid] = found[0]["number"]
            out("%s: #%d (found by marker)" % (uid, numbers[uid]))
            if not dry_run:
                _record_filed(root, plan_id, rec, uid, numbers[uid], read_at)
                _unpend(root, markers[uid])
            continue
        if dry_run:
            out("%s: would create %r" % (uid, drafts[uid]["title"]))
            continue
        marker = markers[uid]
        n = _create_issue(root, repo, uid, drafts[uid], marker)
        dups = find_by_marker(list_issues(repo), marker, drafts[uid]["title"])
        if len(dups) > 1:  # report only; nothing is closed or edited
            raise GithubError("DUPLICATE ISSUES for unit %s: %s all carry %s; the owner must close the extras"
                              % (uid, ", ".join("#%d" % i["number"] for i in dups), marker))
        back = view_issue(repo, n)
        if back["title"] != drafts[uid]["title"] or not has_marker(back["body"], marker):
            raise GithubError("read-back of #%d does not match the draft of unit %s" % (n, uid))
        numbers[uid] = n
        _record_filed(root, plan_id, rec, uid, n, store.utc_now())
        _unpend(root, marker)
        out("%s: #%d (created)" % (uid, n))
    if not dry_run:
        link_dependencies(root, plan_id, repo, units, rec, out)
    return numbers


# ---- dependencies ------------------------------------------------------------

def _blocked_by(repo, number):
    path = "repos/%s/issues/%d/dependencies/blocked_by?per_page=100" % (repo, number)
    items = _read(["api", path], "blocked_by read", list)
    if not all(isinstance(i, dict) and _is_int(i.get("id")) for i in items):
        raise GithubError("blocked_by read returned a malformed entry")
    return set(i["id"] for i in items)  # global ids: a same-numbered issue of another repository does not count


def _comments(repo, number):
    cs = view_issue(repo, number, "number,comments").get("comments")
    if not (isinstance(cs, list) and all(isinstance(c, dict) and isinstance(c.get("body"), str) for c in cs)):
        raise GithubError("issue view returned malformed comments")
    return cs


def ensure_comment(root, repo, number, text, marker):
    """Post `text` with a hidden marker unless a comment with it exists; verified by read-back."""
    body = "%s\n\n%s\n" % (text, marker)
    def marked():
        return [c for c in _comments(repo, number) if has_marker(c["body"], marker)]
    if marked() or _settle(root, marker, marked):
        _unpend(root, marker)
        return False
    _pend(root, marker, "comment")
    try:
        with _bodyfile(root, body) as bf:
            _write(root, "comment", ["issue", "comment", str(number), "-R", repo, "--body-file", bf],
                   issue=number, marker=marker)
    except GithubError:
        _unpend(root, marker)
        raise
    seen = marked()
    if not seen:
        raise Ambiguous("comment on #%d is not visible after writing; rerun to reconcile" % number)
    if len(seen) > 1:
        raise GithubError("DUPLICATE COMMENTS on #%d: %d carry %s; the owner must delete the extras"
                          % (number, len(seen), marker))
    _unpend(root, marker)
    return True


def _link(root, repo, blocked, blocker):
    """Native blocked-by link, else (API missing, HTTP 404) a marked comment. Returns the mode."""
    try:
        bid = _read(["api", "repos/%s/issues/%d" % (repo, blocker)], "issue read", dict).get("id")
        if not _is_int(bid):
            raise GithubError("issue #%d has no numeric id" % blocker)
        if bid in _blocked_by(repo, blocked):
            return "native"
        _write(root, "blocked_by", ["api", "-X", "POST", "repos/%s/issues/%d/dependencies/blocked_by" % (repo, blocked),
                                    "-F", "issue_id=%d" % bid], blocked=blocked, blocker=blocker)
        if bid not in _blocked_by(repo, blocked):
            raise Ambiguous("link #%d blocked by #%d is not visible after writing; rerun to reconcile"
                            % (blocked, blocker))
        return "native"
    except GithubError as e:
        if not e.http404 or isinstance(e, Ambiguous):
            raise
    ensure_comment(root, repo, blocked, "Blocked by #%d" % blocker, "<!-- overseer blocked-by=%d -->" % blocker)
    return "comment"


def link_dependencies(root, plan_id, repo, units, rec, out=print):
    filed = rec.get("filed") or {}
    for u in units:
        for need in u["needs"]:
            key = "%s<-%s" % (u["id"], need)
            if key in (rec.get("links") or {}):
                continue
            if u["id"] not in filed or need not in filed:
                raise GithubError("cannot link %s: an issue is not filed" % key)
            mode = _link(root, repo, filed[u["id"]], filed[need])
            rec.setdefault("links", {})[key] = {"mode": mode, "blocked": filed[u["id"]], "blocker": filed[need],
                                                "read_at": store.utc_now()}
            store.write_json_atomic(_issues_path(root, plan_id), rec)
            out("%s: blocked by %s (%s)" % (u["id"], need, mode))


# ---- close -------------------------------------------------------------------

def close_issue(number, comment, repo, root):
    """Post the comment (once), close the issue and read back CLOSED."""
    if not _is_int(number) or not (isinstance(comment, str) and comment.strip()):
        raise GithubError("close_issue needs an issue number and a non-blank comment")
    ensure_comment(root, repo, number, comment.strip(), "<!-- overseer close=%d -->" % number)
    if view_issue(repo, number)["state"] != "CLOSED":
        _write(root, "issue_close", ["issue", "close", str(number), "-R", repo], issue=number)
        state = view_issue(repo, number)["state"]
        if state != "CLOSED":
            raise Ambiguous("issue #%d reads %s after closing; rerun to reconcile" % (number, state))
    return "CLOSED"


def set_tracker(root, plan_id, unit_id, **fields):
    """Replace the unit's tracker facet (visible in `status`); the caller holds the project lock."""
    st = approval.load_unit_state(root, plan_id, unit_id)
    st["tracker"] = dict(fields)
    approval.save_unit_state(root, plan_id, unit_id, st)


def default_branch(repo):
    d = _read(["repo", "view", repo, "--json", "defaultBranchRef"], "repo view", dict)  # repo view takes no -R
    ref = d.get("defaultBranchRef")
    if not (isinstance(ref, dict) and isinstance(ref.get("name"), str) and ref["name"]):
        raise GithubError("repo view returned no default branch")
    return ref["name"]


def _judgment(root, plan_id, unit):
    n = approval.load_unit_state(root, plan_id, unit["id"])["counters"]["attempts"]
    return J.valid_judgment(root, plan_id, unit, n) if X._int(n) else None


def close_unit_issue(root, plan_id, unit, comment):
    """Close a job unit's issue after a DONE (or orchestrator-resolved done) judgment, or a pr unit's issue
    once its pull request is open (closure `closed`)."""
    if unit["kind"] == "pr":
        closure = approval.load_unit_state(root, plan_id, unit["id"])["closure"]
        if closure.get("state") != "closed":
            raise GithubError("unit %s has no opened pull request (closure %s); run `pr %s` first"
                              % (unit["id"], closure.get("state"), unit["id"]))
    elif unit["kind"] != "job":
        raise GithubError("only job and pr units are closed here; code units close through their pull request")
    else:
        j = _judgment(root, plan_id, unit)
        if not j or j["status"] != "DONE":
            raise GithubError("unit %s has no valid DONE judgment" % unit["id"])
    number = (store.read_json(_issues_path(root, plan_id)).get("filed") or {}).get(unit["id"])
    if not _is_int(number):
        raise GithubError("unit %s has no filed issue" % unit["id"])
    return number, close_issue(number, comment, approval.effective_plan(root, plan_id)["repo"], root)


# ---- pull requests -----------------------------------------------------------

def readback_pr(number, repo):
    """State, head oid, merge commit oid, base ref and closing issue numbers of a pull request."""
    d = _read(["pr", "view", str(number), "-R", repo, "--json",
               "number,state,headRefOid,mergeCommit,baseRefName,closingIssuesReferences,title,body"], "pr view", dict)
    mc, cr = d.get("mergeCommit"), d.get("closingIssuesReferences")
    if not (d.get("number") == number and d.get("state") in ("OPEN", "CLOSED", "MERGED")
            and isinstance(d.get("headRefOid"), str) and isinstance(d.get("baseRefName"), str)
            and (mc is None or (isinstance(mc, dict) and isinstance(mc.get("oid"), str)))
            and isinstance(cr, list) and all(isinstance(c, dict) and _is_int(c.get("number")) for c in cr)
            and isinstance(d.get("body"), str) and isinstance(d.get("title"), str)):
        raise GithubError("pr view returned a malformed record for #%s" % number)
    return {"number": number, "state": d["state"], "head_oid": d["headRefOid"],
            "merge_oid": mc["oid"] if mc else None, "base_ref": d["baseRefName"],
            "closing_issues": sorted(c["number"] for c in cr), "title": d["title"], "body": d["body"]}


def _prs_for_branch(repo, branch, base=None):
    """Same-repository pull requests whose head is `branch` (forks are ignored); OPEN ones against
    `base` only when `base` is given."""
    items = _read(["pr", "list", "-R", repo, "--head", branch, "--state", "all", "--limit", "100", "--json",
                   "number,state,headRefName,baseRefName,body,isCrossRepository,headRepositoryOwner"], "pr list", list)
    owner = repo.split("/")[0].lower()
    out = []
    for i in items:
        if not (isinstance(i, dict) and i.get("headRefName") == branch and _is_int(i.get("number"))):
            continue
        ho = i.get("headRepositoryOwner")
        if i.get("isCrossRepository") is not False or not isinstance(ho, dict) \
                or not isinstance(ho.get("login"), str) or ho["login"].lower() != owner:
            continue
        if base is None or (i.get("state") == "OPEN" and i.get("baseRefName") == base):
            out.append(i)
    return out


def push_argv(branch, sha):
    """The only push this program runs: the judged commit to its branch, never a force form."""
    W.check_branch_name(branch)
    if not SHA_RE.match(sha):
        raise GithubError("push needs a full commit sha, got %r" % (sha,))
    return ["push", "origin", "%s:refs/heads/%s" % (sha, branch)]


def _remote_head(wt, branch):
    out = W.git(wt, "ls-remote", "--heads", "origin", "refs/heads/" + branch, timeout=FETCH_TIMEOUT)["stdout"]
    shas = [ln.split("\t")[0] for ln in out.splitlines() if ln.split("\t")[-1:] == ["refs/heads/" + branch]]
    if len(shas) > 1 or any(not SHA_RE.match(s) for s in shas):
        raise GithubError("ls-remote returned an unexpected answer for %s" % branch)
    return shas[0] if shas else None


def push_branch(root, wt, branch, candidate):
    """Push `branch`; refuse when the remote branch exists and is not an ancestor of `candidate`."""
    remote = _remote_head(wt, branch)
    if remote == candidate:
        return "up-to-date"
    if remote is not None:
        W.git(wt, "fetch", "origin", "refs/heads/" + branch, timeout=FETCH_TIMEOUT)
        if W.git(wt, "cat-file", "-e", remote + "^{commit}", ok=(0, 1, 128))["rc"] != 0 \
                or W.git(wt, "merge-base", "--is-ancestor", remote, candidate, ok=(0, 1))["rc"] != 0:
            raise GithubError("remote %s at %s is not an ancestor of the candidate %s; refusing to push"
                              % (branch, remote[:12], candidate[:12]))
    store.append_event(root, "github_intent", op="push", branch=branch, candidate=candidate, remote=remote)
    try:
        W.git(wt, *push_argv(branch, candidate), timeout=PUSH_TIMEOUT)
    except W.GitError as e:
        if _remote_head(wt, branch) != candidate:
            raise GithubError("push of %s failed or has an unknown outcome: %s" % (branch, e))
    if _remote_head(wt, branch) != candidate:
        raise GithubError("remote %s does not read back at the candidate %s" % (branch, candidate[:12]))
    return "pushed"


def pr_body(unit, issue, j, marker):
    lines = [marker, ""]
    if issue is not None:
        lines += ["Closes #%d" % issue, ""]
    lines += ["## Claims"] + ["- %s" % c for c in unit.get("claims", [])] + ["",
              "## Judgment", "- base: %s" % j.get("base"), "- candidate: %s" % j.get("candidate"),
              "- test rc: %s" % j.get("test_rc"), ""]
    return "\n".join(lines)


def open_pr(root, plan_id, unit):
    """Push the judged candidate and create (or re-body) its pull request; returns the record."""
    if unit["kind"] != "code":
        raise GithubError("unit %s is not a code unit; it has no branch to open a pull request from" % unit["id"])
    j = _judgment(root, plan_id, unit)
    if not j or j["status"] != "DONE":
        raise GithubError("unit %s has no valid DONE judgment (judge it again if the head moved)" % unit["id"])
    plan = approval.effective_plan(root, plan_id)
    repo, target = plan["repo"], W.check_branch_name(plan["target_branch"])
    n = approval.load_unit_state(root, plan_id, unit["id"])["counters"]["attempts"]
    ws = W.load_workspace(paths.attempt_dir(root, plan_id, unit["id"], n))
    branch, wt, cand = ws["branch"], ws["worktree"], j["candidate"]
    issue = (store.read_json(_issues_path(root, plan_id)).get("filed") or {}).get(unit["id"])
    marker = marker_for(root, plan_id, unit["id"])
    body = pr_body(unit, issue, j, marker)
    # inspected before any write (push, edit or create); only same-repository OPEN pull requests count
    opens = [e for e in _prs_for_branch(repo, branch) if e.get("state") == "OPEN"]
    for e in opens:
        if e.get("baseRefName") != target:
            raise GithubError("pull request #%d for %s has base %r, not %r; refusing to touch it"
                              % (e["number"], branch, e.get("baseRefName"), target))
        if not has_marker(e.get("body") or "", marker):
            raise GithubError("pull request #%d for %s does not carry this project's marker; refusing to edit it"
                              % (e["number"], branch))
    if len(opens) > 1:
        raise GithubError("ambiguous: open pull requests %s share head %s" % (", ".join("#%d" % i["number"] for i in opens), branch))
    existing = opens
    pushed = push_branch(root, wt, branch, cand)
    with _bodyfile(root, body) as bf:
        if existing:
            num = existing[0]["number"]
            _write(root, "pr_edit", ["pr", "edit", str(num), "-R", repo, "--body-file", bf], pr=num, branch=branch)
        else:
            r, amb = _write(root, "pr_create", ["pr", "create", "-R", repo, "--base", target, "--head", branch,
                                                "--title", unit["title"], "--body-file", bf], unit=unit["id"], branch=branch)
            m = PR_URL_RE.search(r["stdout"]) if not amb else None
            if m:
                num = int(m.group(1))
            else:
                found = [f for f in _prs_for_branch(repo, branch, target)  # reconcile by head before any retry
                         if has_marker(f.get("body") or "", marker)]
                if len(found) != 1:
                    raise Ambiguous("creating the pull request for %s has an unknown outcome (%d match the head); "
                                    "rerun 'pr' to reconcile" % (unit["id"], len(found)))
                num = found[0]["number"]
    back = readback_pr(num, repo)
    if back["head_oid"] != cand:
        raise GithubError("pull request #%d head reads %s, expected %s" % (num, back["head_oid"], cand))
    if back["base_ref"] != target:
        raise GithubError("pull request #%d base reads %s, expected %s" % (num, back["base_ref"], target))
    if back["body"].replace("\r\n", "\n").strip() != body.strip():
        raise GithubError("pull request #%d body does not read back as written" % num)
    rec = {"number": num, "state": back["state"], "head_ref": branch, "head_oid": back["head_oid"],
           "base_ref": back["base_ref"], "candidate": cand, "attempt": n, "issue": issue, "push": pushed,
           "closing_issues": back["closing_issues"], "read_at": store.utc_now()}
    store.write_json_atomic(os.path.join(paths.unit_dir(root, plan_id, unit["id"]), "pr.json"), rec)
    store.append_event(root, "pr_recorded", plan_id=plan_id, unit_id=unit["id"], number=num, head_oid=cand)
    return rec


def readback_branch_pr(number, repo):
    """Number, state, head ref and oid, base ref, draft flag, title and body of a pull request (pr units)."""
    d = _read(["pr", "view", str(number), "-R", repo, "--json",
               "number,state,headRefName,headRefOid,baseRefName,isDraft,title,body"], "pr view", dict)
    if not (d.get("number") == number and d.get("state") in ("OPEN", "CLOSED", "MERGED")
            and all(isinstance(d.get(k), str) for k in ("headRefName", "headRefOid", "baseRefName", "title", "body"))
            and isinstance(d.get("isDraft"), bool)):
        raise GithubError("pr view returned a malformed record for #%s" % number)
    return {"number": number, "state": d["state"], "head_ref": d["headRefName"], "head_oid": d["headRefOid"],
            "base_ref": d["baseRefName"], "draft": d["isDraft"], "title": d["title"], "body": d["body"]}


def branch_pr_body(unit, issue, marker):
    """Marker, `Refs #N` when the issue was filed (never `Closes`: the pull request is not merged here), the body."""
    lines = [marker, ""]
    if issue is not None:
        lines += ["Refs #%d" % issue, ""]
    return "\n".join(lines) + "\n" + unit["body"]


def _same_body(a, b):
    return a.replace("\r\n", "\n").strip() == b.replace("\r\n", "\n").strip()


def open_branch_pr(root, plan_id, unit):
    """Open (or reconcile) the pull request of a pr unit for a branch that already exists on origin.

    Reads origin first and never pushes. Closes the unit once the pull request reads back OPEN as specified."""
    if unit["kind"] != "pr":
        raise GithubError("unit %s is not a pr unit" % unit["id"])
    uid = unit["id"]
    plan = approval.effective_plan(root, plan_id)
    repo = plan["repo"]
    head = W.check_branch_name(unit["head_branch"], "head_branch")
    base = W.check_branch_name(unit["base_branch"], "base_branch")
    head_oid = _remote_head(root, head)
    if head_oid is None:
        raise GithubError("head branch %s does not exist on origin; nothing was written" % head)
    if unit.get("head_sha") and head_oid != unit["head_sha"]:
        raise GithubError("origin %s is at %s, not the required head_sha %s; nothing was written"
                          % (head, head_oid, unit["head_sha"]))
    if _remote_head(root, base) is None:
        raise GithubError("base branch %s does not exist on origin; nothing was written" % base)
    issue = (store.read_json(_issues_path(root, plan_id)).get("filed") or {}).get(uid)
    marker = marker_for(root, plan_id, uid)
    body = branch_pr_body(unit, issue, marker)
    opens = [e for e in _prs_for_branch(repo, head) if e.get("state") == "OPEN"]
    for e in opens:
        if e.get("baseRefName") != base:
            raise GithubError("pull request #%d for %s has base %r, not %r; refusing to touch it"
                              % (e["number"], head, e.get("baseRefName"), base))
        if not has_marker(e.get("body") or "", marker):
            raise GithubError("pull request #%d for %s does not carry this unit's marker; refusing to edit it"
                              % (e["number"], head))
    if len(opens) > 1:
        raise GithubError("ambiguous: open pull requests %s share head %s" % (", ".join("#%d" % i["number"] for i in opens), head))
    prec = store.read_json(os.path.join(paths.unit_dir(root, plan_id, uid), "pr.json"), None)
    if not opens and isinstance(prec, dict) and _is_int(prec.get("number")):
        was = readback_branch_pr(prec["number"], repo)  # opened earlier: never open a second one
        raise GithubError("pull request #%d recorded for %s reads %s, not OPEN; refusing to open another"
                          % (prec["number"], uid, was["state"]))
    with _bodyfile(root, body) as bf:
        if opens:
            num = opens[0]["number"]
            if not _same_body(opens[0].get("body") or "", body):
                _write(root, "pr_edit", ["pr", "edit", str(num), "-R", repo, "--body-file", bf], pr=num, branch=head)
        else:
            argv = ["pr", "create", "-R", repo, "--base", base, "--head", head, "--title", unit["title"],
                    "--body-file", bf] + (["--draft"] if unit["draft"] else [])
            r, amb = _write(root, "pr_create", argv, unit=uid, branch=head)
            m = PR_URL_RE.search(r["stdout"]) if not amb else None
            if m:
                num = int(m.group(1))
            else:
                found = [f for f in _prs_for_branch(repo, head, base) if has_marker(f.get("body") or "", marker)]
                if len(found) != 1:
                    raise Ambiguous("creating the pull request for %s has an unknown outcome (%d match the head); "
                                    "rerun 'pr' to reconcile" % (uid, len(found)))
                num = found[0]["number"]
    back = readback_branch_pr(num, repo)
    for what, got, want in (("state", back["state"], "OPEN"), ("head ref", back["head_ref"], head),
                            ("head", back["head_oid"], head_oid), ("base", back["base_ref"], base),
                            ("draft flag", back["draft"], unit["draft"]), ("title", back["title"], unit["title"])):
        if got != want:
            raise GithubError("pull request #%d %s reads %r, expected %r" % (num, what, got, want))
    if not _same_body(back["body"], body):
        raise GithubError("pull request #%d body does not read back as written" % num)
    rec = {"number": num, "state": back["state"], "head_ref": back["head_ref"], "head_oid": back["head_oid"],
           "base_ref": back["base_ref"], "draft": back["draft"], "issue": issue, "read_at": store.utc_now()}
    store.write_json_atomic(os.path.join(paths.unit_dir(root, plan_id, uid), "pr.json"), rec)
    store.append_event(root, "pr_recorded", plan_id=plan_id, unit_id=uid, number=num, head_oid=head_oid, draft=back["draft"])
    st = approval.load_unit_state(root, plan_id, uid)
    if st["closure"].get("state") != "closed":
        st["closure"] = {"state": "closed", "via": "pr opened", "pr": num}
        approval.save_unit_state(root, plan_id, uid, st)
        store.append_event(root, "pr_unit_closed", plan_id=plan_id, unit_id=uid, number=num)
    return rec


# ---- commands ----------------------------------------------------------------

def _plan_id(root, plan):
    if plan:
        return plan
    pdir = paths.plans_dir(root)
    ids = [p for p in sorted(os.listdir(pdir)) if paths.valid_id(p)] if os.path.isdir(pdir) else []
    if len(ids) != 1:
        raise GithubError("%d plans found; pass --plan PLAN_ID" % len(ids))
    return ids[0]


def _unit(root, args):
    pid = args.plan or approval.find_plan_for_unit(root, args.unit)
    approval.require_approval(root, pid)
    return pid, X._unit_of(approval.effective_plan(root, pid), args.unit)


def configure_issues(p):
    sub = p.add_subparsers(dest="issues_cmd", metavar="ACTION")
    sub.required = True
    f = sub.add_parser("file", help="file the approved drafts as GitHub issues and link dependencies")
    f.add_argument("--plan", default=None, metavar="PLAN_ID")
    f.add_argument("--dry-run", action="store_true", help="list actions; write nothing")
    c = sub.add_parser("close", help="close the issue of a job unit (DONE judgment) or a pr unit (pull request open)")
    c.add_argument("unit", metavar="UNIT")
    c.add_argument("--plan", default=None, metavar="PLAN_ID")
    c.add_argument("--comment", required=True)


def _guard(fn):
    try:
        return fn()
    except Ambiguous as e:
        sys.stderr.write("ambiguous: %s\n" % e)
        return 3
    except (GithubError, X.ExecutionError) as e:
        sys.stderr.write("error: %s\n" % e)
        return 1


def run_issues(args):
    root = paths.find_root(args.root)

    def go():
        if args.issues_cmd == "file":
            pid = _plan_id(root, args.plan)
            if args.dry_run:
                file_issues(root, pid, True)
            else:
                with store.ProjectLock(root, "issues file"):
                    file_issues(root, pid)
            return 0
        with store.ProjectLock(root, "issues close"):
            pid, unit = _unit(root, args)
            number, state = close_unit_issue(root, pid, unit, args.comment)
            set_tracker(root, pid, unit["id"], issue=number, state="closed", via="issues close")
        print("#%d %s" % (number, state))
        return 0
    return _guard(go)


def configure_pr(p):
    p.add_argument("unit", metavar="UNIT")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")


def run_pr(args):
    root = paths.find_root(args.root)

    def go():
        with store.ProjectLock(root, "pr"):
            pid, unit = _unit(root, args)
            rec = open_branch_pr(root, pid, unit) if unit["kind"] == "pr" else open_pr(root, pid, unit)
        print(json.dumps(rec, indent=2, sort_keys=True))
        return 0
    return _guard(go)


def register_commands(register):
    register("issues", "file issues from an approved plan, or close a job or pr unit's issue", configure_issues, run_issues)
    register("pr", "open or update a pull request: push a judged code unit's branch, or open a pr unit's existing branch", configure_pr, run_pr)
