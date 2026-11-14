#!/bin/sh
# Install or update HealthHub on a Debian/Ubuntu host you can ssh to as root.
#   server/deploy.sh <ssh-host>
# First run creates the user, the service and a random invite code (printed once
# and kept in /etc/healthhub.env). Later runs only update the code and restart.
set -eu
HOST="${1:?usage: deploy.sh <ssh-host>}"
cd "$(dirname "$0")"
tar -cf - healthhub.py healthhub.service static | ssh "$HOST" '
  set -eu
  command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3; }
  id healthhub >/dev/null 2>&1 || useradd --system --home /var/lib/healthhub --shell /usr/sbin/nologin healthhub
  mkdir -p /opt/healthhub /var/lib/healthhub/apk
  if [ -f /var/lib/healthhub/healthhub.db ]; then
    cp -a /var/lib/healthhub/healthhub.db "/var/lib/healthhub/healthhub.db.bak-$(date +%Y%m%d-%H%M%S)"
    ls -t /var/lib/healthhub/healthhub.db.bak-* | tail -n +6 | xargs -r rm -f
  fi
  tar -C /opt/healthhub -xf - --no-same-owner
  chown -R healthhub:healthhub /var/lib/healthhub
  chmod 750 /var/lib/healthhub
  if [ ! -f /etc/healthhub.env ]; then
    code=$(python3 -c "import secrets; print(secrets.token_urlsafe(9))")
    ( umask 077; echo "HT_INVITE_CODE=$code" > /etc/healthhub.env )
    echo "Invite code (share with your brother): $code"
  fi
  mv /opt/healthhub/healthhub.service /etc/systemd/system/healthhub.service
  systemctl daemon-reload
  systemctl enable -q healthhub
  systemctl restart healthhub
  sleep 1
  python3 -c "import urllib.request; print(urllib.request.urlopen(\"http://127.0.0.1:8787/healthz\").read().decode())"
'
