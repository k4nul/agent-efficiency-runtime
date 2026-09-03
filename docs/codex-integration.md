# Codex integration

The integration is an ordinary Codex skill under `integrations/codex` in the source checkout and
source distribution; it is not a wheel runtime resource. Install AER first, then run:

```bash
./integrations/codex/install.sh --copy
# development checkout only:
./integrations/codex/install.sh --symlink --target /explicit/codex/skills
```

The default target is `${CODEX_HOME:-$HOME/.codex}/skills`. The installer creates the skills root
if needed but refuses to replace an existing `agent-efficiency-runtime` entry. It does not edit
Codex configuration or any existing `AGENTS.md`.

The skill tells Codex to discover before Office/PDF/chart/image/archive/data/log work, read only a
compact selected schema, compact verbose commands, inspect selectors, patch small changes, pass
large results as object refs, preserve exact identifiers, and validate deliverables. Copy
`AGENTS-snippet.md` into a project instruction file only when that project should use the same
policy.

Confirm the runtime before relying on it:

```bash
aer doctor
aer discover "ppt patch"
aer schema presentation.patch --compact
```

If discovery or a structured error explicitly says the operation is unsupported, a one-off local
implementation remains appropriate. Repeated deterministic work should become a tested recipe or
capability.
