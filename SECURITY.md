# Security Policy

Agent Efficiency Runtime processes local files, executes explicitly supplied commands, and creates
or mutates artifacts. Security reports involving boundary bypasses are treated as defects even when
the affected input is unusual.

## Supported versions

Security fixes are applied to `main` and, when practical, the latest released `0.1.x` version.
Earlier patch releases may not receive separate fixes. Users should reproduce an issue against the
latest release before reporting it when that can be done safely.

## Reporting a vulnerability

Do not disclose vulnerability details in a public issue, discussion, pull request, or commit.

Use GitHub's **Report a vulnerability** function for this repository when it is available. If
private vulnerability reporting is unavailable, open a minimal public issue titled
`Security contact request` without technical details, proof-of-concept code, logs, or affected file
contents. The maintainer will establish a private channel through GitHub.

Include the following information privately:

- affected AER version or commit;
- operating system and Python version;
- the affected command or capability;
- prerequisites and a minimal reproduction;
- expected and observed security boundary;
- impact assessment;
- whether the issue is already public or under active exploitation.

Remove API keys, personal data, document contents, local usernames, and unrelated paths from all
reports.

## Relevant report classes

Examples include:

- command execution outside an explicitly selected and permitted operation;
- archive traversal, unsafe symlink following, or replacement of an unintended file;
- secret-redaction bypass in persisted command logs;
- macro, embedded payload, or active-content execution;
- resource-limit bypass that creates a practical denial of service;
- stale-content or atomic-write failures that can corrupt or overwrite unrelated data;
- crafted Office, PDF, image, structured-data, or regular-expression input that crosses a
  documented safety boundary.

A missing optional dependency, unsupported file feature, inaccurate token estimate, or behavior
already documented as an explicit limitation is normally not a vulnerability unless it can be used
to cross a security boundary.

## Disclosure and remediation

The maintainer will validate the report, determine affected versions, prepare a fix and regression
test, and coordinate disclosure according to severity and exploitability. No response or fix time is
guaranteed. Please avoid public disclosure until a coordinated release or an agreed disclosure date.
