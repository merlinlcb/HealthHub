# HealthHub

The self-hosted server behind the [Health Tracker](https://github.com/merlinlcb/HealthTracker) Android app.
One Python file using only the standard library, plus SQLite. No pip installs, no external services.
It runs on any domain or LAN address you choose.

It provides:

- **Accounts and sync**: everything the app logs syncs to the server, and signing in on a new phone restores it. Accounts can only be created with the server's invite code (or a one-time invite from an existing user).
- **Chat** and a **weekly XP race** between everyone on the server.
- **A web dashboard** at `/`, with per-person trends (weight, calories, water, workouts, fasting).
- **Compare**: everyone's weight and goals side by side.
- **Shared custom foods**, with an editor in the web UI.
- **APK hand-out and in-app updates**:
  - `/get` gives the app to anyone with the invite code.
  - The app asks `/api/app` for newer versions and downloads them through a short-lived link.

## Quick start (local)

```bash
HT_INVITE_CODE=letmein HT_DATA=./data python3 healthhub.py
# → http://127.0.0.1:8787  (health check: /healthz)
```

In the app, open Settings → App settings → Cloud sync and enter your server's address, for example `http://192.168.1.20:8787`.
Then create an account with the invite code. The first account is the admin.

## Install on a server

`deploy.sh` installs or updates HealthHub as a hardened systemd service. It needs a Debian or Ubuntu host you can reach over ssh as root:

```bash
./deploy.sh root@my-server
```

The first run:

- creates a `healthhub` user
- creates `/opt/healthhub` and `/var/lib/healthhub`
- installs the systemd unit
- creates `/etc/healthhub.env` with a random invite code and upload token, printed once

Later runs back up the database (the last 5 copies are kept), update the code and restart the service.

Put it behind any HTTPS reverse proxy (nginx, Caddy, nginx-proxy-manager, Cloudflare Tunnel…) that forwards to port 8787.
The app works with any address; only HTTPS is needed for use away from home.

## Configuration

Settings are environment variables. See [`healthhub.env.example`](healthhub.env.example).

| Variable | Default | |
|---|---|---|
| `HT_INVITE_CODE` | (required) | Code needed to create an account |
| `HT_DATA` | `./data` | Folder for `healthhub.db` and published APKs |
| `HT_BIND` / `HT_PORT` | `0.0.0.0` / `8787` | Listen address |
| `HT_UPLOAD_TOKEN` | (none) | Lets a build upload an APK to `/api/app/upload` |
| `HT_GITHUB_TOKEN` | (none) | Read-only token. The server then checks the app repo's latest release every 15 minutes and offers newer APKs |
| `HT_GITHUB_REPO` | `merlinlcb/HealthTracker` | Repo whose releases to check (point it at your fork) |
| `HT_ACCESS_LOG` | (off) | Log every request |

## Publishing app updates

There are three ways:

- **Automatic**: set `HT_GITHUB_TOKEN`. When the app repo's CI publishes a release with `HealthTracker.apk`, the server picks it up within 15 minutes.
- **Upload from a build**: `POST /api/app/upload` with `HT_UPLOAD_TOKEN`.
- **By hand**: `./publish-apk.sh root@my-server HealthTracker.apk <versionCode> <versionName>`

## Tests

```bash
python3 -W ignore -m unittest test_healthhub.py
```

The app repo also has end-to-end sync tests that run against a live server (`HT_TEST_SERVER=… npx jest integration`).

## Privacy

- **Admin view**: the admin (the first account) can see everyone's history. Everyone else sees only levels, streaks, XP, chat, and the goal-based Compare view (never food logs).
- **Storage**: passwords are stored as salted scrypt hashes.
- **Outbound connections**: the server only connects out to check GitHub releases, and only when `HT_GITHUB_TOKEN` is set.
