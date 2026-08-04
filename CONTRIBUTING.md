# Contributing & project setup

## Local development

```bash
git clone https://github.com/RithikSatarla/agentcage.git
cd agentcage
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest tests/                          # must pass before any commit
ruff check part_a part_b tests
black --check part_a part_b tests
```

## Publishing the repository to GitHub

The repository is initialised locally with an `initial` commit. To publish:

```bash
# 1. create an empty repo named "agentcage" at https://github.com/new
#    (no README, no .gitignore, no license — this repo already has them)

# 2. point the local repo at it and push
git remote add origin https://github.com/RithikSatarla/agentcage.git
git branch -M main
git push -u origin main
```

Then, in the repository settings:

- **Actions** — enabled by default; the `tests` workflow runs on the first push.
- **Actions → General → Workflow permissions** — read-only is sufficient.
- **Pages** — leave disabled. Vercel serves the site.

Update the `[project.urls]` entries in `pyproject.toml` and the badge URL in `README.md` if
you push under an organisation other than `RithikSatarla`.

## Deliberate deviations from the original build spec

Two places where following the spec literally would have shipped something broken. Both
are intentional; change them back only with a reason.

**1. CI does not run the full Part A study on every push.** The spec's workflow ends with
`python -m part_a.run` in the test matrix. That is thousands of network reads per job,
times four Python versions, on every push — slow, and red whenever GitHub rate-limits or
a sampled repository is briefly unavailable. Instead:

- every push runs `python -m part_a.run --limit 3`, which exercises the real pipeline
  (network, parsing, detection, aggregation) against the live API and asserts the run
  produced a real analysis;
- the complete study runs weekly on a schedule, and on demand via `workflow_dispatch`.

The study still runs in CI. It just doesn't gate every commit on the GitHub API being
healthy.

**2. `vercel.json` does not run `npm run build`.** The spec sets `buildCommand` to
`npm run build` and `outputDirectory` to `out`. There is no `package.json` in this
repository and nothing to build — that configuration fails the deployment. The site is
static HTML, so the config serves `website/` directly with no build step.

## Vercel

`vercel.json` is committed and configured, but there is no `website/` directory yet, so a
deployment will 404 until one exists. That is expected.

1. <https://vercel.com/new> → **Import Git Repository** → select `agentcage`.
2. Framework preset: **Other**. Leave build and install commands empty — `vercel.json`
   already sets `outputDirectory` to `website/out` and disables the build.
3. Deploy. The project is linked and every push to `main` will redeploy.

When the site is added, drop it in `website/` and set `buildCommand` in `vercel.json`.

## Working on Part A

`part_a/run.py` is the measurement. Two rules:

1. **Never hand-edit `part_a/results.json`.** It is generated. If a number needs to change,
   change the code and re-run.
2. **Any change to a detector goes in the revision table in PROTOCOL.md §2.7**, with its
   effect on the headline — including when the effect is to make the finding weaker. Two of
   the seven existing revisions removed false positives that would have inflated the result.

Re-running is cheap: responses cache under `.cache/`, so only newly-requested files hit the
network.

```bash
python -m part_a.run --limit 3        # quick pipeline check
python -m part_a.run                  # full study
python -m part_a.run --no-cache       # ignore the cache
```

Adding a repository to the frame means adding an entry to `part_a/repos.json` — it must be
Python-primary and ship an agent tool surface (see the criteria recorded in that file).
Whether it enters the analysed set is decided by the code, not by you.

## Working on Part B

The interceptor must stay **passive**: it may observe and record, never rewrite a request,
inject a response, or short-circuit the transport. Tests assert this directly
(`test_httpx_capture_does_not_modify_the_response_seen_by_the_caller`) — keep them passing.

New API models belong in `part_b/`, next to `stripe_mock.py`, and need:

- real entity definitions with the fields the API actually returns;
- writes that mutate state, and reads that observe the mutation;
- errors with the real status code, error code and message;
- a documented fidelity boundary — say what you did *not* model, as
  [PROTOCOL.md §3.3](PROTOCOL.md) does for Stripe;
- tests that assert on state transitions, not on call counts.

Tests run without network. Use `httpx.MockTransport` or a local `requests` adapter, as
`tests/test_interceptor.py` does.

## Pull requests

- One concern per PR.
- `pytest tests/` and `ruff check` pass.
- If you touched Part A methodology, PROTOCOL.md is updated in the same PR.
- If a change makes a headline number weaker, say so in the PR description. That is a
  feature.
