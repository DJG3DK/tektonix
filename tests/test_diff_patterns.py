"""Three shapes of a wrong fix the ship gate reads off the diff
(agent/nodes/diff_patterns.py), calibrated on the two 50-task samples of
2026-09-25 that lost the same tasks the same way."""
import json
import pathlib
import subprocess

from agent.nodes import diff_patterns as dp

ISSUE = "TimeSeries.remove_column('flux') raises a misleading exception saying 'time' is missing."

NEW_MESSAGE = '''diff --git a/astropy/timeseries/core.py b/astropy/timeseries/core.py
--- a/astropy/timeseries/core.py
+++ b/astropy/timeseries/core.py
@@ -70,6 +70,12 @@ class BaseTimeSeries(QTable):
                 raise ValueError("{} object is invalid - expected '{}' as the first column{} but found '{}'"
                                  .format(self.__class__.__name__, required_columns[0], plural, self.colnames[0]))
+                if self.colnames[0] == required_columns[0]:
+                    missing = [c for c in required_columns if c not in self.colnames]
+                    if missing:
+                        raise ValueError("{} object is invalid - missing required column{}: {}"
+                                         .format(self.__class__.__name__, plural, missing))
'''

TEMPLATE_KEPT = '''diff --git a/astropy/timeseries/core.py b/astropy/timeseries/core.py
--- a/astropy/timeseries/core.py
+++ b/astropy/timeseries/core.py
@@ -70,6 +70,8 @@ class BaseTimeSeries(QTable):
-                raise ValueError("{} object is invalid - expected '{}' as the first column{} but found '{}'"
-                                 .format(self.__class__.__name__, required_columns[0], plural, self.colnames[0]))
+                raise ValueError("{} object is invalid - expected {} as the first column{} but found {}"
+                                 .format(self.__class__.__name__, required_columns[:i], plural, self.colnames[:i]))
'''

GRAMMAR = '''diff --git a/astropy/units/format/cds.py b/astropy/units/format/cds.py
--- a/astropy/units/format/cds.py
+++ b/astropy/units/format/cds.py
@@ -180,7 +180,13 @@ class CDS(Base):
-                              | unit_expression DIVISION combined_units
+                              | division_chain
+        def p_division_chain(p):
+            """
+            division_chain : product_of_units DIVISION product_of_units
+            """
+            p[0] = p[1] / p[3]
diff --git a/astropy/units/tests/test_format.py b/astropy/units/tests/test_format.py
--- a/astropy/units/tests/test_format.py
+++ b/astropy/units/tests/test_format.py
@@ -10,3 +10,6 @@
+def test_cds_division_order():
+    assert u.Unit("km/s/Mpc", format="cds") == u.km / u.s / u.Mpc
'''

SIBLING = '''diff --git a/sphinx/domains/python.py b/sphinx/domains/python.py
--- a/sphinx/domains/python.py
+++ b/sphinx/domains/python.py
@@ -91,7 +91,9 @@ def unparse(node: ast.AST) -> List[Node]:
-            result.pop()
+            if node.elts:
+                result.pop()
'''


def test_a_new_error_message_is_found_and_a_kept_template_is_not():
    assert dp.new_error_messages(NEW_MESSAGE, ISSUE) == ["{} object is invalid - missing required column{}: {}"]
    assert dp.new_error_messages(TEMPLATE_KEPT, ISSUE) == []


def test_wording_the_report_asks_for_is_not_new():
    issue = ISSUE + " Expected: '<Name> object is invalid - missing required column: flux'."
    assert dp.new_error_messages(NEW_MESSAGE, issue) == []


def test_the_real_astropy_13033_patch_trips_the_check():
    run = pathlib.Path("logs/swebench/tektonix-sample50-seed1-v7-s1/predictions.jsonl")
    if not run.is_file():
        return
    patch = next(json.loads(ln)["model_patch"] for ln in run.read_text().splitlines()
                 if json.loads(ln)["instance_id"] == "astropy__astropy-13033")
    assert dp.new_error_messages(patch, ISSUE)


def test_a_grammar_change_needs_a_rejection_test():
    assert dp.touches_grammar(GRAMMAR) and not dp.has_rejection_test(GRAMMAR)
    with_rejection = GRAMMAR + "+    with pytest.raises(ValueError):\n+        u.Unit('km/s.Mpc-1', format='cds')\n"
    assert dp.has_rejection_test(with_rejection)
    assert not dp.touches_grammar(NEW_MESSAGE)


def test_changed_functions_come_from_hunk_headers():
    assert dp.changed_functions(SIBLING) == [("sphinx/domains/python.py", "unparse")]
    assert dp.changed_functions(GRAMMAR) == []


def test_a_sibling_definition_is_found_and_tests_and_generic_names_are_not(tmp_path):
    (tmp_path / "sphinx" / "domains").mkdir(parents=True)
    (tmp_path / "sphinx" / "pycode").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "sphinx" / "domains" / "python.py").write_text("def unparse(node):\n    pass\n")
    (tmp_path / "sphinx" / "pycode" / "ast.py").write_text("def unparse(node):\n    pass\n\ndef run():\n    pass\n")
    (tmp_path / "tests" / "test_x.py").write_text("def unparse(node):\n    pass\n")
    got = dp.sibling_definitions(tmp_path, [("sphinx/domains/python.py", "unparse"), ("sphinx/pycode/ast.py", "run")])
    assert got == {"unparse": ["sphinx/pycode/ast.py"]}


def test_nudge_fires_each_pattern_once_in_order(tmp_path):
    subprocess.run(["true"])
    name, text = dp.nudge(NEW_MESSAGE, ISSUE, tmp_path, set())
    assert name == "new_error_text" and "missing required column" in text
    assert dp.nudge(NEW_MESSAGE, ISSUE, tmp_path, {"new_error_text"}) is None
    name, text = dp.nudge(GRAMMAR, ISSUE, tmp_path, set())
    assert name == "grammar_rejection" and "STILL be rejected" in text
    assert dp.nudge(GRAMMAR, ISSUE, tmp_path, {"grammar_rejection"}) is None


def test_a_nested_function_is_named_from_the_file_not_the_hunk_header(tmp_path):
    src = tmp_path / "sphinx" / "domains"
    src.mkdir(parents=True)
    (src / "python.py").write_text("\n".join([
        "import ast", "", "",
        "def _parse_annotation(annotation):",
        "    def unparse(node):",
        "        if isinstance(node, ast.Tuple):",
        "            result = []",
        "            for elem in node.elts:",
        "                result.extend(unparse(elem))",
        "            result.pop()",
        "            return result",
        "    return unparse(ast.parse(annotation))",
    ]) + "\n")
    diff = (
        "diff --git a/sphinx/domains/python.py b/sphinx/domains/python.py\n"
        "--- a/sphinx/domains/python.py\n+++ b/sphinx/domains/python.py\n"
        "@@ -7,7 +7,8 @@ def _parse_annotation(annotation):\n"
        "             result = []\n             for elem in node.elts:\n                 result.extend(unparse(elem))\n"
        "-            result.pop()\n+            if node.elts:\n+                result.pop()\n             return result\n"
    )
    assert dp.changed_functions(diff, tmp_path) == [("sphinx/domains/python.py", "unparse"),
                                                   ("sphinx/domains/python.py", "_parse_annotation")]
    assert dp.changed_functions(diff) == [("sphinx/domains/python.py", "_parse_annotation")]
