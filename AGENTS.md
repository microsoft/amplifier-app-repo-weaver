# repo-weaver — Working Conventions

**About this repo:** repo-weaver is a deterministic git → wiki front-end over the
`wiki-weaver` engine. It makes zero direct LLM calls; all synthesis/query is delegated
to wiki-weaver via subprocess. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full picture.

> This file is auto-loaded each session. Treat it as binding repo convention.

## MANDATORY: Keep architecture docs in sync

Whenever a change touches any of:

- the **architecture** (layers, modules, data flow),
- the **command surface** (`init` / `weave` / `ask` / `replay` / `doctor`),
- the **wiki-weaver boundary / subprocess contract**,
- the **materializer output shape** (digest / module-snapshot filenames or sections),
- the **schema** (`policy/schema.md`), or
- the **leverage-level status** (L1–L4),

you MUST, in the **same change**:

1. Update [`ARCHITECTURE.md`](ARCHITECTURE.md), and
2. Regenerate the diagrams:
   - `docs/architecture.dot` / `docs/architecture.png`
   - `docs/leverage-levels.dot` / `docs/leverage-levels.png`

**A change that alters architecture without updating these is incomplete.**

## PR checklist

- [ ] `ARCHITECTURE.md` updated if architecture / commands / boundary / schema changed
- [ ] Diagrams regenerated if structure changed
- [ ] Tests + `python_check` pass
- [ ] No direct LLM calls added to repo-weaver (all synthesis stays delegated to wiki-weaver)

## Re-read cadence

Consult these conventions at the **start of work** and at **each phase transition** —
design, coding, debugging, PR. The architecture-sync rule is easiest to honor when you
check it before you start, not after you've "finished."
