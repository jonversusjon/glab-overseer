import unittest

from helpers import TmpCase, plan_copy

from overseer import plan as P


def errs(plan):
    return P.validate_plan(plan)[0]


class ValidateTests(unittest.TestCase):
    def assertError(self, plan, needle):
        e = errs(plan)
        self.assertTrue(any(needle in line for line in e), "%r not in %r" % (needle, e))

    def unit(self, plan, uid):
        return next(u for u in plan["units"] if u["id"] == uid)

    def test_base_plan_valid(self):
        self.assertEqual(P.validate_plan(plan_copy()), ([], []))

    def test_bad_plan_id(self):
        for bad in ("Bad", "../x", "a b", "", "-a", "a" * 65):
            p = plan_copy()
            p["plan_id"] = bad
            self.assertError(p, "plan_id %r does not match" % bad)

    def test_bad_unit_id(self):
        for bad in ("A", "a/b", "..", "x" * 65):
            p = plan_copy()
            self.unit(p, "alpha")["id"] = bad
            self.assertError(p, "unit id %r does not match" % bad)

    def test_duplicate_unit_id(self):
        p = plan_copy()
        self.unit(p, "beta")["id"] = "alpha"
        self.assertError(p, "duplicate unit id 'alpha'")

    def test_unknown_needs(self):
        p = plan_copy()
        self.unit(p, "beta")["needs"] = ["ghost"]
        self.assertError(p, "unit beta: needs unknown unit 'ghost'")

    def test_cycle(self):
        p = plan_copy()
        self.unit(p, "alpha")["needs"] = ["crunch"]
        self.assertError(p, "dependency cycle among units: alpha, beta, crunch")

    def test_bad_kind(self):
        p = plan_copy()
        self.unit(p, "alpha")["kind"] = "pipeline"
        self.assertError(p, "unit alpha: kind 'pipeline' must be 'code', 'job' or 'pr'")

    def test_code_missing_fields(self):
        for f in ("title", "prompt", "claims", "test_command", "outputs", "scope"):
            p = plan_copy()
            del self.unit(p, "alpha")[f]
            self.assertError(p, "unit alpha: missing or empty field '%s'" % f)

    def test_code_empty_claims(self):
        p = plan_copy()
        self.unit(p, "alpha")["claims"] = []
        self.assertError(p, "unit alpha: missing or empty field 'claims'")

    def test_job_missing_fields(self):
        for f in ("title", "command", "outputs"):
            p = plan_copy()
            del self.unit(p, "crunch")[f]
            self.assertError(p, "unit crunch: missing or empty field '%s'" % f)

    def test_model_provider_family_fields(self):
        for f in ("model", "provider", "family", "model_family"):
            p = plan_copy()
            self.unit(p, "alpha")[f] = "x"
            self.assertError(p, "unit alpha: field %r names a model, provider or family" % f)

    def test_unsafe_output_paths(self):
        for bad in ("/abs/x", "../x", "a/../../x"):
            p = plan_copy()
            self.unit(p, "alpha")["outputs"] = [bad]
            self.assertError(p, "unit alpha: output path %r must be repo-relative" % bad)
            p = plan_copy()
            self.unit(p, "crunch")["outputs"] = [{"path": bad}]
            self.assertError(p, "unit crunch: output path %r must be repo-relative" % bad)

    def test_unsafe_scope_globs(self):
        for bad in ("/abs/**", "../x/*"):
            p = plan_copy()
            self.unit(p, "alpha")["scope"] = [bad]
            self.assertError(p, "unit alpha: scope glob %r must be repo-relative" % bad)
            p = plan_copy()
            p["scope"] = [bad]
            self.assertError(p, "plan: scope glob %r must be repo-relative" % bad)

    def test_unit_scope_outside_plan_scope(self):
        p = plan_copy()
        self.unit(p, "alpha")["scope"] = ["other/x.md", "src/ok.py"]
        self.assertError(p, "unit alpha: scope glob 'other/x.md' is not covered by the plan scope")
        self.assertFalse(any("src/ok.py" in e for e in errs(p)))

    def test_scope_cover_rule(self):
        self.assertTrue(P.scope_covers(["src/**"], "src/a/b.py"))
        self.assertTrue(P.scope_covers(["src/**"], "src/a/*.py"))
        self.assertTrue(P.scope_covers(["src/**"], "src/**"))
        self.assertTrue(P.scope_covers(["**"], "any/where.py"))
        self.assertTrue(P.scope_covers(["docs/*.md"], "docs/a.md"))
        self.assertTrue(P.scope_covers(["src/a?.py"], "src/a?.py"))
        self.assertTrue(P.scope_covers(["x.txt"], "x.txt"))
        self.assertFalse(P.scope_covers(["src/**"], "srcx/a.py"))
        self.assertFalse(P.scope_covers(["src/**"], "tests/a.py"))

    def test_scope_cover_does_not_cross_slash(self):
        self.assertFalse(P.scope_covers(["docs/*.md"], "docs/secret/x.md"))
        self.assertFalse(P.scope_covers(["src/a?.py"], "src/a*.py"))
        self.assertFalse(P.scope_covers(["src/*"], "src/a/b.py"))

    def test_unit_glob_wider_than_plan_glob_rejected_with_advice(self):
        p = plan_copy()
        p["scope"] = ["src/a?.py", "src/beta.py", "src/alpha_data.txt", "tests/**"]
        self.unit(p, "alpha")["scope"] = ["src/a*.py"]
        self.assertError(p, "scope glob 'src/a*.py' is not covered by the plan scope; "
                            "list the literal paths, or widen the plan scope to a dir/** glob")

    def test_narrower_glob_conservatively_rejected(self):
        p = plan_copy()
        p["scope"] = ["src/a?.py", "src/beta.py", "tests/**"]
        self.unit(p, "alpha")["scope"] = ["src/a[bc].py"]
        self.assertError(p, "scope glob 'src/a[bc].py' is not covered")

    def test_non_list_fields_rejected_not_coerced(self):
        for field, val in (("outputs", "/tmp/evil"), ("scope", "src/**"), ("claims", "works"),
                           ("needs", "alpha")):
            p = plan_copy()
            self.unit(p, "beta")[field] = val
            self.assertTrue(errs(p), field)
        p = plan_copy()
        self.unit(p, "beta")["outputs"] = "/tmp/evil"
        self.assertError(p, "unit beta: outputs must be a list")
        p = plan_copy()
        self.unit(p, "crunch")["outputs"] = {"path": "x"}
        self.assertError(p, "unit crunch: outputs must be a list")
        p = plan_copy()
        p["test_globs"] = "tests/**"
        self.assertError(p, "test_globs entry must be a list of strings")
        p = plan_copy()
        p["scope"] = "src/**"
        self.assertError(p, "plan: scope glob must be a list of strings")
        p = plan_copy()
        p["units"] = {"id": "alpha"}
        self.assertError(p, "plan has no units")
        p = plan_copy()
        self.unit(p, "alpha")["title"] = ["x"]
        self.assertError(p, "unit alpha: field 'title' must be a string")

    def test_output_entry_types(self):
        for entry, needle in (
            ({"path": 5}, "needs a string 'path'"),
            ({"min_bytes": 1}, "needs a string 'path'"),
            ({"path": "a", "min_bytes": -1}, "min_bytes must be a non-negative integer"),
            ({"path": "a", "min_bytes": "3"}, "min_bytes must be a non-negative integer"),
            ({"path": "a", "sha256": "abc"}, "sha256 must be 64 lowercase hex digits"),
            ({"path": "a", "validate": 3}, "validate must be a string"),
            ({"path": "a", "validator": "x"}, "unknown keys ['validator']"),
            (7, "output entries must be strings or objects"),
        ):
            p = plan_copy()
            self.unit(p, "crunch")["outputs"] = [entry]
            self.assertError(p, needle)
        p = plan_copy()
        self.unit(p, "crunch")["outputs"] = [{"path": "a", "min_bytes": 0, "sha256": "0" * 64, "validate": "true"}]
        self.assertEqual(errs(p), [])

    def test_model_key_detection_is_exact(self):
        for ok in ("modelling", "remodel", "familyname", "providers_list", "effort_estimate"):
            p = plan_copy()
            self.unit(p, "alpha")[ok] = "x"
            # not a model key; it is still an unknown key, which `plan validate` rejects on its own
            self.assertFalse([e for e in errs(p) if "names a model" in e], ok)
            self.assertTrue([e for e in errs(p) if "unknown key(s) %r" % ok in e], ok)
        for bad in ("model", "models", "provider", "family", "thinking", "effort", "agent_model",
                    "x_provider", "tier_family", "Model"):
            p = plan_copy()
            self.unit(p, "alpha")[bad] = "x"
            self.assertError(p, "field %r names a model, provider or family" % bad)

    def test_duplicate_output_path_in_unit(self):
        p = plan_copy()
        self.unit(p, "alpha")["outputs"] = ["src/alpha.py", {"path": "src/alpha.py", "min_bytes": 1}]
        self.assertError(p, "unit alpha: output path 'src/alpha.py' is declared more than once")

    def test_model_prefix_keys_and_job_scope(self):
        for bad in ("model_name", "provider_id", "family_hint"):
            p = plan_copy()
            self.unit(p, "alpha")[bad] = "x"
            self.assertError(p, "field %r names a model" % bad)
        for bad, needle in (("/abs/x", "must be repo-relative"), ("../x", "must be repo-relative"),
                            ("other/x", "is not covered by the plan scope")):
            p = plan_copy()
            self.unit(p, "crunch")["scope"] = [bad]
            self.assertError(p, needle)
        p = plan_copy()
        self.unit(p, "crunch")["scope"] = ["src/x.py"]
        self.assertEqual(errs(p), [])

    def test_slurm_null_and_blank_count_as_absent(self):
        for key in ("mem", "time"):
            for bad in (None, "", "  "):
                p = plan_copy()
                p["slurm"][key] = bad
                self.assertTrue(errs(p), (key, bad))
        p = plan_copy()
        p["slurm"]["mem_per_cpu"] = ""
        self.assertEqual(errs(p), [])  # blank mem_per_cpu is absent, so no "both"
        p["slurm"]["mem"] = 4
        self.assertError(p, "slurm mem must be a non-empty string")
        for bad in (0, -1, "4", True):
            p = plan_copy()
            p["slurm"]["cpus"] = bad
            self.assertError(p, "slurm cpus must be a positive integer")

    def test_slurm_checked_for_code_units_too(self):
        p = plan_copy()
        del p["slurm"]
        self.unit(p, "alpha")["slurm"] = {"time": "01:00:00"}
        self.assertError(p, "unit alpha: slurm block needs one of mem or mem_per_cpu")
        p = plan_copy()
        del p["slurm"]
        self.unit(p, "alpha")["slurm"] = {"mem": "1G", "mem_per_cpu": "1G"}
        self.assertError(p, "unit alpha: slurm block has both mem and mem_per_cpu")
        p = plan_copy()
        self.unit(p, "beta")["slurm"] = {"time": "UNLIMITED"}
        self.assertError(p, "unit beta: slurm block needs a finite time")

    def test_slurm_both_mem(self):
        p = plan_copy()
        self.unit(p, "crunch")["slurm"] = {"mem": "1G", "mem_per_cpu": "1G"}
        self.assertError(p, "unit crunch: slurm block has both mem and mem_per_cpu")

    def test_slurm_neither_mem(self):
        p = plan_copy()
        del p["slurm"]["mem"]
        self.assertError(p, "unit crunch: slurm block needs one of mem or mem_per_cpu")

    def test_slurm_finite_time(self):
        for bad in (None, "UNLIMITED", "infinite", "0", "soon", -5, True):
            p = plan_copy()
            if bad is None:
                del p["slurm"]["time"]
            else:
                p["slurm"]["time"] = bad
            self.assertError(p, "unit crunch: slurm block needs a finite time")
        for good in ("30", "01:30:00", "2-00", "1-12:30", 90):
            p = plan_copy()
            p["slurm"]["time"] = good
            self.assertEqual(errs(p), [], good)

    def test_slurm_unit_override_swaps_memory_key(self):
        p = plan_copy()
        self.unit(p, "crunch")["slurm"] = {"mem_per_cpu": "2G", "cpus": 4}
        self.assertEqual(errs(p), [])

    def test_job_without_any_slurm_block_is_ok(self):
        p = plan_copy()
        del p["slurm"]
        self.assertEqual(errs(p), [])

    def test_caps_above_limits(self):
        for k, v in (("workers", 4), ("attempts", 3), ("review_rounds", 4)):
            p = plan_copy()
            p["caps"] = {k: v}
            self.assertError(p, "caps.%s %d exceeds the maximum" % (k, v))

    def test_caps_lower_allowed(self):
        p = plan_copy()
        p["caps"] = {"workers": 1, "attempts": 1, "review_rounds": 1}
        self.assertEqual(errs(p), [])
        p["caps"] = {"workers": 0}
        self.assertError(p, "caps.workers must be an integer >= 1")

    def test_overlapping_output_warning(self):
        p = plan_copy()
        p["units"].append(dict(self.unit(p, "alpha"), id="gamma", needs=[], outputs=["src/alpha.py"],
                               scope=["src/alpha.py"]))
        e, w = P.validate_plan(p)
        self.assertEqual(e, [])
        self.assertEqual(len(w), 1)
        self.assertIn("units alpha and gamma have no dependency path but both declare output 'src/alpha.py'", w[0])

    def test_no_warning_with_dependency_path(self):
        p = plan_copy()
        self.unit(p, "beta")["outputs"] = ["src/alpha.py"]
        self.assertEqual(P.validate_plan(p), ([], []))


class ValidateCliTests(TmpCase):
    def test_exit_codes_and_lines(self):
        r = self.cli("plan", "validate", self.write_plan())
        self.assertEqual(r.returncode, 0, r.stderr)
        p = plan_copy()
        p["units"][0]["kind"] = "x"
        p["units"][1]["needs"] = ["ghost"]
        r = self.cli("plan", "validate", self.write_plan(p))
        self.assertEqual(r.returncode, 1)
        lines = [x for x in r.stderr.splitlines() if x]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(x.startswith("error: ") for x in lines))

    def test_warning_exits_zero_on_stderr(self):
        p = plan_copy()
        p["units"].append(dict(p["units"][0], id="gamma", needs=[], outputs=["src/alpha.py"],
                               scope=["src/alpha.py"]))
        r = self.cli("plan", "validate", self.write_plan(p))
        self.assertEqual(r.returncode, 0)
        self.assertIn("warning: units alpha and gamma have no dependency path", r.stderr)

    def test_unreadable_and_bad_json(self):
        r = self.cli("plan", "validate", "/nonexistent/plan.json")
        self.assertEqual(r.returncode, 1)
        bad = self.write_plan({"x": 1})
        with open(bad, "w") as f:
            f.write("{nope")
        self.assertEqual(self.cli("plan", "validate", bad).returncode, 1)


if __name__ == "__main__":
    unittest.main()
