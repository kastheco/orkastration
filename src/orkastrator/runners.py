"""Condense test-runner output down to the part a decision depends on.

Validation output is not read by a human. It is written into a contract, handed
to an agent, and then re-billed on every subsequent turn of that agent. A passing
pytest run costs the same in the transcript whether it says

    ..........................................s.....  [ 43%]
    (fifty more lines)
    2855 passed, 26 skipped, 8 warnings, 81 subtests passed in 95.72s

or just the last line. It says the same thing either way. The progress dots are
paid for once to produce and then again on every turn that follows.

So each supported runner gets a parser that keeps what a verdict actually rests
on - the counts, and every failure with its assertion - and drops the rest. This
is the deterministic half of the loop: whether a suite passed is an exit code and
a regex, never an inference, and nothing here asks a model anything.

Every parser is a pure function from text to text. `condense` never raises: an
unrecognised runner, a truncated stream, or a parser bug falls back to
`_head_and_tail`, which is strictly better than the blind tail it replaces
because a tool that prints its fatal error first and then dumps context still
has that error survive.
"""

from __future__ import annotations

import re

__all__ = ["condense"]

# Keep the same ceiling the contract field enforces, so a condensed result can
# never be the thing that makes a ValidationResult unserialisable.
MAX_OUTPUT = 8_000

# Room for the failures themselves once the summary and header are accounted for.
_KEEP_FAILURES = 12

# Runners colour their output whenever they think a terminal is watching, and
# pytest under `-n` decides that from the parent, not from this pipe. Every
# pattern below is written against plain text, so the colour comes off first.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# The summary line appears two ways and both have to match. Verbose pytest frames
# it in `=` rules; `-q` prints it bare. What is stable across both is that the
# line carries an outcome word and the run's duration, so key on that rather than
# on the furniture around it.
_PYTEST_SUMMARY = re.compile(
    r"^=*\s*(?P<body>(?:[^=\n]*\b(?:passed|failed|error|errors)\b[^=\n]*?\bin\s[\d.]+s[^=\n]*|"
    r"no tests ran[^=\n]*))\s*=*$",
    re.MULTILINE,
)
_PYTEST_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+\S.*$", re.MULTILINE)
_PYTEST_FAILURE_HEAD = re.compile(r"^_{3,}\s+(?P<name>.+?)\s+_{3,}$", re.MULTILINE)
_PYTEST_SECTION = re.compile(r"^=+\s+\S.*=+$", re.MULTILINE)

_TSC_ERROR = re.compile(r"^\S.*?\(\d+,\d+\):\s+error TS\d+:.*$", re.MULTILINE)
_TSC_COUNT = re.compile(r"^Found \d+ errors?.*$", re.MULTILINE)

_NODE_TAP_COUNT = re.compile(
    r"^#\s+(?:tests|suites|pass|fail|cancelled|skipped)\s+\d+$", re.MULTILINE
)
_NODE_TAP_FAIL = re.compile(r"^not ok \d+ - .*$", re.MULTILINE)

_VITEST_COUNT = re.compile(r"^\s*(?:Test Files|Tests|Duration)\s+.*$", re.MULTILINE)
# Vitest marks a failing file with U+00D7 or U+2717 rather than the word, and
# both are written as escapes so nothing in this file depends on how an editor
# renders a character that is easy to confuse with an ASCII x.
_VITEST_FAIL = re.compile("^\\s*(?:FAIL|\\u00d7|\\u2717)\\s+\\S.*$", re.MULTILINE)


def condense(output: str, *, returncode: int, satisfied: bool) -> str:
    """Return the part of `output` a reader needs to justify the verdict.

    `satisfied` is the supervisor's own verdict, already decided from the exit
    status against the requirement's `expect_exit`. It is passed in rather than
    re-derived because an absence check passes by exiting 1, and a condenser that
    guessed from `returncode` alone would call that a failure and keep the wrong
    half of the output.
    """

    try:
        return _condense(output, returncode=returncode, satisfied=satisfied)
    except Exception:
        # A parser bug must never be the thing that fails a validation, and a
        # failed validation is what blocks a lane. Fall back, never raise.
        return _head_and_tail(output)


def _condense(output: str, *, returncode: int, satisfied: bool) -> str:
    if not output.strip():
        return "" if satisfied else f"no output; exit status {returncode}"

    output = _ANSI.sub("", output)

    for parser in (_pytest, _tsc, _node_tap, _vitest):
        condensed = parser(output, satisfied=satisfied)
        if condensed is not None:
            return _cap(condensed)

    return _head_and_tail(output)


def _pytest(output: str, *, satisfied: bool) -> str | None:
    summaries = _PYTEST_SUMMARY.findall(output)
    if not summaries:
        return None
    # The last one is the short test summary; earlier matches are section rules.
    summary = summaries[-1].strip()
    if satisfied:
        return summary

    kept: list[str] = []
    for name in _PYTEST_FAILURE_HEAD.findall(output)[:_KEEP_FAILURES]:
        block = _failure_block(output, name)
        if block:
            kept.append(block)
    if not kept:
        # No traceback sections - collection errors and `-q` runs look like this.
        kept = _PYTEST_FAILED_LINE.findall(output)[:_KEEP_FAILURES]

    total = len(_PYTEST_FAILURE_HEAD.findall(output)) or len(_PYTEST_FAILED_LINE.findall(output))
    header = summary
    if total > len(kept):
        header = f"{summary}\n(showing {len(kept)} of {total} failures)"
    return "\n\n".join([header, *kept])


def _failure_block(output: str, name: str) -> str:
    """The traceback for one named pytest failure, without the runner's furniture."""

    head = re.search(rf"^_{{3,}}\s+{re.escape(name)}\s+_{{3,}}$", output, re.MULTILINE)
    if head is None:
        return ""
    start = head.end()
    following = _PYTEST_FAILURE_HEAD.search(output, start) or _PYTEST_SECTION.search(output, start)
    body = output[start : following.start() if following else len(output)]
    lines = [line for line in body.strip().splitlines() if line.strip()]
    # The assertion and its immediate context carry the finding; the rest of a
    # deep traceback is frame noise the fixer can re-derive from the test name.
    if len(lines) > 24:
        lines = [*lines[:4], f"    ... {len(lines) - 24} frames omitted ...", *lines[-20:]]
    return "\n".join([f"FAILED {name}", *lines])


def _tsc(output: str, *, satisfied: bool) -> str | None:
    errors = _TSC_ERROR.findall(output)
    count = _TSC_COUNT.findall(output)
    if not errors and not count:
        return None
    if satisfied and not errors:
        return (count[-1].strip() if count else "no type errors")
    kept = errors[:_KEEP_FAILURES]
    header = count[-1].strip() if count else f"{len(errors)} type errors"
    if len(errors) > len(kept):
        header = f"{header}\n(showing {len(kept)} of {len(errors)})"
    return "\n".join([header, *kept])


def _node_tap(output: str, *, satisfied: bool) -> str | None:
    counts = _NODE_TAP_COUNT.findall(output)
    if not counts:
        return None
    summary = "\n".join(line.strip() for line in counts)
    if satisfied:
        return summary
    failures = _NODE_TAP_FAIL.findall(output)[:_KEEP_FAILURES]
    return "\n".join([summary, *failures])


def _vitest(output: str, *, satisfied: bool) -> str | None:
    counts = _VITEST_COUNT.findall(output)
    if not counts:
        return None
    summary = "\n".join(line.strip() for line in counts)
    if satisfied:
        return summary
    failures = [line.strip() for line in _VITEST_FAIL.findall(output)][:_KEEP_FAILURES]
    return "\n".join([summary, *failures])


def _head_and_tail(output: str) -> str:
    """Both ends of an unrecognised stream, rather than only the tail.

    A tool that prints its fatal error and then dumps context loses that error to
    a tail. Keeping the head as well costs nothing and is the difference between
    a fixer reading `command not found` and reading the last page of a stack.
    """

    if len(output) <= MAX_OUTPUT:
        return output
    head = MAX_OUTPUT // 3
    tail = MAX_OUTPUT - head - 60
    dropped = len(output) - head - tail
    return f"{output[:head]}\n... {dropped} characters omitted ...\n{output[-tail:]}"


def _cap(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return f"{text[: MAX_OUTPUT - 40]}\n... condensed output truncated ..."
