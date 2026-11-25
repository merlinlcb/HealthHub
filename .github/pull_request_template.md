## What and why

<!-- What does this change, and why? Link the issue: "Closes #123". -->

## How I tested it

<!-- Tests added or changed, and anything you checked by hand (with which app version). -->

- [ ] `python3 -W ignore -m unittest -v test_healthhub.py` passes

## Checklist

- [ ] Standard library only, no new outbound connections
- [ ] It does **not** change the database schema or sync protocol, **or** it only adds (with defaults) and works with older app versions
- [ ] It does **not** change what members can see of each other, **or** that was agreed in an issue
- [ ] Every new data access checks who is asking (`authenticate` plus an owner or admin check)
- [ ] SQL uses `?` placeholders; inputs are validated; no personal data is logged
- [ ] The diff only contains this change

## Anything a reviewer should know

<!-- Things you're unsure about or couldn't test. Automated contributors: say what you could not verify. -->
