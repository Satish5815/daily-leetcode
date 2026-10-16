# Daily LeetCode AI Agent (Python)

A starter project for a daily GitHub Actions workflow that selects 2–3 distinct LeetCode problems, asks an AI model for Python and JavaScript solutions, tests them, and submits only when the test command succeeds.

## Important limitations

- LeetCode submission is not an official public API. This starter uses the unofficial `leetcode_cli` tool; its commands/output can change.
- The first version should be validated with `DRY_RUN=true`. Example tests are not a proof of correctness.
- Do not use this on an account or in a setting where automated submissions violate applicable rules.
- Never commit cookies, API keys, or generated `.leetcode` config files.

## Setup

1. Create a **private** GitHub repository and upload these files.
2. In **Settings → Secrets and variables → Actions**, add:
   - `OPENAI_API_KEY`: your API key.
   - `LEETCODE_COOKIE`: the full LeetCode cookie string required by the CLI (sensitive session credential).
   - `LEETCODE_USERNAME`: your LeetCode username.
3. Keep `DRY_RUN` enabled for the first several runs. The workflow defaults to dry-run.
4. Run the workflow manually from **Actions → Daily LeetCode Agent → Run workflow**.
5. Review artifacts and logs before enabling submissions.

## Security

- Use a private repository.
- Store credentials only as GitHub Actions secrets.
- Never echo secrets, print request headers, or upload CLI config/cookie files as artifacts.
- Rotate the LeetCode session cookie if it is exposed.
- The workflow grants only `contents: write` because it commits the problem history. Remove that permission if you prefer artifact-only history.
- Do not give this workflow write access to unrelated repositories.

## How it works

1. Fetch public problem metadata from LeetCode's GraphQL endpoint.
2. Select problems not present in `data/history.json` (default: Easy + Medium + Hard).
3. Ask OpenAI for complete Python and JavaScript solutions.
4. Save both generated files and run the CLI's example tests for each language.
5. Submit only if the test command exits successfully and `DRY_RUN` is false.
6. Save generated solutions and a daily report as a workflow artifact; update history.

## Configure

Edit environment variables in `.github/workflows/daily.yml`:
- `PROBLEMS_PER_DAY: "3"` (allowed values: 2 or 3)
- `DIFFICULTIES: "EASY,MEDIUM,HARD"`
- Scheduled runs always use `DRY_RUN=true`. Manual runs use the `dry_run` workflow input;
  select `false` only after validating the workflow.
- The workflow and Python agent invoke the installed LeetCode CLI as a Python module, so
  they do not depend on shell `PATH` propagation or a generated console script.

## Current starter status

This is a starter scaffold, not a guarantee that LeetCode's unofficial CLI will remain compatible. The workflow deliberately fails closed: if problem selection, generation, or tests fail, it does not submit that problem.
