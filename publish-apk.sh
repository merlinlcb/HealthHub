#!/bin/sh
# Put an APK on the server so /get hands it out and the app announces the update.
#   server/publish-apk.sh <ssh-host> <apk> <versionCode> <versionName>
# e.g. the APK from a GitHub release, or app/android/app/build/outputs/apk/release/app-release.apk
set -eu
HOST="${1:?usage: publish-apk.sh <ssh-host> <apk> <versionCode> <versionName>}"
APK="${2:?apk path}"; CODE="${3:?versionCode}"; NAME="${4:?versionName}"
[ -f "$APK" ] || { echo "No such file: $APK" >&2; exit 1; }
echo "Publishing $NAME (code $CODE) to $HOST"
ssh "$HOST" 'mkdir -p /var/lib/healthhub/apk && cat > /var/lib/healthhub/apk/HealthTracker.apk.new' < "$APK"
ssh "$HOST" "cd /var/lib/healthhub/apk && mv HealthTracker.apk.new HealthTracker.apk \
  && printf '{\"versionCode\": %s, \"versionName\": \"%s\"}\n' '$CODE' '$NAME' > version.json \
  && chown -R healthhub:healthhub . && cat version.json"
