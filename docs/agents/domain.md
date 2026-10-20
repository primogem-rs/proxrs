# Domain Docs

## Layout

This is a single-context repo:

- `GLOSSARY.md` at the repo root defines the domain vocabulary.
- `docs/adr/` holds architectural decision records, named `NNNN-short-title.md`.

## Before exploring

Read `GLOSSARY.md` and any ADRs in `docs/adr/` that touch the area you are about to work in.

If a domain document or directory is absent, proceed silently. The `domain-modeling` skill creates glossary entries and ADRs when terms or decisions are resolved.

## Use the glossary's vocabulary

Use the glossary's terms and meanings in issue titles, proposals, hypotheses, code, and test names. Respect its avoided synonyms.

When a needed concept is missing, check whether an existing term fits; otherwise note the gap for `domain-modeling`.

## Flag ADR conflicts

If a proposal contradicts an existing ADR, identify the ADR and explain why the decision should be reopened.
