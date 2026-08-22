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
