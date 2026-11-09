"""Execution core and local backend: attempts, invocations, slots, wait and abort.

Layout under an attempt: `inv/<seq>-<kind>/` holds one record set per invocation:
intent.json (written BEFORE the launch), launch.json (after), start.json and
exit.json (written by the generated run.sh, keyed by token and argv digest),
cancel.json (abort), plus run.sh, rec.py, stdout.log, stderr.log, run.log.

Functions that write state expect the caller to hold `store.ProjectLock`.
Liveness is never one signal: RUNNING needs a live pid whose start identity
matches launch.json; EXITED needs an exit.json matching token and argv digest;
anything else with no live process is UNCERTAIN and holds its slot until an
operator resolves or aborts it. Reconciliation never relaunches.
"""
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid

from . import approval, env as E, paths, plan as P, store

SHELL = "/bin/sh"  # run.sh interpreter; the process search matches exactly `SHELL run.sh`
PROJECT_SLOTS = P.CAP_MAX["workers"]  # project-wide limit across plans (O12)
KINDS = ("author", "fixer", "job")
ACTIVE = ("QUEUED", "RUNNING", "UNCERTAIN", "CANCELLING")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_PREFIX = "_OVR_"
RESERVED_NAMES = frozenset("PPID UID EUID GROUPS SHELLOPTS BASHOPTS BASHPID FUNCNAME LINENO RANDOM SECONDS PWD OLDPWD "
                           "IFS PATH HOME SHELL OPTIND OPTARG PS1 PS2 PS4 ENV".split())
TOKEN_VAR = "OVERSEER_INV_TOKEN"  # exported by run.sh; every payload process inherits it
BLOCKING_CLASSES = ("QUOTA", "AUTH")
SIGNALS = ("TERM", "INT", "HUP")
BAD = object()  # a record file exists but is unreadable or not an object
ATTEMPT_RE = re.compile(r"^a([1-9][0-9]*)$")
INV_RE = re.compile(r"^([0-9]{2,})-(author|fixer|job)$")


class ExecutionError(ValueError):
    pass


class SlotsFull(ExecutionError):
    pass


class LaunchRejected(ExecutionError):
    """The backend refused the launch outright: nothing started (recorded as launch_error.json)."""


def _load(path):
    """Record dict; None when absent; BAD when present but unusable."""
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return BAD
    return v if isinstance(v, dict) else BAD


def _int(v, lo=1):
    return type(v) is int and v >= lo


def argv_digest(argv):
    return hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---- process identity ------------------------------------------------------

def _ps_exe():
    """`ps` from fixed absolute locations, independent of the (scrubbed) worker PATH."""
    for cand in ("/bin/ps", "/usr/bin/ps"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


_UNUSABLE = object()  # sysctl cannot be used here; callers fall back to ps
_KINFO_PROC_SIZE = 648  # sizeof(struct kinfo_proc) on 64-bit macOS


def _darwin_start(pid):
    """(zombie, "sec.usec") from sysctl KERN_PROC_PID on macOS, None if there is no such
    process, or _UNUSABLE when sysctl cannot be called or returns a layout not known here.

    `ps -o lstart` has one-second resolution, so a pid reused within the same second as the
    recorded start would pass as the recorded process. kinfo_proc.kp_proc begins with the
    p_starttime timeval (16 bytes on 64-bit), and p_stat is the char at offset 36.
    """
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        buf = ctypes.create_string_buffer(1024)
        size = ctypes.c_size_t(ctypes.sizeof(buf))
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
            return _UNUSABLE
    except (OSError, AttributeError, TypeError, OverflowError):
        return _UNUSABLE
    if size.value == 0:
        return None
    if size.value != _KINFO_PROC_SIZE:
        return _UNUSABLE
    sec = int.from_bytes(buf.raw[0:8], sys.byteorder, signed=True)
    usec = int.from_bytes(buf.raw[8:12], sys.byteorder, signed=True)
    if sec <= 0 or not 0 <= usec < 1000000:
        return _UNUSABLE
    return buf.raw[36] == 5, "%d.%06d" % (sec, usec)  # SZOMB is 5


SYSCTL_START_RE = re.compile(r"^[0-9]+\.[0-9]{6}$")


def proc_start(pid, allow_zombie=False, like=None):
    """Start identity of `pid` (Linux: /proc stat starttime; macOS: sysctl p_starttime in
    microseconds; elsewhere: ps lstart), else None.

    `like` is a recorded identity to compare against: on macOS its format picks the source,
    so an identity recorded by ps (older code, or a lookup where sysctl was unusable) is
    still checked with ps. A sysctl record cannot be checked by ps; such a lookup does not
    match, which reads as "not the recorded process" and never leads to a signal."""
    if not _int(pid):
        return None
    if os.path.isdir("/proc/self"):
        try:
            with open("/proc/%d/stat" % pid) as f:
                data = f.read()
            rest = data[data.rindex(")") + 2:].split()
            if rest[0] == "Z" and not allow_zombie:
                return None
            return rest[19]
        except (OSError, ValueError, IndexError):
            return None
    if sys.platform == "darwin" and (like is None or SYSCTL_START_RE.match(like)):
        got = _darwin_start(pid)
        if got is None:
            return None
        if got is not _UNUSABLE:
            return None if got[0] and not allow_zombie else got[1]
    ps = _ps_exe()
    if ps is None:
        return None
    r = E.run_tool([ps, "-o", "stat=,lstart=", "-p", str(pid)], E.worker_env(), 10)
    parts = r["stdout"].strip().split(None, 1) if r["rc"] == 0 else []
    if len(parts) != 2 or (parts[0].startswith("Z") and not allow_zombie):
        return None
    return parts[1].strip()


_CHILDREN = []  # local launches made by this process, polled so they never linger as zombies


def _reap(pid):
    _CHILDREN[:] = [c for c in _CHILDREN if c.poll() is None]


def identified_alive(pid, start):
    if not (_int(pid) and isinstance(start, str) and start):
        return False
    _reap(pid)
    return proc_start(pid, like=start) == start


def find_by_script(script):
    """Pids of live processes running `script` (sh run.sh), by command line.

    Without /proc, `ps` flattens argv, so a path containing whitespace cannot be matched
    unambiguously: adoption is disabled and such an invocation stays UNCERTAIN until an
    orchestrator resolves it.
    """
    found = []
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    with open("/proc/%s/cmdline" % name, "rb") as f:
                        args = f.read().decode("utf-8", "replace").split("\0")
                except OSError:
                    continue
                if args and args[-1] == "":
                    args.pop()
                if args == [SHELL, script]:
                    found.append(int(name))
        return sorted(found)
    ps = _ps_exe()
    if ps is not None and not any(c.isspace() for c in script):
        r = E.run_tool([ps, "-axo", "pid=,command="], E.worker_env(), 15)
        for line in r["stdout"].splitlines():
            p = line.strip().split(None, 1)
            if len(p) == 2 and p[0].isdigit() and p[1].strip() == "%s %s" % (SHELL, script):
                found.append(int(p[0]))
    return sorted(found)


# ---- invocation records ----------------------------------------------------

def _valid_intent(intent, inv):
    argv = intent.get("argv")
    return bool(isinstance(intent.get("token"), str) and intent["token"]
                and isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)
                and intent.get("argv_sha256") == argv_digest(argv)
                and intent.get("kind") == os.path.basename(inv).split("-", 1)[-1]
                and isinstance(intent.get("backend"), str) and isinstance(intent.get("run_sh"), str))


def _valid_launch(launch, intent):
    if launch is BAD or launch.get("token") != intent["token"]:
        return False
    if intent["backend"] == "local":
        return bool(_int(launch.get("pid")) and _int(launch.get("pgid"))
                    and isinstance(launch.get("start"), str) and launch["start"])
    if intent["backend"] == "slurm":
        jid = launch.get("job_id")
        return isinstance(jid, str) and jid.isdigit()
    return True


def _exit_matches(ex, intent):
    return (ex is not BAD and ex.get("token") == intent["token"]
            and ex.get("argv_sha256") == intent["argv_sha256"]
            and type(ex.get("rc")) is int and ex.get("signal") in (None,) + SIGNALS)


def _cancel_pending(cancel, intent):
    return (isinstance(cancel, dict) and cancel.get("token") == intent["token"]
            and isinstance(cancel.get("requested_at"), str) and "unidentified" not in cancel
            and "confirmed_at" not in cancel)


def _reconcile_launch(inv, intent, write):
    """Find a live run.sh for an intent without launch.json; never launches anything."""
    if intent["backend"] != "local":
        hook = RECONCILE.get(intent["backend"])
        return hook(inv, intent, write) if hook else None
    cands = find_by_script(intent["run_sh"])
    st = _load(os.path.join(inv, "start.json"))
    if isinstance(st, dict) and st.get("token") == intent["token"] and st.get("pid") in cands:
        cands = [st["pid"]]
    if len(cands) != 1:  # zero or several matches: never guess
        return None
    for pid in cands:
        start = proc_start(pid)
        if start is None:
            continue
        try:
            pgid = os.getpgid(pid)
        except OSError:
            continue
        launch = {"token": intent["token"], "pid": pid, "pgid": pgid, "start": start,
                  "backend": intent["backend"], "time": store.utc_now(), "reconciled": True}
        if write:
            store.write_json_atomic(os.path.join(inv, "launch.json"), launch)
        return launch
    return None


def _probe_local(inv, intent, launch, write=False):
    if not identified_alive(launch["pid"], launch["start"]):
        return "GONE"
    st = _load(os.path.join(inv, "start.json"))
    if isinstance(st, dict) and st.get("token") == intent["token"] and st.get("pid") != launch["pid"]:
        return "GONE"
    return "RUNNING"


# backend -> fn(inv, intent, launch, write) -> RUNNING|QUEUED|GONE, or a complete status dict
PROBES = {"local": _probe_local}
RECONCILE = {}  # backend -> fn(inv, intent, write) -> launch dict or None (find a launch by token)
CANCEL_CHECK = {}  # backend -> fn(inv, intent, cancel) -> True once a pending cancel is confirmed


def invocation_status(inv, write=False):
    """`_invocation_status`, except that an orchestrator resolution (resolved.json bound to the
    invocation token, written by `resolve`) turns UNCERTAIN or CANCELLING into RESOLVED, which
    is not active and so releases the slot."""
    st = _invocation_status(inv, write)
    if st["state"] in ("UNCERTAIN", "CANCELLING"):
        intent, res = _load(os.path.join(inv, "intent.json")), _load(os.path.join(inv, "resolved.json"))
        if (isinstance(intent, dict) and isinstance(res, dict) and isinstance(intent.get("token"), str)
                and res.get("token") == intent["token"] and res.get("as") in ("done", "failed")):
            return {"state": "RESOLVED", "as": res["as"]}
    return st


def _invocation_status(inv, write=False):
    """State of one invocation directory: dict with `state` and details.

    States: NOT_LAUNCHED (no intent, nothing started), LAUNCH_FAILED, QUEUED, RUNNING,
    EXITED (with rc, signal), CANCELLED, UNCERTAIN. `write` lets reconciliation record
    a found launch.json.
    """
    intent = _load(os.path.join(inv, "intent.json"))
    if intent is None:
        return {"state": "NOT_LAUNCHED", "reason": "no intent.json"}
    if intent is BAD or not _valid_intent(intent, inv):
        return {"state": "UNCERTAIN", "reason": "intent.json is invalid"}
    cancel = _load(os.path.join(inv, "cancel.json"))
    if isinstance(cancel, dict) and cancel.get("token") == intent["token"] and "unidentified" in cancel:
        # an unidentifiable payload may still run: exit.json cannot clear it, only an orchestrator resolve.
        # A batch job whose id turns up later (squeue token, own start.json) is identified again and the
        # cancel request stays outstanding, so a later abort verifies and cancels it.
        if not (write and intent["backend"] != "local" and _load(os.path.join(inv, "launch.json")) is None
                and _reconcile_launch(inv, intent, True)):
            return {"state": "UNCERTAIN", "reason": "cancel unidentified: %s" % cancel["unidentified"]}
        cancel = {"token": intent["token"], "requested_at": cancel.get("requested_at") or store.utc_now()}
        store.write_json_atomic(os.path.join(inv, "cancel.json"), cancel)
    if _cancel_pending(cancel, intent):  # an abort is outstanding: hold the slot until it confirms
        check = CANCEL_CHECK.get(intent["backend"])
        if not (write and check and check(inv, intent, cancel)):
            return {"state": "CANCELLING"}
        cancel = _load(os.path.join(inv, "cancel.json"))
    ex = _load(os.path.join(inv, "exit.json"))
    if intent["backend"] == "local" and ex is not None and _exit_matches(ex, intent):
        return {"state": "EXITED", "rc": ex["rc"], "signal": ex.get("signal")}
    stale = " (exit.json does not match this invocation)" if ex is not None else ""
    fail = _load(os.path.join(inv, "launch_error.json"))
    if isinstance(fail, dict) and fail.get("token") == intent["token"]:
        return {"state": "LAUNCH_FAILED", "reason": str(fail.get("error"))}
    launch = _load(os.path.join(inv, "launch.json"))
    if launch is None:
        launch = _reconcile_launch(inv, intent, write)
        if launch is None:
            started = os.path.exists(os.path.join(inv, "start.json"))
            return {"state": "UNCERTAIN", "reason": ("process gone before launch.json was written" if started
                    else "intent without launch and no running process") + stale}
    elif not _valid_launch(launch, intent):
        return {"state": "UNCERTAIN", "reason": "launch.json is invalid or belongs to another invocation"}
    probe = PROBES.get(intent["backend"])
    s = probe(inv, intent, launch, write) if probe else "GONE"
    if isinstance(s, dict):
        return s
    if s in ("RUNNING", "QUEUED"):
        return {"state": s, "pid": launch.get("pid")}
    if isinstance(cancel, dict) and cancel.get("token") == intent["token"] \
            and isinstance(cancel.get("confirmed_at"), str):
        return {"state": "CANCELLED"}
    return {"state": "UNCERTAIN", "reason": "process gone and no matching exit.json" + stale}


def _sorted_dirs(parent, rx):
    try:
        names = os.listdir(parent)
    except OSError:
        return []
    return sorted((n for n in names if rx.match(n)), key=lambda n: int(n.split("-")[0].lstrip("a")))


def iter_invocations(root, plan_id=None, unit_id=None):
    pdir = paths.plans_dir(root)
    for pid in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        if not paths.valid_id(pid) or plan_id not in (None, pid):
            continue
        udir = os.path.join(paths.plan_dir(root, pid), "units")
        for uid in sorted(os.listdir(udir)) if os.path.isdir(udir) else []:
            if not paths.valid_id(uid) or unit_id not in (None, uid):
                continue
            adir = os.path.join(udir, uid, "attempts")
            for a in _sorted_dirs(adir, ATTEMPT_RE):
                for name in _sorted_dirs(os.path.join(adir, a, "inv"), INV_RE):
                    m = INV_RE.match(name)
                    yield {"plan_id": pid, "unit_id": uid, "attempt": int(a[1:]), "seq": name,
                           "kind": m.group(2), "path": os.path.join(adir, a, "inv", name)}


def active_invocations(root, plan_id=None, unit_id=None, write=False):
    out = []
    for i in iter_invocations(root, plan_id, unit_id):
        st = invocation_status(i["path"], write)
        if st["state"] in ACTIVE:
            out.append(dict(i, status=st))
    return out


def slots_in_use(root, write=False, plan_id=None):
    """Author, fixer and job invocations QUEUED, RUNNING or UNCERTAIN (one plan, or all plans)."""
    return sum(1 for i in active_invocations(root, plan_id, write=write) if i["kind"] in KINDS)


def check_slots(root, plan_id, cap):
    """Refuse when the plan's own cap or the project-wide limit is reached, naming which."""
    mine = slots_in_use(root, True, plan_id)
    if mine >= cap:
        raise SlotsFull("plan %s has %d of its %d worker slots in use" % (plan_id, mine, cap))
    total = slots_in_use(root, True)
    if total >= PROJECT_SLOTS:
        raise SlotsFull("the project-wide limit of %d worker slots is reached (%d in use)" % (PROJECT_SLOTS, total))


# ---- attempts --------------------------------------------------------------

def record_classification(root, plan_id, unit_id, n, cls):
    """Record how an attempt ended (for example QUOTA or AUTH); read by allocate_attempt."""
    if not isinstance(cls, str) or not cls:
        raise ExecutionError("classification must be a non-empty string")
    store.write_json_atomic(os.path.join(paths.attempt_dir(root, plan_id, unit_id, n), "classification.json"),
                            {"class": cls, "time": store.utc_now()})


def _unit_of(plan, unit_id):
    for u in plan["units"]:
        if u["id"] == unit_id:
            return u
    raise ExecutionError("unit %r is not in plan %s" % (unit_id, plan["plan_id"]))


def refuse_pr_unit(unit, what):
    """A pr unit only opens a pull request: no worker, attempt, judgment, review or merge ever applies to it."""
    if unit["kind"] == "pr":
        raise ExecutionError("unit %s is a pr unit; it %s (use `pr %s`, then `issues close`)"
                             % (unit["id"], what, unit["id"]))
    return unit


def _counter(st):
    n = st["counters"].get("attempts")
    if not _int(n, 0):
        raise ExecutionError("unit.json counters.attempts is %r, not a non-negative integer" % (n,))
    return n


def _admit(root, plan_id, unit_id, after_quota):
    """Admission checks shared by allocation and reuse; returns (plan, unit state, last attempt)."""
    approval.require_approval(root, plan_id)
    plan = approval.effective_plan(root, plan_id)
    refuse_pr_unit(_unit_of(plan, unit_id), "has no worker or attempt")
    st = approval.load_unit_state(root, plan_id, unit_id)
    if st["closure"].get("state") in approval.CLOSED_STATES:
        raise ExecutionError("unit %s is %s" % (unit_id, st["closure"]["state"]))
    last = _counter(st)
    for i in iter_invocations(root, plan_id, unit_id):
        s = invocation_status(i["path"], True)
        if s["state"] in ACTIVE:
            raise ExecutionError("attempt a%d of %s has an invocation %s that is %s"
                                 % (i["attempt"], unit_id, i["seq"], s["state"]))
    if last:
        c = _load(os.path.join(paths.attempt_dir(root, plan_id, unit_id, last), "classification.json"))
        if (c is BAD or (c and c.get("class") in BLOCKING_CLASSES)) and not after_quota:
            raise ExecutionError("attempt a%d of %s ended with %s; pass --after-quota to try again"
                                 % (last, unit_id, "an unreadable classification" if c is BAD else c["class"]))
    return plan, st, last


def allocate_attempt(root, plan_id, unit_id, after_quota=False):
    """Create attempts/a<N> exclusively; returns (N, directory)."""
    plan, st, last = _admit(root, plan_id, unit_id, after_quota)
    n = last + 1
    if n > plan["caps"]["attempts"]:
        raise ExecutionError("unit %s would need attempt %d, above the attempts cap %d"
                             % (unit_id, n, plan["caps"]["attempts"]))
    adir = paths.attempt_dir(root, plan_id, unit_id, n)
    os.makedirs(os.path.dirname(adir), exist_ok=True)
    marker = os.path.join(adir, "allocated.json")
    try:
        os.mkdir(adir)
        store.write_json_atomic(marker, {"attempt": n, "token": uuid.uuid4().hex, "time": store.utc_now()})
    except FileExistsError:
        m = _load(marker)  # a crash between mkdir and the counter bump leaves a marked directory: adopt it
        if m is None and not os.listdir(adir):  # a crash before the marker leaves an empty one: no evidence in it
            store.write_json_atomic(marker, {"attempt": n, "token": uuid.uuid4().hex, "time": store.utc_now()})
        elif not (isinstance(m, dict) and m.get("attempt") == n):
            raise ExecutionError("attempt directory %s already exists and is not a crashed allocation" % adir)
    st["counters"]["attempts"] = n
    st["execution"] = {"state": "allocated", "attempt": n}
    approval.save_unit_state(root, plan_id, unit_id, st)
    store.append_event(root, "attempt_allocated", plan_id=plan_id, unit_id=unit_id, attempt=n)
    return n, adir


# ---- launching -------------------------------------------------------------

_REC = '''import json, os, socket, sys, tempfile, time
def w(path, rec):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rec.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
a = sys.argv
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
if a[1] == "start":
    w(a[2] + "/start.json", {"token": a[3], "pid": int(a[4]), "host": socket.gethostname(), "time": now,
                             "job_id": os.environ.get("SLURM_JOB_ID")})
else:
    rec = {"token": a[3], "argv_sha256": a[4], "rc": int(a[5]), "signal": a[6] or None, "end_time": now}
    if len(a) > 7 and a[7]:
        rec["error"] = a[7]
    w(a[2] + "/exit.json", rec)
'''


def _run_sh(inv, intent, stdin_path):
    q = shlex.quote
    batch = intent["backend"] != "local"
    check = ""
    if batch:  # in a batch job the script must see this very invocation's token file before anything runs
        check = ('_OVR_t=; [ -r %s ] && IFS= read -r _OVR_t <%s\n[ "$_OVR_t" = "$_OVR_TOKEN" ] || '
                 '{ _OVR_rc=97; _OVR_note=identity_mismatch; exit 97; }\n'
                 % (q(os.path.join(inv, "token")), q(os.path.join(inv, "token"))))
    exports = ""
    for k, v in sorted((intent.get("env_extras") or {}).items()):
        exports += "%s=%s; export %s\n" % (k, q(v), k)
    return """#!%s
_OVR_PY=%s; _OVR_REC=%s; _OVR_INV=%s; _OVR_TOKEN=%s; _OVR_SHA=%s
_OVR_sig=; _OVR_rc=1; _OVR_note=
_ovr_fin() { trap - EXIT; "$_OVR_PY" -I "$_OVR_REC" exit "$_OVR_INV" "$_OVR_TOKEN" "$_OVR_SHA" "$_OVR_rc" "$_OVR_sig" "$_OVR_note"; }
trap _ovr_fin EXIT
trap '_OVR_rc=$?; [ "$_OVR_rc" -eq 0 ] && _OVR_rc=143; _OVR_sig=TERM; exit "$_OVR_rc"' TERM
trap '_OVR_rc=$?; [ "$_OVR_rc" -eq 0 ] && _OVR_rc=130; _OVR_sig=INT; exit "$_OVR_rc"' INT
trap '_OVR_rc=$?; [ "$_OVR_rc" -eq 0 ] && _OVR_rc=129; _OVR_sig=HUP; exit "$_OVR_rc"' HUP
%scd %s || { _OVR_rc=126; exit 126; }
"$_OVR_PY" -I "$_OVR_REC" start "$_OVR_INV" "$_OVR_TOKEN" "$$"
%s%s=$_OVR_TOKEN; export %s
%s <%s >"$_OVR_INV/stdout.log" 2>"$_OVR_INV/stderr.log"
_OVR_rc=$?
exit "$_OVR_rc"
""" % (SHELL, q(sys.executable), q(os.path.join(inv, "rec.py")), q(inv), q(intent["token"]), q(intent["argv_sha256"]),
       check, q(intent["cwd"]), exports, TOKEN_VAR, TOKEN_VAR, " ".join(q(a) for a in intent["argv"]), q(stdin_path))


def _write_scripts(inv, intent, stdin_text):
    stdin_path = os.devnull
    if stdin_text is not None:
        stdin_path = os.path.join(inv, "stdin.txt")
        with open(stdin_path, "w", encoding="utf-8") as f:
            f.write(stdin_text)
    if intent["backend"] != "local":
        with open(os.path.join(inv, "token"), "w") as f:
            f.write(intent["token"] + "\n")
    with open(os.path.join(inv, "rec.py"), "w") as f:
        f.write(_REC)
    with open(intent["run_sh"], "w") as f:
        f.write(_run_sh(inv, intent, stdin_path))
    os.chmod(intent["run_sh"], 0o755)


def _spawn_local(inv, run_sh, envd, cwd):
    with open(os.path.join(inv, "run.log"), "ab") as log:
        p = subprocess.Popen([run_sh], cwd=cwd, env=envd, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)
    _CHILDREN.append(p)
    return {"pid": p.pid}


def launch_invocation(root, plan_id, unit_id, attempt, kind, argv, cwd, backend="local",
                      stdin_text=None, env=None, extras=None, spawn=None, extra_intent=None, prepare=None):
    """Launch one author, fixer or job invocation; returns its record (intent plus launch plus `dir`).

    The caller holds the project lock and has allocated the attempt. `argv[0]` is
    resolved to an absolute path on the scrubbed worker PATH. `spawn(inv, run_sh, env, cwd)`
    returns launch fields (including `pid` for the local backend); the default is the
    detached local spawn, which other backends replace together with a PROBES entry.
    `prepare(inv)` (with `argv` None) runs once the invocation directory exists and returns
    (argv, stdin_text), so a task file can be written inside that directory.
    """
    if kind not in KINDS:
        raise ExecutionError("kind must be one of %s" % ", ".join(KINDS))
    if not (isinstance(cwd, str) and os.path.isabs(cwd) and os.path.isdir(cwd)):
        raise ExecutionError("cwd must be an existing absolute directory")
    if not (isinstance(backend, str) and backend):
        raise ExecutionError("backend must be a non-empty string")
    if stdin_text is not None and not isinstance(stdin_text, str):
        raise ExecutionError("stdin_text must be a string")
    for k, v in (extras or {}).items():
        if not (isinstance(k, str) and ENV_NAME_RE.match(k) and not k.startswith(RESERVED_PREFIX)
                and k != TOKEN_VAR and k not in RESERVED_NAMES and not k.startswith("BASH") and isinstance(v, str) and "\0" not in v):
            raise ExecutionError("invalid environment variable %r: names must be shell identifiers, must not "
                                 "start with %s or equal %s, and values must be strings" % (k, RESERVED_PREFIX, TOKEN_VAR))
    if backend == "local" and kind != "job":
        problem = slurm.local_agent_problem(root)  # the supervisor's own environment, not the payload's
        if problem:
            raise ExecutionError(problem)
    approval.require_approval(root, plan_id)
    cap = approval.effective_plan(root, plan_id)["caps"]["workers"]
    adir = paths.attempt_dir(root, plan_id, unit_id, attempt)
    if not os.path.isdir(adir):
        raise ExecutionError("attempt a%d of %s is not allocated" % (attempt, unit_id))
    check_slots(root, plan_id, cap)
    envd = E.worker_env(base=env, extras=extras)

    def resolved(argv, stdin_text):
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) and "\0" not in a for a in argv)):
            raise ExecutionError("argv must be a non-empty list of strings")
        if stdin_text is not None and not isinstance(stdin_text, str):
            raise ExecutionError("stdin_text must be a string")
        exe = E.resolve_exe(argv[0], envd.get("PATH", ""), cwd)
        if exe is None:
            raise ExecutionError("command %r not found (only absolute PATH entries are searched)" % argv[0])
        return [exe] + argv[1:]
    if prepare is None:
        argv = resolved(argv, stdin_text)
    invroot = os.path.join(adir, "inv")
    os.makedirs(invroot, exist_ok=True)
    names = _sorted_dirs(invroot, INV_RE)
    seq = "%02d-%s" % ((int(names[-1].split("-")[0]) + 1) if names else 1, kind)
    inv = os.path.join(invroot, seq)
    os.mkdir(inv)
    if prepare is not None:
        argv, stdin_text = prepare(inv)
        argv = resolved(argv, stdin_text)
    intent = {"token": uuid.uuid4().hex, "kind": kind, "argv": argv, "argv_sha256": argv_digest(argv),
              "backend": backend, "cwd": cwd, "time": store.utc_now(), "run_sh": os.path.join(inv, "run.sh"),
              "plan_id": plan_id, "unit_id": unit_id, "attempt": attempt}
    intent.update(extra_intent or {})
    _write_scripts(inv, intent, stdin_text)
    store.write_json_atomic(os.path.join(inv, "intent.json"), intent)  # the intent precedes the effect
    store.append_event(root, "invocation_intent", plan_id=plan_id, unit_id=unit_id, attempt=attempt,
                       invocation=seq, backend=backend, token=intent["token"])
    try:
        fields = (spawn or _spawn_local)(inv, intent["run_sh"], envd, cwd)
    except OSError as e:
        store.write_json_atomic(os.path.join(inv, "launch_error.json"), {"token": intent["token"], "error": str(e)})
        raise ExecutionError("launch failed: %s" % e)
    except LaunchRejected as e:
        store.write_json_atomic(os.path.join(inv, "launch_error.json"), {"token": intent["token"], "error": str(e)})
        raise
    launch = {"token": intent["token"], "backend": backend, "time": store.utc_now()}
    launch.update(fields)
    if backend == "local":
        pid = launch["pid"]
        launch["start"] = proc_start(pid, allow_zombie=True)
        try:
            launch["pgid"] = os.getpgid(pid)
        except OSError:
            launch["pgid"] = pid
        if launch["start"] is None:  # an unidentifiable worker must not be left running
            try:
                os.killpg(launch["pgid"], signal.SIGKILL) if launch["pgid"] == pid else os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            err = "no start identity for pid %d; the spawned process was terminated" % pid
            store.write_json_atomic(os.path.join(inv, "launch_error.json"), {"token": intent["token"], "error": err})
            raise ExecutionError("launch failed: " + err)
    store.write_json_atomic(os.path.join(inv, "launch.json"), launch)
    return dict(intent, launch=launch, dir=inv, seq=seq)


def acquire_attempt(root, plan_id, uid, after_quota=False):
    """The attempt to launch into: an allocated one that never launched anything is reused, not burned."""
    last = _admit(root, plan_id, uid, after_quota)[2]
    if last and all(invocation_status(i["path"], True)["state"] in ("NOT_LAUNCHED", "LAUNCH_FAILED")
                    for i in iter_invocations(root, plan_id, uid) if i["attempt"] == last) \
            and not os.path.exists(os.path.join(paths.attempt_dir(root, plan_id, uid, last), "classification.json")):
        return last, paths.attempt_dir(root, plan_id, uid, last)
    return allocate_attempt(root, plan_id, uid, after_quota)


def missing_merges(root, plan_id, plan, unit):
    """(unit, merge commit) for each merged code unit in `unit`'s transitive needs whose merge
    commit the project checkout's HEAD does not contain (including one not fetched at all)."""
    from . import workspace as W
    seen, todo, missing = set(), list(unit.get("needs") or []), []
    while todo:
        uid = todo.pop()
        if uid in seen:
            continue
        seen.add(uid)
        dep = _unit_of(plan, uid)
        todo.extend(dep.get("needs") or [])
        closure = approval.load_unit_state(root, plan_id, uid)["closure"]
        sha = closure.get("merge_commit")
        if dep["kind"] != "code" or closure.get("state") != "merged" or not isinstance(sha, str):
            continue
        r = W.git(root, "merge-base", "--is-ancestor", sha, "HEAD", ok=(0, 1, 128))
        if r["rc"] != 0:  # 1: not an ancestor; 128: not a commit here (never fetched)
            missing.append((uid, sha))
    return sorted(missing)


def dispatch_job(root, plan_id, unit, after_quota=False):
    """Run a job unit's command in the project checkout with OVERSEER_OUT set.

    The command runs on whatever the checkout holds, and merges land on the remote target, so
    a job that depends on merged code units is refused until the checkout contains them."""
    plan = approval.effective_plan(root, plan_id)
    uid = unit["id"]
    stale = missing_merges(root, plan_id, plan, unit)
    if stale:
        from . import workspace as W
        target = shlex.quote(plan["target_branch"])  # the advice is meant to be pasted into a shell
        fix = "on branch %s: `git pull --ff-only origin %s`" % (target, target)
        try:  # git before 2.15 has no --is-shallow-repository: the advice then omits unshallow
            shallow = W.git(root, "rev-parse", "--is-shallow-repository", ok=(0,))["stdout"].strip() == "true"
        except W.GitError:
            shallow = False
        if shallow:  # ancestry cannot be shown across a shallow boundary, and pull does not lift it
            fix = "the checkout is shallow, so first `git fetch --unshallow origin`, then " + fix
        raise ExecutionError(
            "the project checkout does not contain the merge of %s, so job %s could run on pre-merge "
            "files; bring the checkout up to the target (%s), then dispatch again"
            % (", ".join("%s (%s)" % (u, s[:12]) for u, s in stale), uid, fix))
    backend = slurm.choose_backend(root)
    if backend == "slurm":
        slurm.preflight(root, plan, unit)  # refuses before any attempt is allocated or sbatch runs
    check_slots(root, plan_id, plan["caps"]["workers"])
    n, adir = acquire_attempt(root, plan_id, uid, after_quota)
    out = os.path.join(adir, "outputs")
    os.makedirs(out, exist_ok=True)
    sh = E.resolve_exe("sh", E.worker_env().get("PATH", ""))
    if sh is None:
        raise ExecutionError("sh not found on an absolute PATH entry")
    argv = [sh, "-c", unit["command"]]
    if backend == "slurm":
        return slurm.launch(root, plan_id, uid, n, "job", argv, root, extras={"OVERSEER_OUT": out})
    return launch_invocation(root, plan_id, uid, n, "job", argv, root, extras={"OVERSEER_OUT": out})


# ---- abort -----------------------------------------------------------------

class PidList(list):
    unknown = False  # True when one of our own processes could not be inspected


def _in_scope(name, scope):
    """True when /proc/<name> is in the wrapper's session or process group (stat stays readable)."""
    try:
        with open("/proc/%s/stat" % name) as f:
            data = f.read()
        rest = data[data.rindex(")") + 2:].split()
        return int(rest[3]) == scope[0] or int(rest[2]) == scope[1]
    except (OSError, ValueError, IndexError):
        return False


def _token_pids(token, scope=None):
    """Our-uid processes carrying this invocation's token in their environment; None without /proc.

    Other users' processes are ignored. If a process of ours has an unreadable environment
    and lies in the wrapper's session or process group (`scope` = (sid, pgid); with no scope
    every such process counts), the result is flagged `unknown`: it may be a payload that made
    itself undumpable. Unrelated undumpable processes of ours (agents, browsers) are not.
    """
    if not os.path.isdir("/proc/self"):
        return None
    want = ("%s=%s" % (TOKEN_VAR, token)).encode()
    out = PidList()
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            if os.stat("/proc/%s" % name).st_uid != os.getuid():
                continue
            with open("/proc/%s/environ" % name, "rb") as f:
                data = f.read()
        except (FileNotFoundError, ProcessLookupError):
            continue  # exited meanwhile
        except OSError:
            if scope is None or _in_scope(name, scope):
                out.unknown = True
            continue
        if want in data.split(b"\0"):
            out.append(int(name))
    return out


def abort_invocation(root, inv, grace=10.0, sleep=time.sleep, confirm_wait=5.0):
    """SIGTERM, escalate to SIGKILL after `grace`, confirm only once everything is gone.

    While the wrapper's start identity matches, its process group is signalled. After it
    is gone, members are found by the invocation token in /proc/<pid>/environ and signalled
    one by one (a group is never signalled on a bare pgid, which could have been reused).
    Without /proc nothing else can be identified: a dead wrapper then yields
    `cancel_unidentified`; while the wrapper lives the group is signalled, and afterwards the
    group is only probed (`killpg(pgid, 0)`, never a real signal), so survivors keep the
    abort unconfirmed. A child that calls setsid() can escape that probe (cooperative
    boundary C20). cancel.json gets requested_at before any signal and confirmed_at only when
    the wrapper is gone and no process carries the token; an unreadable environment of one
    of our processes counts as alive (`cancel_unconfirmed_reason`).
    """
    st = invocation_status(inv, True)
    if st["state"] not in ACTIVE:
        return st["state"]
    intent = _load(os.path.join(inv, "intent.json"))
    if intent["backend"] == "slurm":
        return slurm.abort(root, inv, intent, confirm_wait=grace, sleep=sleep)
    if intent["backend"] != "local":
        raise ExecutionError("abort is not supported for backend %r here" % intent["backend"])
    launch = _load(os.path.join(inv, "launch.json"))
    ref = {"plan_id": intent["plan_id"], "unit_id": intent["unit_id"], "attempt": intent["attempt"],
           "invocation": os.path.basename(inv)}
    cpath = os.path.join(inv, "cancel.json")
    reason = None
    if launch is None or not _valid_launch(launch, intent):
        reason = "no identifiable launch (no launch.json and no matching process); a payload may still run"
    else:
        def wrapper():
            return identified_alive(launch["pid"], launch["start"])
        if not wrapper() and _token_pids(intent["token"]) is None:
            reason = "the wrapper is gone and this host cannot list processes; a payload may still run"
    if reason:
        store.write_json_atomic(cpath, {"token": intent["token"], "requested_at": store.utc_now(),
                                        "unidentified": reason})
        store.append_event(root, "cancel_unidentified", reason=reason, **ref)
        return "UNIDENTIFIED"
    cancel = _load(cpath)
    if not _cancel_pending(cancel, intent):
        cancel = {"token": intent["token"], "requested_at": store.utc_now()}
        store.write_json_atomic(cpath, cancel)
        store.append_event(root, "cancel_requested", **ref)

    def group_probe():
        if launch["pgid"] != launch["pid"] or launch["pgid"] == os.getpgrp():
            return False
        try:
            os.killpg(launch["pgid"], 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True

    scope = (launch["pid"], launch["pgid"])

    def alive():
        if wrapper():
            return True
        pids = _token_pids(intent["token"], scope)
        if pids is None:
            return group_probe()
        return bool(pids) or pids.unknown

    def send(sig):
        try:
            if wrapper():
                if launch["pgid"] == launch["pid"] and launch["pgid"] != os.getpgrp():
                    os.killpg(launch["pgid"], sig)
                else:
                    os.kill(launch["pid"], sig)
            else:
                for pid in _token_pids(intent["token"], scope) or []:
                    os.kill(pid, sig)
        except ProcessLookupError:
            pass

    if alive():
        send(signal.SIGTERM)
    deadline = time.monotonic() + grace
    while alive() and time.monotonic() < deadline:
        sleep(0.1)
    if alive():
        send(signal.SIGKILL)
        end = time.monotonic() + confirm_wait
        while alive() and time.monotonic() < end:
            sleep(0.05)
    if alive():
        pids = _token_pids(intent["token"], scope)
        if pids is not None and not wrapper() and not pids and pids.unknown:
            cancel["cancel_unconfirmed_reason"] = "a process of ours has an unreadable environment"
            store.write_json_atomic(cpath, cancel)
        return "UNCONFIRMED"
    cancel["confirmed_at"] = store.utc_now()
    store.write_json_atomic(cpath, cancel)
    store.append_event(root, "cancel_confirmed", **ref)
    return invocation_status(inv, True)["state"]


# ---- wait ------------------------------------------------------------------

def _snap(root, write):
    return dict(((i["plan_id"], i["unit_id"], i["attempt"], i["seq"]), (i, invocation_status(i["path"], write)))
                for i in iter_invocations(root))


def _snapshot(root):
    try:
        with store.ProjectLock(root, "wait"):
            return _snap(root, True)
    except store.LockHeld:
        return _snap(root, False)


def wait_for_change(root, timeout=1800.0, poll=15.0, emit=None, sleep=time.sleep):
    """Poll until an invocation appears, changes state or leaves the active set. 0 on change, 3 on timeout.

    The whole project is re-read on every poll. With nothing active at the start it returns 0 at once.
    """
    emit = emit or (lambda line: print(line, flush=True))
    base = _snapshot(root)
    before = dict((k, v[1]["state"]) for k, v in base.items())
    if not any(st in ACTIVE for st in before.values()):
        sys.stderr.write("no active invocations\n")
        return 0
    end = time.monotonic() + timeout
    while True:
        cur = _snapshot(root)
        now = dict((k, v[1]["state"]) for k, v in cur.items())
        changed = [k for k in sorted(set(before) | set(now))
                   if before.get(k) != now.get(k) and (k not in before or before[k] in ACTIVE or now.get(k) in ACTIVE)]
        for k in changed:
            i, st = cur[k] if k in cur else (base[k][0], {})
            emit(json.dumps({"plan": k[0], "unit": k[1], "attempt": k[2], "invocation": k[3], "kind": i["kind"],
                             "from": before.get(k), "to": now.get(k), "rc": st.get("rc"),
                             "signal": st.get("signal")}, sort_keys=True))
        if changed:
            return 0
        if time.monotonic() >= end:
            sys.stderr.write("timeout after %gs\n" % timeout)
            return 3
        sleep(min(poll, max(0.0, end - time.monotonic())))


# ---- commands --------------------------------------------------------------

def configure_dispatch(p):
    p.add_argument("--unit", default=None, metavar="UNIT", help="omit to dispatch every READY unit up to the free slots")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")
    p.add_argument("--after-quota", action="store_true", help="allow a new attempt after a QUOTA or AUTH failure")


def run_dispatch(args):
    root = paths.find_root(args.root)
    if args.unit is None:
        from . import controller
        return controller.dispatch_ready(root, args.plan, args.after_quota)
    with store.ProjectLock(root, "dispatch"):
        pid = args.plan or approval.find_plan_for_unit(root, args.unit)
        from . import controller
        problems = controller.owner_problems(root, pid)  # the same owner checks as dispatch without a unit
        if problems:
            controller.report_owner_stops(root, "dispatch", pid, problems)
            return 1
        approval.require_approval(root, pid)
        unit = refuse_pr_unit(_unit_of(approval.effective_plan(root, pid), args.unit), "is never dispatched")
        if unit["kind"] == "code":
            from . import workspace
            rec = workspace.dispatch_code(root, pid, unit, args.after_quota)
        else:
            rec = dispatch_job(root, pid, unit, args.after_quota)
    print(json.dumps({"plan": pid, "unit": unit["id"], "attempt": rec["attempt"], "invocation": rec["seq"],
                      "pid": rec["launch"].get("pid"), "job_id": rec["launch"].get("job_id")}, sort_keys=True))
    return 0


def configure_wait(p):
    p.add_argument("--timeout", type=float, default=1800.0, metavar="S")
    p.add_argument("--poll", type=float, default=15.0, metavar="S")


def run_wait(args):
    if args.timeout < 0 or args.poll <= 0:
        raise ExecutionError("--timeout must be >= 0 and --poll > 0")
    return wait_for_change(paths.find_root(args.root), args.timeout, args.poll)


def configure_abort(p):
    p.add_argument("unit", metavar="UNIT")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")
    p.add_argument("--grace", type=float, default=10.0, metavar="S", help="seconds before SIGKILL")


def run_abort(args):
    root = paths.find_root(args.root)
    with store.ProjectLock(root, "abort"):
        pid = args.plan or approval.find_plan_for_unit(root, args.unit)
        todo = active_invocations(root, pid, args.unit, True)
        if not todo:
            print("nothing to abort for %s" % args.unit)
            return 0
        bad = 0
        for i in todo:
            res = abort_invocation(root, i["path"], args.grace)
            print("%s/a%d/%s: %s" % (args.unit, i["attempt"], i["seq"], res))
            bad += res in ("UNCONFIRMED", "UNIDENTIFIED")
    return 1 if bad else 0


def configure_status(p):
    p.add_argument("--json", action="store_true", dest="as_json")


def collect_status(root, write=False):
    plans = {}
    pdir = paths.plans_dir(root)
    for pid in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        if not (paths.valid_id(pid) and os.path.exists(os.path.join(pdir, pid, "plan.json"))):
            continue
        plan = approval.effective_plan(root, pid)
        act = active_invocations(root, pid, write=write)
        units = {}
        for u in plan["units"]:
            st = approval.load_unit_state(root, pid, u["id"])
            units[u["id"]] = dict(
                dict((f, st[f]) for f in approval.FACETS), kind=u["kind"], attempts=st["counters"]["attempts"],
                **({"pr": st["closure"].get("pr")} if u["kind"] == "pr" else {}),
                active=[{"attempt": i["attempt"], "invocation": i["seq"], "kind": i["kind"],
                         "state": i["status"]["state"], "pid": i["status"].get("pid"),
                         "job_id": i["status"].get("job_id")}
                        for i in act if i["unit_id"] == u["id"]])
        plans[pid] = {"workers_cap": plan["caps"]["workers"], "slots_in_use": slots_in_use(root, write, pid),
                      "units": units}
    return {"slots_in_use": slots_in_use(root, write), "project_limit": PROJECT_SLOTS, "plans": plans}


def run_status(args):
    root = paths.find_root(args.root)
    try:
        with store.ProjectLock(root, "status"):
            data = collect_status(root, True)
    except store.LockHeld:
        data = collect_status(root, False)
    if args.as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    for pid, p in data["plans"].items():
        print("plan %s: slots %d/%d (project %d/%d)" % (pid, p["slots_in_use"], p["workers_cap"],
                                                          data["slots_in_use"], data["project_limit"]))
        for uid, u in p["units"].items():
            print("  %s (%s) auth=%s exec=%s closure=%s tracker=%s attempts=%d" % (
                uid, u["kind"], u["authorization"].get("state"), u["execution"].get("state"),
                u["closure"].get("state"), u["tracker"].get("state", "-"), u["attempts"])
                  + (" pr=#%s" % u["pr"] if u["kind"] == "pr" and u.get("pr") is not None else ""))
            for a in u["active"]:
                print("    a%d/%s %s %s" % (a["attempt"], a["invocation"], a["state"],
                                   "job=%s" % a["job_id"] if a.get("job_id") else "pid=%s" % a.get("pid")))
    return 0

from . import slurm  # noqa: E402  registers the slurm hooks; slurm imports this module
