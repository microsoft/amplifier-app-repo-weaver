---
bundle:
  name: repo-weaver
  version: 0.1.0
  description: >
    Turn git repositories into a queryable knowledge corpus via wiki-weaver.
    Exposes three repo-weaver commands as mountable tools: repo_weaver_init,
    repo_weaver_weave, repo_weaver_ask. Each wraps the repo-weaver lib that
    materialises git history (commits + PRs) into source documents and
    orchestrates wiki-weaver ingest (with automatic retry on transient
    failures). Compose this bundle onto any bundle to add git-to-corpus
    automation — no separate CLI install needed.

# Thin root: only includes. The real payload (the tool-module) lives in the
# behavior, which the root composes here so it is reachable.
includes:
  - bundle: repo-weaver:behaviors/repo-weaver
---

# repo-weaver
