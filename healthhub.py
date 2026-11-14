#!/usr/bin/env python3
"""HealthHub - the shared server behind the Health Tracker app.

Python standard library only (no pip installs). One SQLite file holds
accounts, synced entries, chat and each person's game summary. It also
serves the web dashboard at /.

Configuration (environment):
  HT_DATA         directory for healthhub.db          (default ./data)
  HT_BIND         listen address                      (default 0.0.0.0)
  HT_PORT         listen port                         (default 8787)
  HT_INVITE_CODE  code needed to create an account    (required)
"""
import datetime
import hashlib
import math
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "2.0.0"
KINDS = {"day", "calorie", "weight", "fast", "workout", "water"}
MAX_ENTRY = 64 * 1024
MAX_SETTINGS = 512 * 1024
MAX_BODY = 8 * 1024 * 1024
MAX_PUSH = 20000
MAX_MSG = 2000
ID_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,80}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USER_RE = re.compile(r"^[a-z0-9_.\-]{2,32}$")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  pw_salt BLOB NOT NULL,
  pw_hash BLOB NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  profile TEXT NOT NULL DEFAULT '{}',
  profile_updated INTEGER NOT NULL DEFAULT 0,
  stats TEXT NOT NULL DEFAULT '{}',
  stats_updated INTEGER NOT NULL DEFAULT 0,
  created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  label TEXT NOT NULL DEFAULT '',
  created INTEGER NOT NULL,
  last_used INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  id TEXT NOT NULL,
  kind TEXT NOT NULL,
  ts INTEGER NOT NULL,
  day TEXT NOT NULL,
  data TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  rev INTEGER NOT NULL,
  PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS entries_rev ON entries (user_id, rev);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'msg',
  body TEXT NOT NULL,
  ts INTEGER NOT NULL
);
"""


def now_ms():
    return int(time.time() * 1000)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def hash_password(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Hub:
    """All server behaviour, independent of HTTP so it is easy to test."""

    def __init__(self, data_dir, invite_code):
        if not invite_code:
            raise ValueError("an invite code is required")
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "healthhub.db")
        self.invite_code = invite_code
        self._fail_lock = threading.Lock()
        self._fails = {}  # key -> [timestamps of recent failures]
        with self.db() as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('rev', '0')")

    def db(self):
        conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── sign-in throttling ────────────────────────────────────────────────
    def _throttled(self, key, limit=8, window=900):
        cutoff = time.time() - window
        with self._fail_lock:
            recent = [t for t in self._fails.get(key, []) if t > cutoff]
            self._fails[key] = recent
            return len(recent) >= limit

    def _note_failure(self, key):
        with self._fail_lock:
            self._fails.setdefault(key, []).append(time.time())

    # ── accounts ──────────────────────────────────────────────────────────
    @staticmethod
    def _user_public(row):
        return {
            "id": row["id"],
            "username": row["username"],
            "displayName": row["display_name"],
            "isAdmin": bool(row["is_admin"]),
        }

    def _issue_token(self, conn, user_id, label):
        token = secrets.token_urlsafe(32)
        t = now_ms()
        conn.execute(
            "INSERT INTO tokens (token_hash, user_id, label, created, last_used) VALUES (?,?,?,?,?)",
            (token_hash(token), user_id, str(label or "")[:60], t, t),
        )
        return token

    def register(self, body):
        if self._throttled("register"):
            raise ApiError(429, "Too many attempts. Try again in a few minutes.")
        invite = str(body.get("invite") or "")
        if not hmac.compare_digest(invite.encode(), self.invite_code.encode()):
            self._note_failure("register")
            raise ApiError(403, "That invite code is not right.")
        username = str(body.get("username") or "").strip().lower()
        display = str(body.get("displayName") or "").strip()[:40] or username
        password = str(body.get("password") or "")
        if not USER_RE.match(username):
            raise ApiError(400, "Username must be 2-32 characters: letters, numbers, . _ -")
        if len(password) < 8:
            raise ApiError(400, "Password must be at least 8 characters.")
        salt = secrets.token_bytes(16)
        pw = hash_password(password, salt)
        conn = self.db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise ApiError(409, "That username is taken.")
            first = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0
            cur = conn.execute(
                "INSERT INTO users (username, display_name, pw_salt, pw_hash, is_admin, created)"
                " VALUES (?,?,?,?,?,?)",
                (username, display, salt, pw, 1 if first else 0, now_ms()),
            )
            token = self._issue_token(conn, cur.lastrowid, body.get("device"))
            row = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
            conn.execute("COMMIT")
            return {"token": token, "user": self._user_public(row)}
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def login(self, body):
        username = str(body.get("username") or "").strip().lower()
        password = str(body.get("password") or "")
        key = "login:" + username
        if self._throttled(key):
            raise ApiError(429, "Too many attempts. Try again in a few minutes.")
        conn = self.db()
        try:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            # Hash even when the user is unknown so timing does not reveal it.
            salt = row["pw_salt"] if row else b"0" * 16
            candidate = hash_password(password, salt)
            if not row or not hmac.compare_digest(candidate, row["pw_hash"]):
                self._note_failure(key)
                raise ApiError(401, "Wrong username or password.")
            token = self._issue_token(conn, row["id"], body.get("device"))
            return {"token": token, "user": self._user_public(row)}
        finally:
            conn.close()

    def authenticate(self, conn, token):
        if not token:
            raise ApiError(401, "Sign in first.")
        row = conn.execute(
            "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash=?",
            (token_hash(token),),
        ).fetchone()
        if not row:
            raise ApiError(401, "Your sign-in has expired. Sign in again.")
        return row

    def logout(self, token):
        with self.db() as conn:
            conn.execute("DELETE FROM tokens WHERE token_hash=?", (token_hash(token or ""),))
        return {"ok": True}

    def change_password(self, token, body):
        conn = self.db()
        try:
            user = self.authenticate(conn, token)
            old = str(body.get("oldPassword") or "")
            new = str(body.get("newPassword") or "")
            if not hmac.compare_digest(hash_password(old, user["pw_salt"]), user["pw_hash"]):
                raise ApiError(401, "Current password is wrong.")
            if len(new) < 8:
                raise ApiError(400, "Password must be at least 8 characters.")
            salt = secrets.token_bytes(16)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE users SET pw_salt=?, pw_hash=? WHERE id=?",
                         (salt, hash_password(new, salt), user["id"]))
            # Sign every other device out.
            conn.execute("DELETE FROM tokens WHERE user_id=? AND token_hash<>?",
                         (user["id"], token_hash(token)))
            conn.execute("COMMIT")
            return {"ok": True}
        finally:
            conn.close()

    # ── sync ──────────────────────────────────────────────────────────────
    @staticmethod
    def _clean_entry(e):
        if not isinstance(e, dict):
            raise ApiError(400, "Bad entry.")
        eid, kind, day = e.get("id"), e.get("kind"), e.get("day")
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        if not isinstance(eid, str) or not ID_RE.match(eid):
            raise ApiError(400, "Bad entry id.")
        if kind not in KINDS:
            raise ApiError(400, "Unknown entry kind: %r" % (kind,))
        if not isinstance(day, str) or not DAY_RE.match(day):
            raise ApiError(400, "Bad entry day.")
        try:
            ts = int(e.get("ts"))
            updated = int(e.get("updatedAt") or ts)
        except (TypeError, ValueError):
            raise ApiError(400, "Bad entry time.")
        blob = json.dumps(data, separators=(",", ":"))
        if len(blob) > MAX_ENTRY:
            raise ApiError(400, "Entry too large.")
        return eid, kind, ts, day, blob, updated, 1 if e.get("deleted") else 0

    @staticmethod
    def _entry_out(row):
        return {
            "id": row["id"], "kind": row["kind"], "ts": row["ts"], "day": row["day"],
            "data": json.loads(row["data"]), "updatedAt": row["updated_at"],
            "deleted": bool(row["deleted"]),
        }

    def sync(self, token, body):
        push = body.get("push") or []
        if not isinstance(push, list) or len(push) > MAX_PUSH:
            raise ApiError(400, "Bad push list.")
        cleaned = [self._clean_entry(e) for e in push]
        try:
            since = int(body.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        conn = self.db()
        try:
            user = self.authenticate(conn, token)
            uid = user["id"]
            conn.execute("BEGIN IMMEDIATE")
            # Pull first: everything this device has not seen yet.
            pulled = [self._entry_out(r) for r in conn.execute(
                "SELECT * FROM entries WHERE user_id=? AND rev>? ORDER BY rev", (uid, since))]
            rev = int(conn.execute("SELECT value FROM meta WHERE key='rev'").fetchone()["value"])
            accepted = 0
            if cleaned:
                rev += 1
                for eid, kind, ts, day, blob, updated, deleted in cleaned:
                    cur = conn.execute(
                        "INSERT INTO entries (user_id,id,kind,ts,day,data,updated_at,deleted,rev)"
                        " VALUES (?,?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(user_id,id) DO UPDATE SET kind=excluded.kind, ts=excluded.ts,"
                        " day=excluded.day, data=excluded.data, updated_at=excluded.updated_at,"
                        " deleted=excluded.deleted, rev=excluded.rev"
                        " WHERE excluded.updated_at > entries.updated_at",
                        (uid, eid, kind, ts, day, blob, updated, deleted, rev),
                    )
                    accepted += cur.rowcount
                conn.execute("UPDATE meta SET value=? WHERE key='rev'", (str(rev),))

            # Profile: newest copy wins.
            profile_out = None
            prof = body.get("profile")
            server_prof_at = user["profile_updated"]
            if isinstance(prof, dict) and isinstance(prof.get("data"), dict):
                try:
                    client_at = int(prof.get("updatedAt") or 0)
                except (TypeError, ValueError):
                    client_at = 0
                blob = json.dumps(prof["data"], separators=(",", ":"))
                if len(blob) > MAX_SETTINGS:
                    raise ApiError(400, "Settings too large.")
                if client_at > server_prof_at:
                    conn.execute("UPDATE users SET profile=?, profile_updated=? WHERE id=?",
                                 (blob, client_at, uid))
                elif client_at < server_prof_at:
                    profile_out = {"data": json.loads(user["profile"]), "updatedAt": server_prof_at}
            elif server_prof_at:
                profile_out = {"data": json.loads(user["profile"]), "updatedAt": server_prof_at}

            stats = body.get("stats")
            if isinstance(stats, dict):
                blob = json.dumps(stats, separators=(",", ":"))
                if len(blob) > 8000:
                    raise ApiError(400, "Stats too large.")
                conn.execute("UPDATE users SET stats=?, stats_updated=? WHERE id=?",
                             (blob, now_ms(), uid))
            conn.execute("UPDATE tokens SET last_used=? WHERE token_hash=?",
                         (now_ms(), token_hash(token)))
            conn.execute("COMMIT")
            return {"rev": rev, "entries": pulled, "profile": profile_out, "accepted": accepted,
                    "user": self._user_public(user)}
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ── friends (the game leaderboard) ────────────────────────────────────
    def friends(self, token):
        conn = self.db()
        try:
            me = self.authenticate(conn, token)
            out = []
            for r in conn.execute("SELECT * FROM users ORDER BY id"):
                out.append({
                    "id": r["id"], "displayName": r["display_name"], "isMe": r["id"] == me["id"],
                    "stats": json.loads(r["stats"]), "statsUpdated": r["stats_updated"],
                })
            return {"people": out}
        finally:
            conn.close()

    # ── chat ──────────────────────────────────────────────────────────────
    def chat_list(self, token, since, limit=200):
        conn = self.db()
        try:
            me = self.authenticate(conn, token)
            if since > 0:
                rows = conn.execute(
                    "SELECT m.*, u.display_name FROM messages m JOIN users u ON u.id=m.user_id"
                    " WHERE m.id>? ORDER BY m.id LIMIT ?", (since, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM (SELECT m.*, u.display_name FROM messages m"
                    " JOIN users u ON u.id=m.user_id ORDER BY m.id DESC LIMIT ?) ORDER BY id",
                    (limit,)).fetchall()
            return {"messages": [{
                "id": r["id"], "userId": r["user_id"], "name": r["display_name"],
                "mine": r["user_id"] == me["id"], "kind": r["kind"], "body": r["body"], "ts": r["ts"],
            } for r in rows]}
        finally:
            conn.close()

    def chat_post(self, token, body):
        text = str(body.get("body") or "").strip()
        kind = "event" if body.get("kind") == "event" else "msg"
        if not text:
            raise ApiError(400, "Message is empty.")
        if len(text) > MAX_MSG:
            raise ApiError(400, "Message is too long.")
        conn = self.db()
        try:
            me = self.authenticate(conn, token)
            cur = conn.execute("INSERT INTO messages (user_id, kind, body, ts) VALUES (?,?,?,?)",
                               (me["id"], kind, text, now_ms()))
            return {"id": cur.lastrowid}
        finally:
            conn.close()

    # ── app download ──────────────────────────────────────────────────────
    # Put the build at <HT_DATA>/apk/HealthTracker.apk with a version.json
    # beside it: {"versionCode": 2, "versionName": "2.0.0"}.
    def apk_info(self):
        folder = os.path.join(os.path.dirname(self.db_path), "apk")
        apk = os.path.join(folder, "HealthTracker.apk")
        info = {"available": os.path.isfile(apk), "versionCode": 0, "versionName": ""}
        try:
            with open(os.path.join(folder, "version.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
            info["versionCode"] = int(meta.get("versionCode") or 0)
            info["versionName"] = str(meta.get("versionName") or "")[:20]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return apk, info

    def apk_for_code(self, code):
        """Path to the APK if the invite code is right."""
        if self._throttled("apk"):
            raise ApiError(429, "Too many attempts. Try again in a few minutes.")
        if not hmac.compare_digest(str(code or "").strip().encode(), self.invite_code.encode()):
            self._note_failure("apk")
            raise ApiError(403, "That invite code is not right.")
        apk, info = self.apk_info()
        if not info["available"]:
            raise ApiError(404, "No app build has been uploaded yet.")
        return apk, info

    # ── compare (everyone can see this) ───────────────────────────────────
    def compare(self, token, days, today=None):
        """Per person: weights and whether each daily goal was hit. Only yes/no per
        goal is shared, never the underlying calories, water or food."""
        days = max(1, min(int(days or 7), 90))
        conn = self.db()
        try:
            me = self.authenticate(conn, token)
            end = datetime.date.fromisoformat(today) if today else datetime.date.today()
            keys = [(end - datetime.timedelta(days=i)).isoformat() for i in range(days)]
            start = keys[-1]
            people = []
            for u in conn.execute("SELECT * FROM users ORDER BY id"):
                settings = json.loads(u["profile"] or "{}")
                profile = settings.get("profile") or {}
                fasting_on = (settings.get("fasting") or {}).get("enabled", True) is not False
                rows = conn.execute(
                    "SELECT kind, day, ts, data FROM entries WHERE user_id=? AND deleted=0 AND kind IN ('day','weight','fast')",
                    (u["id"],)).fetchall()
                by_day = {}
                weights = []
                fasts = {}
                for r in rows:
                    d = json.loads(r["data"])
                    if r["kind"] == "day":
                        by_day[r["day"]] = d
                    elif r["kind"] == "weight" and d.get("kg"):
                        weights.append((r["day"], r["ts"], float(d["kg"])))
                    elif r["kind"] == "fast" and d.get("start") and d.get("end"):
                        h = (_iso_ms(d["end"]) - _iso_ms(d["start"])) / 3600000
                        fasts[r["day"]] = max(fasts.get(r["day"], 0), h)
                weights.sort()
                weighed = {w[0] for w in weights}
                goal_rows = []
                for k in keys:
                    goal_rows.append({"day": k, "hits": goal_hits(profile, by_day.get(k), k in weighed,
                                                                  fasts.get(k, 0), fasting_on)})
                before = [w for w in weights if w[0] < start]
                in_range = [w for w in weights if start <= w[0] <= keys[0]]
                base = (before[-1] if before else (in_range[0] if in_range else None))
                current = weights[-1] if weights else None
                people.append({
                    "id": u["id"], "displayName": u["display_name"], "isMe": u["id"] == me["id"],
                    "currentKg": current[2] if current else None,
                    "currentDay": current[0] if current else None,
                    "startKg": base[2] if base else None,
                    "goalKg": profile.get("goalWeightKg") if profile.get("goal") == "lose" else None,
                    "units": profile.get("units") or "imperial",
                    "fasting": fasting_on,
                    "level": (json.loads(u["stats"] or "{}")).get("level"),
                    "streak": (json.loads(u["stats"] or "{}")).get("streak"),
                    "days": goal_rows,
                })
            return {"days": keys, "goals": GOALS, "people": people}
        finally:
            conn.close()

    # ── custom foods (anyone can see and edit anyone's) ───────────────────
    # The app keeps custom foods in the settings it syncs (users.profile). Editing
    # them here marks the settings newer, so each phone pulls the change on its
    # next sync, with no app change needed.
    FOOD_NUMS = ("kcal", "protein", "carbs", "fat", "fiber")

    @staticmethod
    def _food_out(f):
        return {"id": str(f.get("id")), "name": str(f.get("name") or ""),
                "servingLabel": str(f.get("servingLabel") or "1 serving"),
                **{k: round(_num(f.get(k)), 1) for k in Hub.FOOD_NUMS}}

    def custom_foods(self, token):
        conn = self.db()
        try:
            me = self.authenticate(conn, token)
            people = []
            for u in conn.execute("SELECT id, display_name, profile FROM users ORDER BY id"):
                foods = self._settings(u).get("customFoods") or []
                people.append({"id": u["id"], "displayName": u["display_name"], "isMe": u["id"] == me["id"],
                               "foods": [self._food_out(f) for f in foods if isinstance(f, dict) and f.get("id")]})
            return {"people": people}
        finally:
            conn.close()

    @staticmethod
    def _settings(row):
        try:
            s = json.loads(row["profile"] or "{}")
        except ValueError:
            s = {}
        return s if isinstance(s, dict) else {}

    def _edit_foods(self, token, owner_id, fn):
        conn = self.db()
        try:
            self.authenticate(conn, token)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM users WHERE id=?", (owner_id,)).fetchone()
            if not row:
                raise ApiError(404, "No such person.")
            settings = self._settings(row)
            foods = [f for f in (settings.get("customFoods") or []) if isinstance(f, dict)]
            result = fn(settings, foods)
            blob = json.dumps(settings, separators=(",", ":"))
            if len(blob) > MAX_SETTINGS:
                raise ApiError(400, "Too many custom foods.")
            conn.execute("UPDATE users SET profile=?, profile_updated=? WHERE id=?",
                         (blob, max(now_ms(), row["profile_updated"] + 1), owner_id))
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def save_custom_food(self, token, body):
        try:
            owner_id = int(body.get("ownerId"))
        except (TypeError, ValueError):
            raise ApiError(400, "Pick whose food this is.")
        f = body.get("food") if isinstance(body.get("food"), dict) else {}
        name = str(f.get("name") or "").strip()[:120]
        if not name:
            raise ApiError(400, "Give the food a name.")
        clean = {"name": name, "servingLabel": (str(f.get("servingLabel") or "").strip()[:60] or "1 serving")}
        for k in self.FOOD_NUMS:
            v = _num(f.get(k))
            if v < 0 or v > 20000:
                raise ApiError(400, "Numbers must be between 0 and 20,000.")
            clean[k] = round(v, 1)
        fid = str(f.get("id") or "")

        def apply(settings, foods):
            if fid:
                for i, x in enumerate(foods):
                    if str(x.get("id")) == fid:
                        foods[i] = {**x, **clean}
                        break
                else:
                    raise ApiError(404, "That food no longer exists.")
                out = foods[i]
            else:
                out = {"id": "my-" + secrets.token_hex(6), **clean}
                foods.append(out)
            settings["customFoods"] = foods
            return {"food": self._food_out(out)}
        return self._edit_foods(token, owner_id, apply)

    def delete_custom_food(self, token, body):
        try:
            owner_id = int(body.get("ownerId"))
        except (TypeError, ValueError):
            raise ApiError(400, "Pick whose food this is.")
        fid = str(body.get("id") or "")

        def apply(settings, foods):
            settings["customFoods"] = [f for f in foods if str(f.get("id")) != fid]
            # Same clean-up the app does when you delete a custom food.
            settings["favorites"] = [x for x in (settings.get("favorites") or []) if x != fid]
            settings["recentFoods"] = [r for r in (settings.get("recentFoods") or [])
                                       if not (isinstance(r, dict) and r.get("foodId") == fid)]
            return {"ok": True}
        return self._edit_foods(token, owner_id, apply)

    # ── dashboard ─────────────────────────────────────────────────────────
    def dashboard_users(self, token):
        conn = self.db()
        try:
            me = self.authenticate(conn, token)
            if me["is_admin"]:
                rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
            else:
                rows = [me]
            return {"me": self._user_public(me), "users": [self._user_public(r) for r in rows]}
        finally:
            conn.close()

    def dashboard_data(self, token, user_id):
        conn = self.db()
        try:
            me = self.authenticate(conn, token)
            if user_id != me["id"] and not me["is_admin"]:
                raise ApiError(403, "You can only view your own history.")
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise ApiError(404, "No such person.")
            entries = [self._entry_out(r) for r in conn.execute(
                "SELECT * FROM entries WHERE user_id=? AND deleted=0 ORDER BY ts", (user_id,))]
            return {
                "user": self._user_public(row),
                "profile": json.loads(row["profile"]),
                "stats": json.loads(row["stats"]),
                "statsUpdated": row["stats_updated"],
                "entries": entries,
            }
        finally:
            conn.close()


# ── HTTP layer ────────────────────────────────────────────────────────────────
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


# ── goal maths, the same as the app (app/src/lib/calc.js and game.js) ────────
GOALS = [
    {"id": "water", "icon": "💧", "label": "Water"},
    {"id": "food", "icon": "🍽️", "label": "Calories on target"},
    {"id": "protein", "icon": "🥩", "label": "Protein"},
    {"id": "move", "icon": "🏋️", "label": "Workout"},
    {"id": "weigh", "icon": "⚖️", "label": "Weigh-in"},
    {"id": "fast", "icon": "⏱️", "label": "12h fast"},
]
_FACTORS = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "very": 1.725, "extreme": 1.9}


def _jsround(x):
    return math.floor(x + 0.5)


def _iso_ms(s):
    return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def calorie_target(p):
    kg, cm, age = _num(p.get("weightKg")), _num(p.get("heightCm")), _num(p.get("age"))
    if not (kg and cm and age):
        return 2000
    base = 10 * kg + 6.25 * cm - 5 * age
    bmr = _jsround(base - 161 if p.get("sex") == "female" else base + 5)
    tdee = _jsround(bmr * _FACTORS.get(p.get("activityLevel"), 1.55))
    if not tdee:
        return 2000
    delta = _jsround((_num(p.get("paceKgPerWeek")) or 0.5) * 7700 / 7)
    target = tdee
    if p.get("goal") == "lose":
        target = tdee - delta
    elif p.get("goal") == "gain":
        target = tdee + min(delta, 500)
    return _jsround(max(target, 1200 if p.get("sex") == "female" else 1500))


def protein_goal(p):
    if _num(p.get("proteinGoal")) > 0:
        return _jsround(_num(p["proteinGoal"]))
    kg = _num(p.get("weightKg"))
    if not kg:
        ref = 70
    else:
        ref = kg
        cm = _num(p.get("heightCm"))
        if cm:
            upper = round(24.9 * (cm / 100) ** 2 * 10) / 10
            if kg > upper:
                ref = (kg + upper) / 2
        goal = _num(p.get("goalWeightKg"))
        if goal and p.get("goal") == "lose":
            ref = max(goal, min(ref, kg))
    return _jsround(ref * (1.2 if p.get("goal") == "maintain" else 1.6))


def water_goal_ml(p, exercise_min=0):
    kg = _num(p.get("weightKg")) or 70
    base = min(max(kg * 35, 1500), 4000)
    if p.get("climate") == "hot":
        base += 500
    if p.get("pregnancy") == "pregnant":
        base += 300
    if p.get("pregnancy") == "breastfeeding":
        base += 700
    return _jsround((base + min(exercise_min, 240) * 12) / 50) * 50


def goal_hits(profile, day, weighed, fast_hours, fasting_on):
    day = day or {}
    water = sum(_num(w.get("ml")) for w in day.get("water") or [])
    food = day.get("food") or []
    kcal = sum(_num(f.get("kcal")) for f in food)
    protein = sum(_num(f.get("protein")) for f in food)
    minutes = sum(_num(e.get("minutes")) for e in day.get("exercise") or [])
    target = calorie_target(profile)
    hits = {
        "water": water >= water_goal_ml(profile, minutes),
        "food": bool(food) and target * 0.5 <= kcal <= target,
        "protein": bool(food) and protein >= protein_goal(profile),
        "move": bool((day.get("workout") or {}).get("done")) or minutes > 0,
        "weigh": bool(weighed),
        "fast": fast_hours >= 12,
    }
    if not fasting_on:
        hits["fast"] = None
    return hits


def make_handler(hub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HealthHub/" + VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet by default; errors still print
            if os.environ.get("HT_ACCESS_LOG"):
                super().log_message(fmt, *args)

        def _send(self, status, payload, ctype="application/json; charset=utf-8"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _token(self):
            auth = self.headers.get("Authorization") or ""
            return auth[7:].strip() if auth.lower().startswith("bearer ") else ""

        def _raw_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "Bad request.")
            if length > MAX_BODY:
                raise ApiError(413, "Request too large.")
            return self.rfile.read(length) if length else b""

        def _send_apk(self, path, info):
            name = "HealthTracker-%s.apk" % (re.sub(r"[^0-9A-Za-z.]", "", info["versionName"]) or "latest")
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

        def _body(self):
            raw = self._raw_body() or b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "Request was not valid JSON.")
            if not isinstance(body, dict):
                raise ApiError(400, "Request must be a JSON object.")
            return body

        def _static(self, name):
            path = os.path.join(STATIC_DIR, name)
            ext = os.path.splitext(name)[1]
            if ext not in STATIC_TYPES or not os.path.isfile(path):
                raise ApiError(404, "Not found.")
            with open(path, "rb") as fh:
                self._send(200, fh.read(), STATIC_TYPES[ext])

        def _route(self, method):
            url = urlparse(self.path)
            path, q = url.path, parse_qs(url.query)
            tok = self._token()

            def qint(name, default=0):
                try:
                    return int(q.get(name, [default])[0])
                except (TypeError, ValueError):
                    return default

            if method == "GET":
                if path in ("/", "/index.html"):
                    return self._static("dashboard.html")
                if path == "/get":
                    return self._static("get.html")
                if path == "/healthz":
                    return self._send(200, {"ok": True, "version": VERSION})
                if path == "/api/app":
                    return self._send(200, hub.apk_info()[1])
                if path == "/api/me":
                    conn = hub.db()
                    try:
                        return self._send(200, {"user": hub._user_public(hub.authenticate(conn, tok))})
                    finally:
                        conn.close()
                if path == "/api/friends":
                    return self._send(200, hub.friends(tok))
                if path == "/api/chat":
                    return self._send(200, hub.chat_list(tok, qint("since")))
                if path == "/api/customfoods":
                    return self._send(200, hub.custom_foods(tok))
                if path == "/api/compare":
                    today = q.get("today", [""])[0]
                    return self._send(200, hub.compare(tok, qint("days", 7), today if DAY_RE.match(today) else None))
                if path == "/api/dashboard/users":
                    return self._send(200, hub.dashboard_users(tok))
                if path == "/api/dashboard/data":
                    return self._send(200, hub.dashboard_data(tok, qint("user")))
            elif method == "POST":
                if path == "/download":
                    # A plain HTML form post, so the code never appears in a URL or a log.
                    form = parse_qs(self._raw_body().decode("utf-8", "replace"))
                    apk, info = hub.apk_for_code(form.get("code", [""])[0])
                    return self._send_apk(apk, info)
                body = self._body()
                if path == "/api/register":
                    return self._send(200, hub.register(body))
                if path == "/api/login":
                    return self._send(200, hub.login(body))
                if path == "/api/logout":
                    return self._send(200, hub.logout(tok))
                if path == "/api/password":
                    return self._send(200, hub.change_password(tok, body))
                if path == "/api/sync":
                    return self._send(200, hub.sync(tok, body))
                if path == "/api/chat":
                    return self._send(200, hub.chat_post(tok, body))
                if path == "/api/customfoods/save":
                    return self._send(200, hub.save_custom_food(tok, body))
                if path == "/api/customfoods/delete":
                    return self._send(200, hub.delete_custom_food(tok, body))
            raise ApiError(404, "Not found.")

        def _handle(self, method):
            try:
                self._route(method)
            except ApiError as e:
                self._send(e.status, {"error": e.message})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # never leak a traceback to the client
                print("healthhub error:", repr(e), file=sys.stderr, flush=True)
                self._send(500, {"error": "Something went wrong on the server."})

        def do_GET(self):
            self._handle("GET")

        def do_HEAD(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler


def make_server(data_dir, invite_code, bind="127.0.0.1", port=0):
    hub = Hub(data_dir, invite_code)
    httpd = ThreadingHTTPServer((bind, port), make_handler(hub))
    httpd.daemon_threads = True
    httpd.hub = hub
    return httpd


def main():
    invite = os.environ.get("HT_INVITE_CODE", "")
    if not invite:
        sys.exit("HT_INVITE_CODE must be set (the code people need to create an account).")
    httpd = make_server(os.environ.get("HT_DATA", "./data"), invite,
                        os.environ.get("HT_BIND", "0.0.0.0"), int(os.environ.get("HT_PORT", "8787")))
    print("HealthHub %s listening on %s:%s" % (VERSION, *httpd.server_address), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
