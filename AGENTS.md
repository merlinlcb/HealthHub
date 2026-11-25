# Rules for automated contributors

This file is for bots and AI coding agents working on HealthHub. Read [CONTRIBUTING.md](CONTRIBUTING.md) first.
HealthHub holds people's health data and is reachable from the internet, so the rules below are strict.

## What you may and may not do

- **Do** work on a branch named `bot/<short-description>` and open a pull request against `main`.
- **Do** label the pull request `automated` and fill in every section of the pull request template.
- **Never** push to `main`, merge, tag, or deploy (`deploy.sh`, `publish-apk.sh`, ssh to any host).
- **Never** edit `.github/workflows/`, `LICENSE`, `deploy.sh`, `publish-apk.sh` or `healthhub.service`.
- **Never** add a dependency outside the Python standard library.
- **Never** add outbound network connections.
- **Never** put secrets, IP addresses, host names, real people's names or real health data in code, tests,
  commits or pull request text.

## Ask a human first

Comment on the issue and stop before changing any of these:

- the database schema (`SCHEMA` or the `ALTER TABLE` upgrades in `Hub.__init__`)
- the sync protocol (`Hub.sync`, entry kinds, the request or response shape)
- authentication, tokens, invites, throttling or password handling
- anything that changes what members can see of each other (`friends`, `compare`, chat, notes, dashboard)
- the goal maths (they must match the app)

## Scope

- One issue per pull request, and only what it asks for. No refactoring or reformatting on the side.
- If it needs more than about 300 changed lines (not counting tests), explain the plan in the issue instead.

## Before opening the pull request

```bash
python3 -W ignore -m unittest -v test_healthhub.py   # must pass
```

- Add a test for every behaviour you change, and a failing-then-passing test for a bug fix.
- Re-read the diff. Remove debugging output and anything you can't explain.

## In the pull request description

- What changed and why, with the issue it closes.
- The test results and the tests you added.
- Whether it touches the database, sync, or what members can see (it should say "no" unless a human agreed).
- What you could not verify.

## Review

An automated reviewer checks your pull request first. Push fixes to the same branch and answer each point. If it
can't verify something, a human maintainer decides.
