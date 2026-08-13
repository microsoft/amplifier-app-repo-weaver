"""Injectable seam for the GitHub API half of :func:`repo_weaver.materialize.materialize`.

``materialize()`` does two kinds of I/O: local git plumbing (via :mod:`repo_weaver.gitio`'s
``git *`` helpers) and GitHub API reads (merged PRs, PR discussion, issues, issue
discussion). The git-local half is untouched by this module -- it stays exactly as it
is, always going through ``gitio``.

The GitHub API half is the part that needs to vary per caller:

* the ``gh`` CLI authenticates off **ambient process environment** -- there is no
  ``env=`` passed anywhere in ``gitio._run``, so one process can hold exactly one
  GitHub identity. A caller juggling multiple credentials (e.g. a GitHub App
  installation token for personal repos, a service-account token for org repos)
  cannot make ``materialize()`` use a different identity per repo without this seam.
* ``gh`` also hides HTTP response headers, so a caller cannot see
  ``X-RateLimit-Reset`` / ``Retry-After`` to do correct rate-limit back-off, nor
  reliably tell a 401 (bad/expired credential) from a 403 (rate-limited /
  forbidden) apart -- both surface as "gh exited non-zero" with a fuzzy stderr
  message.

This module defines the seam: a small :class:`typing.Protocol` (``GitHubFetcher``)
naming exactly the four GitHub-API-reading operations ``materialize()`` performs,
plus :class:`GhCliFetcher`, the default implementation that delegates straight
through to the existing ``gitio.gh_*`` functions -- today's behaviour, byte for
byte, when no fetcher is supplied.

A caller that needs per-repo credentials and/or REST-level header visibility
supplies its own ``GitHubFetcher`` implementation (e.g. an ``httpx``-backed one
that resolves a token per repo, reads ``X-RateLimit-*`` headers, and applies its
own retry/back-off policy) via ``materialize(..., fetcher=my_fetcher)``.

Method names intentionally match the ``gitio.gh_*`` function names 1:1 (minus the
``gh_`` redundancy inside a fetcher-flavoured namespace would be tempting, but
matching names exactly keeps the mapping between "what ``gitio`` does" and "what
the Protocol requires" unambiguous, and keeps :class:`GhCliFetcher` a genuinely
thin, obviously-correct pass-through).

Raise vs. degrade -- READ THIS BEFORE IMPLEMENTING A CUSTOM FETCHER
--------------------------------------------------------------------
Every method below returns a ``(data, error)`` tuple, mirroring ``gitio``'s
contract: ``(data, None)`` on success (data may legitimately be empty), and
``(empty, error_string)`` when the fetch failed. ``materialize()`` honours that
contract today: on ``(empty, error_string)`` it still returns a **valid
document** -- an italic ``_(gh error: ... -- PR data could not be fetched for
this window.)_`` note in place of the missing section, with zero PRs/issues.

That is a **silent-degradation hazard** for any caller that just writes the
returned documents into a corpus: a rate-limited or auth-expired window is
*indistinguishable*, once written, from a window that was genuinely quiet.
Downstream, degraded content gets committed and a sync watermark advances past
the gap -- so the gap is never retried.

``materialize()`` does **not** wrap these fetcher calls in ``try/except``
(verify against ``materialize.py`` -- the only ``try`` blocks in that region
guard ``int()`` conversions of PR/issue numbers, not the fetch calls
themselves). So:

    An implementation that wants to PREVENT a degraded document from being
    produced for a transient/rate-limit/auth failure should RAISE an
    exception instead of returning ``(empty, error_string)``. The exception
    propagates straight out of ``materialize()`` to the caller, who can then
    retry the whole window later rather than committing a degraded document
    and silently advancing past the gap.

Returning ``(empty, error_string)`` is still the right choice for failures a
caller is comfortable degrading through (or wants surfaced in-document, e.g.
for a human-reviewed digest). Raising is the right choice when the caller's
pipeline advances a watermark on success and would otherwise never revisit the
window.
"""

from __future__ import annotations

from typing import Optional, Protocol

from . import gitio


class GitHubFetcher(Protocol):
    """The GitHub-API-reading operations ``materialize()`` needs.

    Exactly four methods, matching ``gitio.gh_merged_prs`` / ``gh_pr_discussion``
    / ``gh_issues`` / ``gh_issue_discussion``'s current signatures and return
    shapes. See the module docstring for the raise-vs-degrade contract that
    governs the ``error`` half of each returned tuple.
    """

    def gh_merged_prs(
        self,
        owner_repo: str,
        since: str,
        until: str,
        max_fetch: int = 120,
    ) -> tuple[list[dict[str, object]], Optional[str]]:
        """Fetch merged PRs whose mergedAt date falls in (since, until].

        See :func:`repo_weaver.gitio.gh_merged_prs` for the full contract.
        """
        ...

    def gh_pr_discussion(
        self,
        owner_repo: str,
        pr_number: int,
    ) -> tuple[dict[str, list[dict[str, object]]], Optional[str]]:
        """Fetch a single PR's comments and reviews.

        See :func:`repo_weaver.gitio.gh_pr_discussion` for the full contract.
        """
        ...

    def gh_issues(
        self,
        owner_repo: str,
        since: str,
        until: str,
        max_fetch: int = 120,
    ) -> tuple[list[dict[str, object]], Optional[str]]:
        """Fetch issues whose updatedAt date falls in (since, until].

        See :func:`repo_weaver.gitio.gh_issues` for the full contract.
        """
        ...

    def gh_issue_discussion(
        self,
        owner_repo: str,
        issue_number: int,
    ) -> tuple[list[dict[str, object]], Optional[str]]:
        """Fetch a single issue's comments.

        See :func:`repo_weaver.gitio.gh_issue_discussion` for the full contract.
        """
        ...


class GhCliFetcher:
    """Default :class:`GitHubFetcher`: delegates straight through to ``gitio.gh_*``.

    This is a thin pass-through, not a reimplementation -- it preserves today's
    behaviour byte for byte. ``materialize()`` constructs one of these whenever
    no ``fetcher`` is supplied, so every existing caller is unaffected by the
    seam's existence.

    Calls ``gitio.gh_*`` via module-level lookup (not a bound reference captured
    at class-definition time) so tests that monkeypatch/patch
    ``repo_weaver.gitio.gh_*`` (or the ``repo_weaver.materialize.gitio.gh_*``
    alias, which is the same module object) continue to work unchanged.
    """

    def gh_merged_prs(
        self,
        owner_repo: str,
        since: str,
        until: str,
        max_fetch: int = 120,
    ) -> tuple[list[dict[str, object]], Optional[str]]:
        return gitio.gh_merged_prs(owner_repo, since, until, max_fetch=max_fetch)

    def gh_pr_discussion(
        self,
        owner_repo: str,
        pr_number: int,
    ) -> tuple[dict[str, list[dict[str, object]]], Optional[str]]:
        return gitio.gh_pr_discussion(owner_repo, pr_number)

    def gh_issues(
        self,
        owner_repo: str,
        since: str,
        until: str,
        max_fetch: int = 120,
    ) -> tuple[list[dict[str, object]], Optional[str]]:
        return gitio.gh_issues(owner_repo, since, until, max_fetch=max_fetch)

    def gh_issue_discussion(
        self,
        owner_repo: str,
        issue_number: int,
    ) -> tuple[list[dict[str, object]], Optional[str]]:
        return gitio.gh_issue_discussion(owner_repo, issue_number)
