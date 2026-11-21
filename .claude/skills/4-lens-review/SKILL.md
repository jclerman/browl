---
name: 4-lens-review
description: Review a PR or branch through four lenses (correctness, efficiency & clarity, security, test coverage including the issue's acceptance criteria), one sub-agent per lens, then give one combined report ranked by severity, saved under docs/pr_reviews unless --no-save. Use when asked for a 4-lens review, or a multi-part review of a PR or branch.
argument-hint: "[PR number | URL | branch] [--no-save]"
---

# 4-lens review

Review a change through four lenses, each in its own sub-agent, then combine their findings into one report.

## Target

- The argument, if given: a PR number or URL, or a branch name. Otherwise the current branch's diff from `main`.
- `--no-save`, anywhere in the arguments: don't write the report to a file (see "Saving the report").
- Skip `research/` unless the user says to include it.
- For a PR, find its issue from the body (e.g. "Closes #14") or the branch name (`p-5-…` → the plan item's issue in
  `docs/DESIGN.md` §3). The test-coverage lens needs its acceptance criteria.

## Reviewers

Start four sub-agents in parallel, each with a fresh context. Give each one:

- the target (PR number or branch, base `main`) and how to see the diff (`gh pr diff N` or
  `git diff main...<branch>`), excluding `research/` unless told otherwise;
- the project's rules to judge against: `AGENTS.md`, `docs/CODING-STANDARDS.md`, and the relevant sections of
  `docs/DESIGN.md`;
- its lens, below;
- instructions to make no changes, to verify each finding against the code before reporting it, and to report each
  one with `file:line`, what's wrong, a concrete failure scenario or cost where one applies, and a suggested fix, rated
  **must fix**, **should change** or **consider**.

The lenses:

1. **Correctness:** bugs, wrong behaviour, edge cases, error handling, concurrency and async misuse (e.g. blocking the
   event loop), resource leaks, mismatches with `docs/DESIGN.md`.
2. **Efficiency & clarity:** unnecessary work or complexity, duplication, reinventing what the standard library or a
   dependency provides, naming, structure, and docs and comments that are wordy, stale or redundant (the coding
   standards ask for terse internal docs).
3. **Security:** untrusted input (request paths, headers, uploads, ontology content), injection (SPARQL, SQL, HTML),
   escaping and autoescape, information leaks (e.g. error details on public pages), access to admin routes and
   endpoints, files and subprocesses, denial of service, and dependencies.
4. **Test coverage:** whether the tests exercise the changed code's behaviour and edge cases (not just its lines), and
   whether each acceptance criterion of the issue is met and tested. List each criterion with its status (met and
   tested, met but untested, not met).

## Report

Once all four return, give one combined report:

- Findings ranked by severity: must fix, then should change, then consider. Within each rank, order by impact.
- Merge duplicate findings, and note which lenses raised each one (e.g. "correctness, security"). A finding several
  lenses raised independently is worth calling out.
- Check any finding that looks doubtful, or that reviewers disagree on, against the code before including it; drop or
  mark the ones that don't hold up.
- The acceptance-criteria table from the test-coverage lens.
- Keep it concise: one or two lines per finding, with `file:line`.

## Saving the report

Unless `--no-save` was given, also write the combined report to `docs/pr_reviews/pr-<N>-4-lens-review.md` (for a
branch without a PR, `<branch>-4-lens-review.md`), following `docs/pr_reviews/TEMPLATE-4-lens-review.md`. The
directory is gitignored, so don't stage it. Say in the reply where it is.

After fixes, update that file: mark each finding FIXED or WON'T FIX with its resolution, and fill in the Decisions
table.

## After the report

Offer to fix the should-change items (and any must-fix ones), then stop and wait for the user's answer. Don't change
anything until the user says to go ahead, and then fix only what they choose.
