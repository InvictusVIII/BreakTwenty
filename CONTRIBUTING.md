# Contributing to BreakTwenty

Contributions to BreakTwenty are welcome, including bug fixes, improvements, documentation changes, and feature suggestions.

Please keep contributions focused and free of credentials, private financial information, security-sensitive data, or other personal information.

BreakTwenty does not automatically upload financial data, authentication material, or diagnostics to the developer. Anything you place in an issue, pull request, discussion, email, or attachment is information you intentionally submit through that channel, so review and sanitize it first.

## Contribution Terms

BreakTwenty is source-available, not open source.

All contributions are **voluntary and unpaid**.

By intentionally submitting a pull request or other contribution for inclusion in BreakTwenty, you agree to the contribution terms in Section 9 of [LICENSE.md](LICENSE.md#9-contributions-to-the-official-breaktwenty-project).

In practical terms, this means:

* you must have the right to submit the contribution;
* an accepted contribution may become part of BreakTwenty;
* BreakTwenty may use, modify, distribute, sublicense, relicense, and commercially use the contribution;
* those rights are perpetual and irrevocable;
* submitting a contribution does not entitle you to payment, royalties, revenue sharing, ownership in BreakTwenty, or other compensation; and
* BreakTwenty is not required to accept, merge, publish, or continue using any contribution.

You retain copyright in your original contribution unless a separate written copyright assignment is agreed to.

If you are not willing to grant these rights, please do not submit the contribution.

## Pull Requests

* Create a focused branch from `main`.
* Keep credentials, databases, logs, provider captures, private screenshots, runtime artifacts, and generated packages out of commits.
* Keep changes focused on the issue or feature being addressed.
* Preserve dependency locks and required third-party notices when changing dependencies.
* Run `python3 scripts/run_release_health.py` before requesting final review.
* Describe what changed and how you tested it.
* Normal Git authorship will be preserved for accepted contributions.

Pull-request jobs do not receive signing, updater, bank, application-key, or release credentials. Contributions must not depend on secrets being available in CI.

## Security Issues

Do not report vulnerabilities or exploitation details through a public issue, discussion, or pull request.

Use the private reporting process described in [SECURITY.md](.github/SECURITY.md#security-testing-and-disclosure).
