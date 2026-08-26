"""Condensing must keep the verdict and its evidence, and drop everything else."""

from __future__ import annotations

from orkastrator.runners import MAX_OUTPUT, condense

PYTEST_PASS = """\
============================= test session starts ==============================
platform linux -- Python 3.13.1, pytest-8.3.4, pluggy-1.5.0
rootdir: /home/kas/dev/kashh
plugins: xdist-3.6.1, subtests-0.14.1, anyio-4.8.0
8 workers [2881 items]
........................................................................ [  2%]
........................................................................ [  5%]
s.......................................................s............... [  7%]
=============================== warnings summary ===============================
services/control-plane/jobs_control_plane/search.py:41
    class SearchInterface(BaseModel):

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
========== 2855 passed, 26 skipped, 8 warnings, 81 subtests passed in 95.72s ===========
"""

PYTEST_FAIL = """\
============================= test session starts ==============================
platform linux -- Python 3.13.1, pytest-8.3.4
collected 3 items

tests/test_overview_redraft.py .F.                                       [100%]

=================================== FAILURES ===================================
_________________ test_unanswered_email_has_no_missing_draft_path ______________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f2a>

    def test_unanswered_email_has_no_missing_draft_path(monkeypatch):
        schema = [{"field_id": "email"}]
>       assert actions.draft_missing_blocker(item) == "nothing left for the drafter"
E       AssertionError: assert None == 'nothing left for the drafter'

tests/test_overview_redraft.py:462: AssertionError
=========================== short test summary info ============================
FAILED tests/test_overview_redraft.py::test_unanswered_email_has_no_missing_draft_path
========================= 1 failed, 2 passed in 0.41s ==========================
"""


def test_a_passing_pytest_run_condenses_to_its_counts():
    """The dots and the warnings block say nothing the summary line does not."""

    result = condense(PYTEST_PASS, returncode=0, satisfied=True)

    assert result == "2855 passed, 26 skipped, 8 warnings, 81 subtests passed in 95.72s"
    # The specific things that cost the most and carry the least.
    assert "[  2%]" not in result
    assert "warnings summary" not in result
    assert "plugins:" not in result
    assert len(result) < len(PYTEST_PASS) / 8


def test_a_failing_pytest_run_keeps_the_assertion_that_failed():
    """A fixer cannot act on a count alone, so the failure survives in full."""

    result = condense(PYTEST_FAIL, returncode=1, satisfied=False)

    assert "1 failed, 2 passed in 0.41s" in result
    assert "test_unanswered_email_has_no_missing_draft_path" in result
    assert "AssertionError: assert None == 'nothing left for the drafter'" in result
    # Still not the session preamble.
    assert "platform linux" not in result
    assert "collected 3 items" not in result


def test_an_absence_check_that_passes_by_failing_is_condensed_as_a_pass():
    """`satisfied` is the supervisor's verdict, not a re-derivation of the exit code.

    `rg PATTERN path` exits 1 once the symbol is gone, which is exactly what a
    finding about a removed symbol requires. A condenser that read `returncode`
    would keep the failure half of the output for a check that passed.
    """

    passing = condense(PYTEST_PASS, returncode=1, satisfied=True)

    assert passing == "2855 passed, 26 skipped, 8 warnings, 81 subtests passed in 95.72s"


def test_more_failures_than_fit_are_counted_rather_than_silently_dropped():
    blocks = "\n".join(
        f"_______________ test_case_{index} _______________\n"
        f">       assert {index} == 0\n"
        f"E       AssertionError: assert {index} == 0\n"
        for index in range(30)
    )
    output = f"=== FAILURES ===\n{blocks}\n===== 30 failed in 2.10s =====\n"

    result = condense(output, returncode=1, satisfied=False)

    assert "30 failed in 2.10s" in result
    assert "showing 12 of 30 failures" in result
    assert "test_case_0" in result
    assert "test_case_29" not in result


def test_typescript_errors_survive_and_the_rest_does_not():
    output = """\
> tsc -b

src/PackageScreen.tsx(1171,7): error TS2367: This comparison appears unintentional.
src/overview.ts(515,3): error TS2739: Type is missing properties.
Found 2 errors in 2 files.
"""

    result = condense(output, returncode=2, satisfied=False)

    assert "Found 2 errors in 2 files." in result
    assert "error TS2367" in result
    assert "error TS2739" in result
    assert "> tsc -b" not in result


def test_a_node_test_runner_pass_condenses_to_its_tap_counts():
    output = """\
TAP version 13
# Subtest: the Email row's send-back control follows the served disposition
ok 1 - the Email row's send-back control follows the served disposition
  ---
  duration_ms: 412.9
  ...
1..1
# tests 1
# suites 0
# pass 1
# fail 0
# cancelled 0
# skipped 0
"""

    result = condense(output, returncode=0, satisfied=True)

    assert "# pass 1" in result
    assert "# fail 0" in result
    assert "duration_ms" not in result
    assert "TAP version 13" not in result


def test_a_node_test_runner_failure_keeps_the_failing_assertion_line():
    output = """\
TAP version 13
not ok 1 - the Email row's send-back control follows the served disposition
1..1
# tests 1
# pass 0
# fail 1
"""

    result = condense(output, returncode=1, satisfied=False)

    assert "# fail 1" in result
    assert "not ok 1 - the Email row's send-back control" in result


def test_an_unrecognised_runner_keeps_both_ends_not_only_the_tail():
    """A tool that prints its fatal error first must not lose it to a tail.

    This is the whole reason the fallback is not `output[-8000:]`: the previous
    behaviour dropped the one line that said what went wrong whenever the tool
    kept talking afterwards.
    """

    output = "could not execute 'npx': No such file or directory\n" + ("filler line\n" * 4_000)

    result = condense(output, returncode=127, satisfied=False)

    assert "could not execute 'npx'" in result
    assert "characters omitted" in result
    assert result.endswith("filler line\n") or result.endswith("filler line")
    assert len(result) <= MAX_OUTPUT


def test_short_unrecognised_output_is_returned_whole():
    output = "error: no such option: --locked\n"

    assert condense(output, returncode=2, satisfied=False) == output


def test_empty_output_says_so_only_when_the_check_failed():
    assert condense("", returncode=0, satisfied=True) == ""
    assert condense("   \n", returncode=3, satisfied=False) == "no output; exit status 3"


def test_a_parser_that_raises_falls_back_instead_of_failing_the_validation(monkeypatch):
    """A bug in here must never be the thing that kills a monitor tick."""

    from orkastrator import runners

    def explode(*_args, **_kwargs):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(runners, "_condense", explode)

    assert runners.condense(PYTEST_PASS, returncode=0, satisfied=True) == PYTEST_PASS


def test_condensed_output_never_exceeds_the_contract_field():
    """`ValidationResult.output` caps at 8000, so nothing here may return more."""

    blocks = "\n".join(
        f"_______________ test_case_{index} _______________\n" + ("x" * 900) + "\n"
        for index in range(40)
    )
    output = f"=== FAILURES ===\n{blocks}\n===== 40 failed in 9.10s =====\n"

    assert len(condense(output, returncode=1, satisfied=False)) <= MAX_OUTPUT


# Captured verbatim from `uv run --locked python -m pytest -q -n 8` in kashh on
# 2026-08-22, colour codes and all. The first version of this module was written
# against a hand-typed imitation of pytest output and condensed the real thing by
# 0.3%: the imitation had no ANSI, and `-q` prints its summary with none of the
# `=` rules the pattern was keyed on. Keep this fixture byte-exact.
KASHH_QUIET_XDIST_PASS = "bringing up nodes...\nbringing up nodes...\n\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32mu\x1b[0m\x1b[32mu\x1b[0m\x1b[32mu\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\x1b[32m.\x1b[0m\n.venv/lib/python3.12/site-packages/graphiti_core/driver/search_interface/search_interface.py:22\n  /home/kas/dev/kashh/.venv/lib/python3.12/site-packages/graphiti_core/driver/search_interface/search_interface.py:22: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.13/migration/\n    class SearchInterface(BaseModel):\n\n-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n\x1b[33m\x1b[32m2859 passed\x1b[0m, \x1b[33m\x1b[1m26 skipped\x1b[0m, \x1b[33m\x1b[1m8 warnings\x1b[0m, \x1b[32m81 subtests passed\x1b[0m\x1b[33m in 93.89s (0:01:33)\x1b[0m\x1b[0m\n"  # noqa: E501


def test_the_real_captured_kashh_run_condenses_to_one_line():
    result = condense(KASHH_QUIET_XDIST_PASS, returncode=0, satisfied=True)

    assert result == "2859 passed, 26 skipped, 8 warnings, 81 subtests passed in 93.89s (0:01:33)"
    assert "\x1b[" not in result
    assert "warnings summary" not in result


def test_colour_codes_do_not_defeat_the_parser():
    """Runners colour whenever they think a terminal is watching, and under `-n`
    pytest decides that from the parent rather than from the pipe it is writing
    to. A pattern that only matches uncoloured output matches nothing in practice.
    """

    coloured = "\x1b[33m\x1b[31m3 failed\x1b[0m, \x1b[32m9 passed\x1b[0m\x1b[33m in 1.20s\x1b[0m\n"

    condensed = condense(coloured, returncode=1, satisfied=False)

    assert condensed.startswith("3 failed, 9 passed in 1.20s")


RUFF_FAILURE = """src/orkastrator/execution.py:12:8: F401 [*] `os` imported but unused
   |
10 | import asyncio
11 | import json
12 | import os
   |        ^^ F401
   |
   = help: Remove unused import: `os`

src/orkastrator/store.py:88:5: SIM105 Use `contextlib.suppress(OrcaError)` here
   |
86 |     try:
87 |         close()
88 |     except OrcaError:
   |     ^^^^^^^^^^^^^^^^ SIM105
   |
   = help: Replace with `contextlib.suppress(OrcaError)`

src/orkastrator/git.py:4:8: F401 [*] `shlex` imported but unused
   |
   = help: Remove unused import: `shlex`

Found 3 errors.
[*] 2 fixable with the `--fix` option.
"""


def test_a_failing_ruff_run_keeps_the_diagnostics_and_drops_the_code_frames() -> None:
    condensed = condense(RUFF_FAILURE, returncode=1, satisfied=False)

    assert "Found 3 errors." in condensed
    assert "F401=2  SIM105=1" in condensed
    assert "src/orkastrator/execution.py:12:8: F401 [*] `os` imported but unused" in condensed
    assert "src/orkastrator/store.py:88:5: SIM105" in condensed
    # The frames, carets and help lines are the whole reason this parser exists.
    assert "^^^^" not in condensed
    assert "= help:" not in condensed
    assert "import asyncio" not in condensed


def test_a_clean_ruff_run_is_its_own_summary_line() -> None:
    assert condense("All checks passed!\n", returncode=0, satisfied=True) == "All checks passed!"


def test_a_flood_of_one_rule_is_reported_as_a_count_before_it_is_reported_at_all() -> None:
    output = "\n".join(
        f"src/module_{index}.py:{index}:1: E501 line too long ({100 + index} > 100)"
        for index in range(200)
    )
    condensed = condense(f"{output}\nFound 200 errors.\n", returncode=1, satisfied=False)

    assert "E501=200" in condensed
    assert "(showing 12 of 200)" in condensed
    assert condensed.count("E501 line too long") == 12


def test_mypy_diagnostics_are_grouped_by_their_bracketed_code() -> None:
    output = """src/orkastrator/runners.py:111: error: Argument 1 has incompatible type  [arg-type]
src/orkastrator/runners.py:111: note: this is context for the error above
src/orkastrator/execution.py:164: error: Missing return statement  [return]
src/orkastrator/execution.py:316: error: Missing return statement  [return]
Found 3 errors in 2 files (checked 12 source files)
"""
    condensed = condense(output, returncode=1, satisfied=False)

    assert "Found 3 errors in 2 files (checked 12 source files)" in condensed
    assert "return=2  arg-type=1" in condensed
    # A note is a continuation of the error above it, not a fourth diagnostic.
    assert "note: this is context" not in condensed


def test_a_clean_mypy_run_is_its_own_summary_line() -> None:
    condensed = condense(
        "Success: no issues found in 12 source files\n", returncode=0, satisfied=True
    )

    assert condensed == "Success: no issues found in 12 source files"


def test_pyright_diagnostics_are_grouped_by_their_rule_name() -> None:
    output = """
/home/kas/dev/app/main.py:10:5 - error: "handle" undefined (reportUndefinedVariable)
/home/kas/dev/app/main.py:22:9 - error: "spawn" is not defined (reportUndefinedVariable)
/home/kas/dev/app/util.py:4:1 - warning: Import "os" is not accessed (reportUnusedImport)
2 errors, 1 warning, 0 informations
"""
    condensed = condense(output, returncode=1, satisfied=False)

    assert "2 errors, 1 warning, 0 informations" in condensed
    assert "reportUndefinedVariable=2  reportUnusedImport=1" in condensed


def test_a_pytest_failure_is_not_mistaken_for_a_lint_diagnostic() -> None:
    output = """FAILED tests/test_store.py::test_stage_started - AssertionError: 3 != 4
1 failed, 2850 passed in 95.72s
"""
    condensed = condense(output, returncode=1, satisfied=False)

    assert "1 failed, 2850 passed in 95.72s" in condensed
    # The lint parser never ran, so there is no rule histogram to show.
    assert "=1" not in condensed


# Captured verbatim from `ruff check`, escapes and all. Ruff's default renderer
# leads with the rule code and wraps it in an OSC 8 hyperlink, neither of which
# a hand-written sample would have got right.
RUFF_DEFAULT_RENDERER = (
    "\x1b[1m\x1b[91m\x1b]8;;https://docs.astral.sh/ruff/rules/unused-import\x1b\\F401"
    "\x1b]8;;\x1b\\\x1b[0m [\x1b[1m\x1b[96m*\x1b[0m]\x1b[1m `os` imported but unused\x1b[0m\n"
    " \x1b[1m\x1b[94m--> \x1b[0m/tmp/bad.py:1:8\n"
    "  \x1b[1m\x1b[94m|\x1b[0m\n"
    "\x1b[1m\x1b[94m1\x1b[0m \x1b[1m\x1b[94m|\x1b[0m import os\n"
    "  \x1b[1m\x1b[94m|\x1b[0m        \x1b[1m\x1b[91m^^\x1b[0m\n"
    "  \x1b[1m\x1b[94m|\x1b[0m\n"
    "\x1b[1m\x1b[92mhelp\x1b[0m: Remove unused import: `os`\n"
    "\nFound 1 error.\n"
)


def test_ruffs_own_renderer_is_read_through_its_hyperlinks() -> None:
    condensed = condense(RUFF_DEFAULT_RENDERER, returncode=1, satisfied=False)

    # Reported in the concise shape even though ruff rendered the other one, so
    # a reader sees one format regardless of how the command was invoked.
    assert "/tmp/bad.py:1:8: F401 `os` imported but unused" in condensed
    assert "F401=1" in condensed
    assert "Found 1 error." in condensed
    assert "\x1b" not in condensed
    assert "^^" not in condensed
    assert "help:" not in condensed
