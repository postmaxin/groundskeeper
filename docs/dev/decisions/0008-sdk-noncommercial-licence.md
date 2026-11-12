# 0008. Use Yarbo SDK 0.3.0 under PolyForm Noncommercial

- **Status:** Accepted
- **Date:** 2026-11-11
- **Links:** #31, #114, `THIRD_PARTY_NOTICES.md`

## Context
The panel signs in to the Yarbo cloud and gets its broker credentials through Yarbo's Python SDK (`yarbo-data-sdk`).
Up to 0.2.1 the SDK was MIT. From 0.3.0 Yarbo publishes it under PolyForm Noncommercial 1.0.0; the code is the same
(the two wheels' packages compare identical). The panel had stayed on 0.2.1 to keep everything it ships MIT, but
that left it pinned to an old release of the one library it cannot replace.

## Decision
Follow the SDK's current releases and accept the licence. The panel's own code stays MIT. The Docker image installs
the SDK, so the image as a whole is for noncommercial use, and `THIRD_PARTY_NOTICES.md` and the README say so.

## Consequences
- Anyone who wants to use Groundskeeper commercially must replace or relicense the SDK part themselves.
- Dependabot follows the SDK again, so a future licence or behaviour change shows up in a PR.
- Revisit if Yarbo changes the licence again, or if the cloud sign-in can be done without the SDK.

## Enforced by
`requirements.txt` and `pyproject.toml` (the pin, with the licence noted), `THIRD_PARTY_NOTICES.md`.
