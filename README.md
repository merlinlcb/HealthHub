# HealthHub

The small, self-hosted server behind **[FossFitness](https://github.com/merlinlcb/FossFitness)**, the free and
open-source fitness tracker for Android.

The app works fully on its own. Run HealthHub when you want the group features: chat, comparing progress with
friends or family, a weekly XP race, shared foods, and backing up your history so a new phone can restore it.

- **One Python file, standard library only.** No pip installs, no framework, no external services. Data lives in
  one SQLite file.
- **Yours.** It runs on any home server, small VPS or LAN address, behind whatever HTTPS proxy you already use.
- **Invite only.** Accounts can only be created with the server's invite code, or a one-time code from someone
  already in the group.

## What it provides

| Feature | Details |
|---|---|
| Accounts and sync | Everything the app logs syncs to the server. Signing in on a new phone restores it |
| Chat and shared notes | For everyone on the server |
| Weekly XP race | Levels, streaks and XP side by side |
| Compare | Everyone's weight trend and which daily goals they hit. Only yes/no per goal is shared, never calories or food |
| Shared custom foods | Foods anyone makes are offered to everyone, with an editor in the web dashboard |
| Web dashboard | At `/`: your own trends (weight, calories, water, workouts, fasting); the admin can see everyone's |
| App downloads and updates | `/get` hands the app to people with the invite code; the app checks `/api/app` for newer versions |

## Quick start

```bash
HT_INVITE_CODE=letmein HT_DATA=./data python3 healthhub.py
# open http://127.0.0.1:8787 (health check: /healthz)
```

In the app, open **Settings → App settings → Cloud sync**, enter your server's address (for example
`http://192.168.1.20:8787` on a home network) and create an account with the invite code.
**The first account becomes the admin.**

## Install on a server

`deploy.sh` installs or updates HealthHub as a hardened systemd service on a Debian or Ubuntu host you can reach
over ssh as root:

```bash
./deploy.sh root@my-server
```

The first run creates a `healthhub` system user, `/opt/healthhub`, `/var/lib/healthhub` and the systemd unit, and
writes `/etc/healthhub.env` with a random invite code (printed once). Later runs back up the database (keeping the
last 5 copies), update the code and restart the service.

Put it behind any HTTPS reverse proxy (Caddy, nginx, nginx-proxy-manager, a Cloudflare Tunnel…) forwarding to port
8787. HTTPS is only needed for use away from home.

## Configuration

Environment variables, usually in `/etc/healthhub.env` (see [`healthhub.env.example`](healthhub.env.example)):

| Variable | Default | |
|---|---|---|
| `HT_INVITE_CODE` | (required) | Code needed to create an account |
| `HT_DATA` | `./data` | Folder for `healthhub.db` and the app downloads |
| `HT_BIND` / `HT_PORT` | `0.0.0.0` / `8787` | Listen address and port |
| `HT_FOSSFITNESS_REPO` | (off) | GitHub repo whose releases to offer as app updates, e.g. `merlinlcb/FossFitness` or your fork. Checked every 15 minutes; a public repo needs no token |
| `HT_GITHUB_TOKEN` | (none) | Read-only GitHub token: needed for a private app repo, and turns on the legacy channel below |
| `HT_GITHUB_REPO` | `merlinlcb/HealthTracker` | Legacy update channel for the app's old "Health Tracker" builds (checked only when `HT_GITHUB_TOKEN` is set) |
| `HT_UPLOAD_TOKEN` | (none) | Lets a build upload an APK to `/api/app/upload` |
| `HT_ACCESS_LOG` | (off) | Log every request |

## App updates

The server can hand out new app versions in three ways:

- **From GitHub releases** (easiest): set `HT_FOSSFITNESS_REPO`. New releases are offered within 15 minutes.
- **Upload from a build:** `POST /api/app/upload` with `HT_UPLOAD_TOKEN`.
- **By hand:** `./publish-apk.sh root@my-server FossFitness.apk <versionCode> <versionName>`.

## Privacy

- **The admin** (the first account) can see everyone's synced history in the dashboard. Choose who runs your server.
- **Other members** see only levels, streaks, XP, chat, shared notes and the Compare view, never food logs.
- **The app's health trackers** (medications, blood pressure and so on) are never sent to the server.
- **Passwords** are stored as salted scrypt hashes; sign-in tokens are stored hashed.
- **Outbound connections:** the server only connects out to check GitHub for app releases, and only when you set
  it up to.

## Tests

```bash
python3 -W ignore -m unittest -v test_healthhub.py
```

## Contributing

Help is welcome. [CONTRIBUTING.md](CONTRIBUTING.md) explains how the server is put together and the rules that keep
people's data safe. Automated contributors also follow [AGENTS.md](AGENTS.md). To report a security problem
privately, see [SECURITY.md](SECURITY.md).

## License

HealthHub is free software under the [GNU Affero General Public License v3.0 or later](LICENSE). If you run a
modified version for other people, you must offer them its source code.
