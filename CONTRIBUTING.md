# Contributing to HealthHub

Thank you for helping. HealthHub is the optional server for the
[FossFitness](https://github.com/merlinlcb/FossFitness) app, and is meant to be community maintained along with it.
This guide explains where things live, how the server works and the rules that keep people's data safe. If
anything is unclear or out of date, fixing this file is a great first pull request.

The app has its own [CONTRIBUTING.md](https://github.com/merlinlcb/FossFitness/blob/main/CONTRIBUTING.md), and
many changes touch both projects. Read the "How the app works" part of it if you change sync.

- [What HealthHub promises](#what-healthhub-promises)
- [Getting set up](#getting-set-up)
- [Map of the repository](#map-of-the-repository)
- [How the server works](#how-the-server-works)
- [Rules that protect people's data](#rules-that-protect-peoples-data)
- [Security rules](#security-rules)
- [Writing code](#writing-code)
- [Pull requests and review](#pull-requests-and-review)
- [Automated contributors](#automated-contributors)
- [Releases and deployment](#releases-and-deployment)
- [License](#license)

## What HealthHub promises

1. **Simple to run.** One Python file, the standard library only, one SQLite database. Adding a dependency needs a
   very good reason and an issue first.
2. **Invite only.** Nobody can create an account without the invite code or a one-time invite.
3. **Least sharing.** Members see each other's levels, streaks, XP, chat, shared notes and the Compare view
   (weights and yes/no goal hits). Never food logs or any other raw history. Only the admin can see full history.
4. **No phoning home.** The server makes no outbound connections except the optional GitHub release check.
5. **Old apps keep working.** Phones update at different times, so every change must work with the app versions
   people still run.

## Getting set up

You need Python 3.10 or newer. Nothing else.

```bash
HT_INVITE_CODE=letmein HT_DATA=./data python3 healthhub.py     # http://127.0.0.1:8787
python3 -W ignore -m unittest -v test_healthhub.py             # tests
```

To try it with the app, point a FossFitness build at your computer's LAN address under
Settings → App settings → Cloud sync.

## Map of the repository

| Path | What it is |
|---|---|
| `healthhub.py` | The whole server (see below) |
| `test_healthhub.py` | Tests. Each one starts a real server on a free port with a temporary database |
| `static/dashboard.html` | The web dashboard at `/`: one self-contained page (HTML, CSS and JavaScript inline) |
| `static/get.html` | The `/get` page that hands out the app |
| `deploy.sh` | Installs or updates the server on a Debian/Ubuntu host over ssh |
| `publish-apk.sh` | Puts an APK on a server by hand |
| `healthhub.service` | The hardened systemd unit `deploy.sh` installs |
| `healthhub.env.example` | Example configuration |
| `.github/workflows/test.yml` | CI: runs the tests on every push and pull request |

### Inside `healthhub.py`

The file is in sections, top to bottom:

| Section | What it does |
|---|---|
| Constants | Size limits (`MAX_BODY`, `MAX_ENTRY`, `MAX_SETTINGS`…), entry kinds (`KINDS`), update channels (`CHANNELS`), id and username patterns |
| `SCHEMA` | The SQLite tables: `users`, `tokens`, `entries`, `shared_foods`, `notes`, `messages`, `invites`, `meta` |
| `class Hub` | **All server behaviour**, independent of HTTP so it's easy to test: accounts and throttling, sync, friends, invites, chat, app downloads and updates, the handoff from the old app, Compare, shared foods, notes and the dashboard |
| GitHub releases | `check_github_release` and `github_release_loop`: fetch new app builds every 15 minutes |
| Goal maths | `calorie_target`, `protein_goal`, `water_goal_ml`, `goal_hits`: **copies of the app's formulas** (`app/src/lib/calc.js` and `game.js`) used by Compare |
| HTTP layer | `make_handler`: routing (`_route`), JSON parsing, size limits, security headers, error handling |
| `make_server`, `main` | Start-up and configuration from environment variables |

## How the server works

### Accounts and tokens

`register` needs the invite code (`HT_INVITE_CODE`) or an open one-time invite. The first account is the admin.
Passwords are hashed with scrypt and a random salt. Signing in issues a random token; only its SHA-256 hash is
stored. Every API call except sign-up, sign-in, `/healthz`, `/api/app` and downloads needs
`Authorization: Bearer <token>`. Failed sign-ins, sign-ups and downloads are throttled (`_throttled`).

### Sync

`POST /api/sync` is how the app's data reaches the server and other phones. One request does everything:

- **Entries** (`push`): the app's log in small pieces, each with an `id`, a `kind` (`day`, `weight`, `fast`,
  `workout`, `water`, `calorie`), a `day`, a `data` object and an `updatedAt` time. An entry only replaces the stored
  one if its `updatedAt` is newer, so the newest edit wins. Deletions are entries with `deleted: true`, kept so
  other phones learn about them.
- **Pulling** (`since`): the server keeps a global revision counter (`meta.rev`). The response contains every entry
  of this person's newer than the `since` the phone sent, and the new `rev` to send next time.
- **Settings** (`profile`): the app's settings object (`SETTINGS_KEYS` in the app's `lib/sync.js`), stored whole in
  `users.profile`. The newest copy from any of the person's phones wins. The server stores it without
  understanding it, so new app settings sync without a server change.
- **Stats** (`stats`): level, streak and XP, shown to the group.
- **Client** (`client`): which app and version the phone runs.

### Shared foods

Custom foods anyone creates are gathered into a shared pool (`_absorb_foods`), duplicates are merged
(`_dedupe_foods`) and the full list is handed back into everyone's settings (`_spread_foods`).

### App updates and the old app

The app asks `GET /api/app?app=fossfitness` for the newest version, then `POST /api/app/link` for a short-lived
download link (`/dl/...`). Builds are stored per channel (`CHANNELS`). The `healthtracker` channel serves the old
"Health Tracker" app (no `app` parameter), and `/api/handoff` plus `/api/handoff/claim` move a signed-in person
from that app to FossFitness with a one-time code.

### Schema changes

The schema is created with `CREATE TABLE IF NOT EXISTS`. New columns are added in `Hub.__init__` with a check
followed by `ALTER TABLE ... ADD COLUMN ... DEFAULT ...` (see `profile_client_at` and `tokens.app`). There is no
separate migration tool: add your column the same way, with a default, so existing databases upgrade on start.

## Rules that protect people's data

1. **Never drop or rename a table or column, and never change what one means.** Add new ones with defaults.
2. **Stay compatible with every app version still in use.** A request from an older app must still work, and a
   response must not contain anything that makes an older app fail. New fields in requests and responses are fine;
   removed or renamed ones are not.
3. **Treat settings and entry `data` as opaque.** The server stores and returns them; it should only read the
   fields it really needs (Compare and shared foods do).
4. **Keep the goal maths identical to the app.** If you change `calorie_target`, `protein_goal`, `water_goal_ml` or
   `goal_hits`, change the app's formulas the same way (and the other way round). `test_goal_maths_match_the_app`
   holds the shared examples.
5. **Never widen what members can see of each other.** Anything new that's visible to other members needs an issue
   and agreement first.
6. **Deploys back up the database first** (`deploy.sh` keeps the last 5 copies). Keep it that way.

## Security rules

HealthHub holds health data and is reachable from the internet, so:

- **Check authorisation in `Hub`, not in the HTTP layer.** Every method that reads or changes data calls
  `self.authenticate(...)` and checks the caller is allowed (themselves, or the admin).
- **Use parameterised SQL only** (`?` placeholders). Never build SQL from strings.
- **Compare secrets with `hmac.compare_digest`**, and hash anything that can be guessed with scrypt.
- **Validate every input**: types, lengths and patterns (`ID_RE`, `DAY_RE`, `USER_RE`) and the size limits.
- **Don't log personal data.** Errors print only the exception, never request bodies.
- **Never send tracebacks or internal details to clients.** The HTTP layer already turns unexpected errors into a
  plain message.
- **Keep the web pages' Content-Security-Policy.** No external scripts, fonts or images.

Found a vulnerability? Report it privately (see [SECURITY.md](SECURITY.md)), not in an issue.

## Writing code

- Keep to the standard library. Keep `healthhub.py` as one file unless there's agreement to split it.
- Put behaviour in `Hub` methods and keep the HTTP handler thin.
- Every change needs a test in `test_healthhub.py`, and every bug fix a test that failed before the fix. Tests run a
  real server, so they cover the HTTP layer as well.
- Error messages are shown to people in the app, so write them in plain, friendly English.
- Keep diffs focused; don't reformat code you aren't changing.

## Pull requests and review

1. Branch from `main`, make one change, and open a pull request using the template.
2. Say whether the change touches the database, the sync protocol, or what members can see. If it changes what the
   app sends or receives, link the matching FossFitness pull request.
3. CI must pass.

Reviewers check:

- **Promises:** still standard library only, invite only, least sharing, no new outbound connections.
- **Data:** schema changes only add, with defaults; sync stays compatible with older apps; there's a test.
- **Security:** authorisation in `Hub`, parameterised SQL, input validated, no personal data in logs.
- **Correctness and scope:** the change does what it says, with tests, and nothing else.

Only maintainers merge and deploy.

## Automated contributors

Bots and AI agents follow the stricter rules in [AGENTS.md](AGENTS.md). Their pull requests are reviewed first by an
automated reviewer; anything it can't verify goes to a human maintainer, who always has the final say.

## Releases and deployment

There are no tagged releases yet: `main` is what runs. Maintainers deploy with `./deploy.sh <host>`, which backs up
the database first. `VERSION` in `healthhub.py` is shown at `/healthz`. Raise it when the API changes.

## License

HealthHub is licensed under the GNU Affero General Public License v3.0 or later. By contributing, you agree that
your contribution is licensed under the same terms.
