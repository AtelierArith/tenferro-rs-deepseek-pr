# AI Workflow Assets

This directory contains tenferro-rs-specific agent workflows and automation
prompts. It is not a vendored copy of shared rules from another repository.

Shared tensor4all agent rules live in
`tensor4all/tensor4all-agent-rules` and are read online on demand, with the
optional sibling checkout fallback documented in `AGENTS.md`.

## Contents

- `contribution-workflows/`: reusable repository-local workflows for issue
  intake and bug-fix pull requests.
- `prompts/repository-rules-review.md`: system prompt for the delta-scoped
  `REPOSITORY_RULES.md` review used in CI and local pre-PR checks.
- `repo-settings.json`: the expected GitHub repository settings and required
  branch protection checks for this repository.
- `run-codex-solve-bug.sh`, `run-claude-solve-bug.sh`, and
  `solve_bug_issue.md`: headless bug-fix automation entry points.

## Repository rules review

Same-repo pull requests run the `review bot` workflow
(`.github/workflows/review_bot.yml`). It reviews **only the PR diff** against
selected sections of `REPOSITORY_RULES.md` and blocks merge when the model
reports `severity=block` findings. `severity=warn` findings are reported but do
not fail CI. Fork pull requests skip the LLM step and pass the gate job, matching
the same-repo-only pattern used by `CI_gpu.yml`.

Maintainers can waive the LLM review with the `rules-review:waive` label.

### Local check

Install local helpers once:

```bash
python3 -m pip install -r scripts/requirements-dev.txt
```

When a repository-root `.env` file defines `DEEPSEEK_API_KEY`, the review script
loads it automatically via `python-dotenv`. Include uncommitted changes with
`--worktree`:

```bash
python3 scripts/repository-rules-review.py \
  --worktree \
  --base origin/main \
  --head HEAD
```

Use `--dry-run` to exercise diff selection and deterministic checks without
calling the API. CI uses the committed merge base and PR head SHA instead of
`--worktree`.

Policy remains in `REPOSITORY_RULES.md`. This section documents workflow
mechanics only.

## Rules

- Do not add vendored shared-rule bundles under `ai/`.
- Do not add agent asset lockfiles or sync manifests for external templates.
- Keep durable tenferro-specific policy in `REPOSITORY_RULES.md`.
- Keep contribution policy in `CONTRIBUTING.md`; keep workflow mechanics here.
