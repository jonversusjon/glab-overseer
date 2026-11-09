"""Unit kind `pr`: a pull request for a branch that already exists on origin; unknown plan keys are errors."""
import copy
import hashlib
import json
import os
import re
import unittest

from helpers import base_plan, plan_copy
from test_u5 import ID, jload, sh
from test_u7 import GhCase

from overseer import approval, controller as C, execution as X, github as G, paths, plan as P, store

GOLDEN_DIGEST = "9fc3ac013ad664c4b38c301e9f2fa152f1e8daa02502b53ca9ed3724d0328552"
GOLDEN_PLAN = "09f13dc86b5063ef0a95a460e42e00fc31333bafd0b790b8c34839178a311677"
GOLDEN_DRAFTS = "6dfc75a4749ac3f9c0bec7546068de6ebc913f73d8d4be6c426cb22ebf7f79f4"
SHA_A = "a" * 40


def pr_unit(uid="p1", head="feat/a", **kw):
    u = {"id": uid, "kind": "pr", "title": "PR " + uid, "head_branch": head,
         "body": "Body of %s.\n\nSecond paragraph." % uid}
    u.update(kw)
    return u


def pr_plan(*units, **top):
    p = {"plan_id": "demo", "repo": "lab-org/demo-repo", "target_branch": "main", "units": list(units) or [pr_unit()]}
    p.update(top)
    return p


def errors_of(plan):
    return P.validate_plan(plan)[0]


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Validation(unittest.TestCase):
    def assertError(self, plan, *needles):
        errs = errors_of(plan)
        self.assertTrue(any(all(n in e for n in needles) for e in errs), (needles, errs))

    def test_kind_message_names_all_three(self):
        p = plan_copy()
        p["units"][0]["kind"] = "x"
        self.assertError(p, "kind 'x' must be 'code', 'job' or 'pr'")
        self.assertEqual(P.KINDS, ("code", "job", "pr"))

    def test_minimal_and_full_pr_units_are_valid(self):
        self.assertEqual(P.validate_plan(pr_plan()), ([], []))
        full = pr_unit("p2", "feat/b", head_sha=SHA_A, base_branch="feat/a", draft=True, needs=["p1"])
        self.assertEqual(P.validate_plan(pr_plan(pr_unit(), full)), ([], []))

    def test_plan_without_code_unit_may_omit_scope_or_give_empty(self):
        self.assertEqual(errors_of(pr_plan()), [])
        self.assertEqual(errors_of(pr_plan(scope=[])), [])
        self.assertEqual(P.normalize_plan(pr_plan())["scope"], [])
        self.assertEqual(P.normalize_plan(pr_plan(scope=[]))["scope"], [])
        job = {"id": "j", "kind": "job", "title": "J", "command": "true", "outputs": ["r.txt"]}
        self.assertEqual(errors_of(pr_plan(pr_unit(), job)), [])

    def test_scope_stays_required_with_a_code_unit(self):
        for scope in ("absent", [], None):
            p = plan_copy()
            if scope == "absent":
                del p["scope"]
            else:
                p["scope"] = scope
            self.assertError(p, "missing or empty field 'scope'")

    def test_unit_scope_needs_a_plan_scope(self):
        job = {"id": "j", "kind": "job", "title": "J", "command": "true", "outputs": ["r.txt"], "scope": ["a.txt"]}
        self.assertError(pr_plan(pr_unit(), job), "unit j", "plan declares none")

    def test_required_fields(self):
        for f in ("title", "head_branch", "body"):
            u = pr_unit()
            del u[f]
            self.assertError(pr_plan(u), "unit p1", "missing or empty field '%s'" % f)
        self.assertError(pr_plan(pr_unit(body="  ")), "missing or empty field 'body'")
        self.assertError(pr_plan(pr_unit(body=3)), "field 'body' must be a string")

    def test_forbidden_keys_on_a_pr_unit_are_errors_naming_the_key(self):
        values = {"prompt": "p", "claims": ["c"], "test_command": "true", "command": "true", "outputs": ["a"],
                  "scope": ["a"], "slurm": {"time": "01:00:00", "mem": "1G"}}
        for k, v in values.items():
            self.assertError(pr_plan(pr_unit(**{k: v})), "unit p1", "unknown key(s) %r" % k, "pr unit")

    def test_head_sha(self):
        for bad in ("A" * 40, "a" * 39, "a" * 41, "xyz", 7):
            self.assertError(pr_plan(pr_unit(head_sha=bad)), "head_sha must be 40 lowercase hex")
        self.assertEqual(errors_of(pr_plan(pr_unit(head_sha=SHA_A))), [])

    def test_draft_must_be_a_bool(self):
        for bad in ("yes", 1, "true"):
            self.assertError(pr_plan(pr_unit(draft=bad)), "draft must be true or false")

    def test_branch_names_use_the_existing_check(self):
        self.assertError(pr_plan(pr_unit(head="bad..name")), "head_branch 'bad..name' is not a safe branch name")
        self.assertError(pr_plan(pr_unit(base_branch="x//y")), "base_branch 'x//y' is not a safe branch name")
        self.assertError(pr_plan(pr_unit(head="-rf")), "head_branch")

    def test_head_must_differ_from_base(self):
        self.assertError(pr_plan(pr_unit(head="main")), "head_branch 'main' must differ from its base 'main'")
        self.assertError(pr_plan(pr_unit(head="feat/a", base_branch="feat/a")), "must differ from its base 'feat/a'")
        self.assertEqual(errors_of(pr_plan(pr_unit(head="main", base_branch="dev"))), [])

    def test_unknown_top_level_keys(self):
        p = plan_copy()
        p["merge"] = True
        p["owner"] = "me"
        self.assertError(p, "plan: unknown key(s) 'merge', 'owner'")
        self.assertEqual(errors_of(plan_copy()), [])
        for k in P.PLAN_KEYS:
            self.assertIn(k, ("plan_id", "repo", "target_branch", "scope", "caps", "test_globs", "slurm", "units"))

    def test_unknown_unit_keys_for_each_kind(self):
        for idx, kind, extra in ((0, "code", "command"), (2, "job", "prompt")):
            p = plan_copy()
            p["units"][idx]["merge"] = False
            p["units"][idx][extra] = "x"
            errs = errors_of(p)
            uid = p["units"][idx]["id"]
            self.assertTrue(any(e.startswith("unit %s: unknown key(s) " % uid) and "'merge'" in e and repr(extra) in e
                                and "for a %s unit (allowed: " % kind in e for e in errs), errs)
        self.assertError(pr_plan(pr_unit(merge=False)), "unit p1: unknown key(s) 'merge' for a pr unit (allowed: ")

    def test_allowed_key_sets(self):
        self.assertEqual(P.UNIT_KEYS["code"], ("id", "kind", "title", "prompt", "claims", "test_command", "outputs",
                                               "scope", "needs", "slurm"))
        self.assertEqual(P.UNIT_KEYS["job"], ("id", "kind", "title", "command", "outputs", "scope", "needs", "slurm"))
        self.assertEqual(P.UNIT_KEYS["pr"], ("id", "kind", "title", "head_branch", "head_sha", "base_branch", "draft",
                                             "body", "needs"))

    def test_model_keys_keep_their_message_and_are_not_reported_twice(self):
        p = plan_copy()
        p["units"][0]["model"] = "x"
        p["units"][0]["extra"] = 1
        errs = [e for e in errors_of(p) if e.startswith("unit alpha")]
        self.assertEqual(sum("'model'" in e for e in errs), 1)
        self.assertTrue(any("names a model, provider or family" in e and "'model'" in e for e in errs))
        unknown = [e for e in errs if "unknown key" in e]
        self.assertEqual(len(unknown), 1)
        self.assertIn("'extra'", unknown[0])
        self.assertNotIn("'model'", unknown[0])

    def test_wo011_plan_now_fails_naming_head_branch_draft_and_merge(self):
        plan = {"plan_id": "wo002-record-pr", "repo": "lab-org/demo-repo", "target_branch": "main", "scope": ["**"],
                "units": [{"id": "wo002", "kind": "code", "title": "Record PR for a finished analysis",
                           "prompt": "Make no change of your own. Fast-forward this branch to origin/feat/wo002-attribution and stop.",
                           "claims": ["Retroactive record PR. Draft; not to be merged."],
                           "test_command": "python3 -c pass", "outputs": ["README.md"], "scope": ["**"],
                           "head_branch": "feat/wo002-attribution", "draft": True, "merge": False}]}
        errs = errors_of(plan)
        self.assertEqual(len(errs), 1, errs)
        self.assertEqual(errs[0].split(" (allowed")[0],
                         "unit wo002: unknown key(s) 'draft', 'head_branch', 'merge' for a code unit")
        for k in ("head_branch", "draft", "merge"):
            self.assertIn(repr(k), errs[0])


class Golden(unittest.TestCase):
    def test_base_plan_is_byte_identical_to_the_pre_pr_kind_form(self):
        plan = base_plan()
        norm = P.normalize_plan(plan)
        drafts = P.render_drafts(norm)
        self.assertEqual(P.approval_digest(norm, drafts), GOLDEN_DIGEST)
        self.assertEqual(sha256(P.canonical_json(norm)), GOLDEN_PLAN)
        self.assertEqual(sha256(P.canonical_json(drafts)), GOLDEN_DRAFTS)

    def test_normalized_pr_unit_and_draft(self):
        p = pr_plan(pr_unit("p1", draft=True, head_sha=SHA_A, needs=["p0", "p0"]),
                    pr_unit("p0", "feat/z"), pr_unit("p2", "feat/b", base_branch="feat/a"))
        n = P.normalize_plan(p)
        self.assertEqual(n["units"][0], {"id": "p1", "kind": "pr", "title": "PR p1", "needs": ["p0"],
                                         "head_branch": "feat/a", "base_branch": "main", "draft": True,
                                         "body": "Body of p1.\n\nSecond paragraph.", "head_sha": SHA_A})
        self.assertEqual(sorted(n["units"][1]), ["base_branch", "body", "draft", "head_branch", "id", "kind", "needs",
                                                 "title"])
        self.assertIs(n["units"][1]["draft"], False)
        self.assertEqual(n["units"][2]["base_branch"], "feat/a")
        self.assertEqual(errors_of(n), [])  # the normalized form validates again
        d = P.render_draft("demo", n["units"][0])
        self.assertEqual(d["title"], "PR p1")
        self.assertIn("Kind: pr", d["body"])
        self.assertIn("- head: `feat/a`", d["body"])
        self.assertIn("- base: `main`", d["body"])
        self.assertIn("- draft: true", d["body"])
        self.assertIn("- head commit: `%s`" % SHA_A, d["body"])
        self.assertIn("## Needs\n- p0", d["body"])
        self.assertNotIn("Outputs", d["body"])
        self.assertIn("- draft: false", P.render_draft("demo", n["units"][1])["body"])
        self.assertIn("- none", P.render_draft("demo", n["units"][1])["body"])


class PrCase(GhCase):
    def setUp(self):
        super().setUp()
        self.sha = {}
        self.branch("feat/a")
        self.branch("feat/b", base="feat/a")

    def branch(self, name, base="main"):
        sh(self.root, "checkout", "-q", "-b", name, base)
        self.put("src/%s.txt" % name.replace("/", "_"), name + "\n")
        sh(self.root, *(ID + ["add", "-A"]))
        sh(self.root, *(ID + ["commit", "-q", "-m", "work on " + name]))
        sh(self.root, "push", "-q", "origin", name)
        self.sha[name] = sh(self.root, "rev-parse", "HEAD")
        sh(self.root, "checkout", "-q", "main")

    def start(self, *units, file_issues=True, **top):
        p = pr_plan(*units, **top)
        approval.approve_plan(self.root, p, "me")
        if file_issues:
            self.assertEqual(self.cli("issues", "file")[0], 0)
        return p

    def nxt(self):
        rc, out, err = self.cli("next")
        self.assertEqual(rc, 0, err)
        return out.splitlines()

    def open_pr(self, uid="p1"):
        rc, out, err = self.cli("pr", uid)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def unit_state(self, uid="p1"):
        return approval.load_unit_state(self.root, "demo", uid)

    def writes(self):
        return [c for c in self.calls() if c[:2] in (["pr", "create"], ["pr", "edit"], ["issue", "create"],
                                                      ["issue", "close"], ["issue", "comment"])]


class Opening(PrCase):
    def test_draft_pull_request_with_base_head_title_and_body(self):
        self.start(pr_unit(draft=True))
        rec = self.open_pr()
        create = self.calls("pr", "create")
        self.assertEqual(len(create), 1)
        c = create[0]
        self.assertEqual((c[c.index("-R") + 1], c[c.index("--base") + 1], c[c.index("--head") + 1],
                          c[c.index("--title") + 1]), ("lab-org/demo-repo", "main", "feat/a", "PR p1"))
        self.assertIn("--draft", c)
        pr = self.gh_state()["prs"][0]
        self.assertEqual(pr["body"], "%s\n\nRefs #1\n\nBody of p1.\n\nSecond paragraph." % self.mk("p1"))
        self.assertNotIn("Closes", pr["body"])
        self.assertIs(pr["isDraft"], True)
        self.assertEqual((rec["number"], rec["state"], rec["head_ref"], rec["head_oid"], rec["base_ref"], rec["draft"],
                          rec["issue"]), (1, "OPEN", "feat/a", self.sha["feat/a"], "main", True, 1))
        on_disk = jload(os.path.join(paths.unit_dir(self.root, "demo", "p1"), "pr.json"))
        self.assertEqual(on_disk, rec)
        self.assertTrue(re.match(r"\d{4}-\d\d-\d\dT", rec["read_at"]))
        self.assertEqual(self.unit_state()["closure"], {"state": "closed", "via": "pr opened", "pr": 1})
        kinds = [e["kind"] for e in store.read_events(self.root)]
        self.assertIn("pr_recorded", kinds)
        self.assertIn("pr_unit_closed", kinds)

    def test_non_draft_pull_request_has_no_draft_flag(self):
        self.start(pr_unit())
        self.open_pr()
        self.assertNotIn("--draft", self.calls("pr", "create")[0])
        self.assertIs(self.gh_state()["prs"][0]["isDraft"], False)

    def test_without_a_filed_issue_the_body_has_no_refs_line(self):
        self.start(pr_unit(), file_issues=False)
        self.open_pr()
        self.assertEqual(self.gh_state()["prs"][0]["body"], "%s\n\nBody of p1.\n\nSecond paragraph." % self.mk("p1"))
        self.assertIsNone(jload(os.path.join(paths.unit_dir(self.root, "demo", "p1"), "pr.json"))["issue"])

    def test_never_pushes(self):
        self.start(pr_unit())
        before = sh(self.origin, "for-each-ref", "--format=%(refname) %(objectname)")
        self.open_pr()
        self.assertEqual(sh(self.origin, "for-each-ref", "--format=%(refname) %(objectname)"), before)
        for e in store.read_events(self.root):
            self.assertNotEqual(e.get("op"), "push")

    def test_head_sha_must_match_origin(self):
        self.start(pr_unit(head_sha=self.sha["feat/a"]))
        self.open_pr()
        self.assertEqual(len(self.gh_state()["prs"]), 1)

    def test_head_sha_mismatch_is_refused_with_no_gh_write(self):
        self.start(pr_unit(head_sha=SHA_A), file_issues=False)
        rc, _, err = self.cli("pr", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("head_sha", err)
        self.assertEqual(self.gh_state()["calls"], [])
        self.assertEqual(self.unit_state()["closure"], {"state": "open"})

    def test_missing_head_or_base_is_refused_with_no_gh_write(self):
        self.start(pr_unit(head="feat/none"), file_issues=False)
        rc, _, err = self.cli("pr", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("head branch feat/none does not exist on origin", err)
        self.assertEqual(self.gh_state()["calls"], [])

    def test_missing_base_is_refused_with_no_gh_write(self):
        self.start(pr_unit(base_branch="release/none"), file_issues=False)
        rc, _, err = self.cli("pr", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("base branch release/none does not exist on origin", err)
        self.assertEqual(self.gh_state()["calls"], [])

    def seed(self, **kw):
        pr = {"number": 1, "state": "OPEN", "headRefName": "feat/a", "baseRefName": "main", "title": "PR p1",
              "body": "someone else's", "mergeCommit": None, "isDraft": False}
        pr.update(kw)
        st = self.gh_state()
        st["prs"].append(pr)
        self.gh_state(st)

    def test_existing_unmarked_open_pull_request_is_refused(self):
        self.start(pr_unit(), file_issues=False)
        self.seed()
        rc, _, err = self.cli("pr", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("marker", err)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.gh_state()["prs"][0]["body"], "someone else's")

    def test_existing_marked_pull_request_with_another_base_is_refused(self):
        self.start(pr_unit(), file_issues=False)
        self.seed(baseRefName="feat/b", body=self.mk("p1") + "\n\nBody")
        rc, _, err = self.cli("pr", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("has base 'feat/b'", err)
        self.assertEqual(self.writes(), [])

    def test_a_closed_foreign_pull_request_is_ignored(self):
        self.start(pr_unit(), file_issues=False)
        self.seed(state="CLOSED")
        self.open_pr()
        self.assertEqual(len(self.calls("pr", "create")), 1)

    def test_rerun_reconciles_and_never_opens_a_second(self):
        self.start(pr_unit(draft=True))
        first = self.open_pr()
        again = self.open_pr()
        self.assertEqual(again["number"], first["number"])
        self.assertEqual(len(self.calls("pr", "create")), 1)
        self.assertEqual(self.calls("pr", "edit"), [])
        self.assertEqual(len(self.gh_state()["prs"]), 1)
        self.assertEqual(self.unit_state()["closure"], {"state": "closed", "via": "pr opened", "pr": 1})

    def test_rerun_restores_a_drifted_body(self):
        self.start(pr_unit())
        self.open_pr()
        st = self.gh_state()
        st["prs"][0]["body"] = self.mk("p1") + "\n\nedited by hand"
        self.gh_state(st)
        self.open_pr()
        self.assertEqual(len(self.calls("pr", "edit")), 1)
        self.assertEqual(len(self.gh_state()["prs"]), 1)
        self.assertEqual(self.gh_state()["prs"][0]["body"], "%s\n\nRefs #1\n\nBody of p1.\n\nSecond paragraph." % self.mk("p1"))

    def test_rerun_after_the_pull_request_was_closed_refuses_to_open_another(self):
        self.start(pr_unit())
        self.open_pr()
        st = self.gh_state()
        st["prs"][0]["state"] = "CLOSED"
        self.gh_state(st)
        rc, _, err = self.cli("pr", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("reads CLOSED", err)
        self.assertEqual(len(self.calls("pr", "create")), 1)

    def test_readback_head_mismatch_refuses_and_closes_nothing(self):
        self.start(pr_unit())
        self.fault(wrong_head=SHA_A)
        rc, _, err = self.cli("pr", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("head reads", err)
        self.assertEqual(self.unit_state()["closure"], {"state": "open"})
        self.assertFalse(os.path.exists(os.path.join(paths.unit_dir(self.root, "demo", "p1"), "pr.json")))

    def test_readback_draft_mismatch_refuses(self):
        self.start(pr_unit(draft=True))
        self.seed(body=self.mk("p1") + "\n\nRefs #1\n\nBody of p1.\n\nSecond paragraph.", isDraft=False)
        rc, _, err = self.cli("pr", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("draft flag reads False, expected True", err)
        self.assertEqual(self.unit_state()["closure"], {"state": "open"})

    def test_readback_title_mismatch_refuses(self):
        self.start(pr_unit())
        self.seed(body=self.mk("p1") + "\n\nRefs #1\n\nBody of p1.\n\nSecond paragraph.", title="Other title")
        rc, _, err = self.cli("pr", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("title reads", err)

    def test_code_unit_pr_path_keeps_its_gh_field_list(self):
        import inspect
        src = inspect.getsource(G.readback_pr)
        self.assertIn("number,state,headRefOid,mergeCommit,baseRefName,closingIssuesReferences,title,body", src)
        self.assertNotIn("isDraft", src)


class Stacked(PrCase):
    def test_two_stacked_pr_units_in_one_plan(self):
        self.start(pr_unit("p1", "feat/a", draft=True),
                   pr_unit("p2", "feat/b", base_branch="feat/a", needs=["p1"]))
        self.assertEqual(len(self.nxt()), 1)
        self.assertTrue(self.nxt()[0].startswith("pr p1"))
        self.open_pr("p1")
        lines = self.nxt()
        self.assertEqual(sorted(l.split(":")[0] for l in lines), ["pr p2", "tracker p1"])
        self.open_pr("p2")
        c = self.calls("pr", "create")[1]
        self.assertEqual((c[c.index("--base") + 1], c[c.index("--head") + 1]), ("feat/a", "feat/b"))
        self.assertNotIn("--draft", c)
        prs = self.gh_state()["prs"]
        self.assertEqual([(p["headRefName"], p["baseRefName"], p["isDraft"]) for p in prs],
                         [("feat/a", "main", True), ("feat/b", "feat/a", False)])
        self.assertEqual(sorted(l.split(":")[0] for l in self.nxt()), ["tracker p1", "tracker p2"])

    def test_dependent_stays_unready_until_the_need_is_closed(self):
        self.start(pr_unit("p1", "feat/a"), pr_unit("p2", "feat/b", base_branch="feat/a", needs=["p1"]),
                   file_issues=False)
        self.assertEqual([l.split(":")[0] for l in self.nxt()], ["pr p1"])
        st = self.unit_state("p1")
        st["closure"] = {"state": "closed", "via": "pr opened", "pr": 1}
        approval.save_unit_state(self.root, "demo", "p1", st)
        u = approval.effective_plan(self.root, "demo")["units"][0]
        self.assertTrue(C.is_closed(self.root, "demo", u, self.unit_state("p1")))
        self.assertEqual([l.split(":")[0] for l in self.nxt()], ["pr p2"])


class Next(PrCase):
    def test_pr_then_tracker_then_nothing(self):
        self.start(pr_unit())
        lines = self.nxt()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("pr p1: "), lines)
        self.open_pr()
        self.assertEqual(self.nxt(), ["tracker p1: run `issues close`"])
        rc, out, err = self.cli("issues", "close", "p1", "--comment", "Pull request opened.")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "#1 CLOSED")
        self.assertEqual(self.gh_state()["issues"][0]["state"], "CLOSED")
        self.assertEqual(self.unit_state()["tracker"]["state"], "closed")
        self.assertEqual(self.nxt(), [])

    def test_no_issue_means_nothing_after_the_pull_request(self):
        self.start(pr_unit(), file_issues=False)
        self.open_pr()
        self.assertEqual(self.nxt(), [])

    def test_next_never_offers_worker_or_merge_actions_and_uses_no_slot(self):
        self.start(pr_unit("p1"), pr_unit("p2", "feat/b", base_branch="feat/a"), caps={"workers": 1})
        acts = [json.loads(l) if False else l for l in self.nxt()]
        self.assertEqual([a.split(":")[0] for a in acts], ["pr p1", "pr p2"])
        rc, out, err = self.cli("next", "--json")
        for a in json.loads(out):
            self.assertEqual(a["action"], "pr")
        self.assertEqual(X.slots_in_use(self.root, False, "demo"), 0)

    def test_issues_close_refuses_a_pr_unit_that_is_not_closed(self):
        self.start(pr_unit())
        rc, _, err = self.cli("issues", "close", "p1", "--comment", "x")
        self.assertEqual(rc, 1)
        self.assertIn("no opened pull request", err)
        self.assertEqual(self.gh_state()["issues"][0]["state"], "OPEN")


class Refusals(PrCase):
    def test_dispatch_never_launches_or_allocates_an_attempt(self):
        self.start(pr_unit(), file_issues=False)
        rc, out, err = self.cli("dispatch")
        self.assertEqual((rc, out), (0, ""), err)
        rc, out, err = self.cli("dispatch", "--unit", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("pr unit", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")
        self.assertEqual(self.unit_state()["counters"]["attempts"], 0)
        self.assertFalse(os.path.exists(os.path.join(paths.unit_dir(self.root, "demo", "p1"), "attempts")))
        with self.assertRaises(X.ExecutionError):
            X.allocate_attempt(self.root, "demo", "p1", False)
        self.assertEqual(self.unit_state()["counters"]["attempts"], 0)

    def test_every_worker_command_refuses_a_pr_unit(self):
        self.start(pr_unit(), file_issues=False)
        prompt = os.path.join(self.tmp, "prompt.txt")
        with open(prompt, "w") as f:
            f.write("fix it")
        cmds = [("judge", "p1"), ("resolve", "p1", "--as", "failed", "--reason", "r", "--evidence", "e"),
                ("review", "p1"), ("fix", "p1", "--prompt-file", prompt),
                ("reproduce", "p1", "--finding", "f1", "--command", "true"), ("dispositions", "p1", "--state"),
                ("nonvacuous", "p1"), ("merge", "p1")]
        for cmd in cmds:
            rc, out, err = self.cli(*cmd)
            self.assertEqual(rc, 1, (cmd, out, err))
            self.assertIn("pr unit", err, cmd)
            self.assertNotIn("Traceback", err)
        self.assertEqual(self.gh_state()["calls"], [])
        self.assertEqual(self.unit_state()["counters"]["attempts"], 0)

    def test_merge_refuses_even_after_the_pull_request_is_open(self):
        self.start(pr_unit())
        self.open_pr()
        rc, _, err = self.cli("merge", "p1")
        self.assertEqual(rc, 1)
        self.assertIn("never merges", err)
        self.assertEqual(self.calls("pr", "merge"), [])
        self.assertEqual(self.unit_state()["closure"]["state"], "closed")


class StatusAndReport(PrCase):
    def test_status_shows_kind_closure_and_pull_request_number(self):
        self.start(pr_unit())
        rc, out, err = self.cli("status")
        self.assertIn("p1 (pr)", out)
        self.assertNotIn("pr=#", out)
        self.open_pr()
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)
        line = [l for l in out.splitlines() if "p1 (pr)" in l][0]
        self.assertIn("closure=closed", line)
        self.assertIn("pr=#1", line)
        self.assertIn("attempts=0", line)
        rc, out, err = self.cli("status", "--json")
        u = json.loads(out)["plans"]["demo"]["units"]["p1"]
        self.assertEqual((u["kind"], u["closure"]["state"], u["pr"]), ("pr", "closed", 1))

    def report(self, *flags):
        out_dir = os.path.join(self.tmp, "rep" + "".join(flags).replace("-", ""))
        rc, out, err = self.cli("report", *flags, "--out", out_dir)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(out_dir, "report.md")) as f:
            md = f.read()
        return jload(os.path.join(out_dir, "report.json")), md

    def test_closed_pr_unit_is_closed_opened_and_not_merged(self):
        self.start(pr_unit(draft=True))
        self.open_pr()
        for flags in (("--public",), ()):
            rep, md = self.report(*flags)
            u = rep["plans"][0]["units"][0]
            self.assertEqual((u["kind"], u["state"], u["facets"]["closure"]), ("pr", "closed", "closed"))
            self.assertEqual((u["pr"]["number"], u["pr"]["opened"], u["pr"]["merged"], u["pr"]["draft"]),
                             (1, True, False, True))
            self.assertEqual(u["pr"]["head"], self.sha["feat/a"])
            self.assertEqual(u["merge"], None)
            self.assertEqual(rep["incomplete"], [])
            self.assertIn("opened, not merged (draft)", md)
            self.assertNotIn("DONE, not merged", md)

    def test_unclosed_pr_unit_is_listed_as_incomplete_without_crashing(self):
        self.start(pr_unit(), pr_unit("p2", "feat/b", base_branch="feat/a", needs=["p1"]))
        self.open_pr("p1")
        rep, _ = self.report("--public")
        self.assertEqual([(i["unit"], i["item"]) for i in rep["incomplete"]], [("p2", "state pending")])
        u = rep["plans"][0]["units"][1]
        self.assertEqual((u["state"], u["pr"], u["attempts"]), ("pending", None, []))


class Split(PrCase):
    CODE = {"id": "a", "kind": "code", "title": "A", "prompt": "p", "claims": ["c"], "test_command": "true",
            "outputs": ["src/a.py", "src/b.py"], "scope": ["src/**"], "needs": []}

    def kids(self):
        return [{"id": "a1", "outputs": ["src/a.py"]}, {"id": "a2", "outputs": ["src/b.py"]}]

    def test_split_refuses_a_pr_unit(self):
        self.start(self.CODE, pr_unit(needs=["a"]), file_issues=False, scope=["src/**"])
        spec = os.path.join(self.tmp, "spec.json")
        with open(spec, "w") as f:
            json.dump({"children": [{"id": "x1", "outputs": ["a.txt"]}]}, f)
        rc, out, err = self.cli("plan", "split", "p1", "--into", spec, "--reason", "why")
        self.assertEqual(rc, 1)
        self.assertIn("pr unit", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(approval.load_revisions(self.root, "demo"), [])

    def test_check_split_and_build_children_refuse_a_pr_parent(self):
        parent = P.normalize_unit(pr_unit(), "main")
        with self.assertRaises(approval.OwnerStop):
            approval.check_split(parent, [])
        with self.assertRaises(approval.PlanStateError):
            approval._build_children(parent, {"children": [{"id": "x", "outputs": ["a"]}]})

    def test_splitting_a_code_unit_of_a_plan_with_pr_units_works(self):
        self.start(self.CODE, pr_unit(needs=["a"]), file_issues=False, scope=["src/**"])
        approval.split_unit(self.root, "demo", "a", {"children": self.kids()}, "too big")
        eff = approval.effective_plan(self.root, "demo")
        self.assertEqual([u["id"] for u in eff["units"]], ["a1", "a2", "p1"])
        self.assertEqual(P.validate_plan(eff), ([], []))
        self.assertEqual(self.cli("next")[0], 0)
        self.assertEqual(self.cli("status")[0], 0)

    def test_split_children_still_ignore_extra_keys(self):
        self.start(self.CODE, file_issues=False, scope=["src/**"])
        kids = self.kids()
        kids[0]["note"] = "ignored as before"
        rev = approval.split_unit(self.root, "demo", "a", {"children": kids}, "too big")
        self.assertNotIn("note", rev["children"][0])


if __name__ == "__main__":
    unittest.main()
