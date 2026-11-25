#!/usr/bin/env python3
"""HealthHub - the self-hosted server behind the FossFitness app (formerly Health Tracker).

SPDX-License-Identifier: AGPL-3.0-or-later

Python standard library only (no pip installs). One SQLite file holds
accounts, synced entries, chat and each person's game summary. It also
serves the web dashboard at /.

Configuration (environment):
  HT_DATA         directory for healthhub.db          (default ./data)
  HT_BIND         listen address                      (default 0.0.0.0)
  HT_PORT         listen port                         (default 8787)
  HT_INVITE_CODE  code needed to create an account    (required)
  HT_UPLOAD_TOKEN secret that lets a build upload new APKs to /api/app/upload (optional)
  HT_GITHUB_TOKEN read-only GitHub token; the server then checks the repo's latest release
                  every 15 minutes and offers any newer APK as an update (optional)
  HT_GITHUB_REPO  owner/repo whose releases to check  (default merlinlcb/HealthTracker)
  HT_FOSSFITNESS_REPO  owner/repo of FossFitness releases (tags vA.B.C, asset FossFitness.apk) to offer
                  to FossFitness installs, checked every 15 minutes (optional; a public repo needs no token)
  HT_ACCESS_LOG   set to anything to log every request (optional)
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
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

VERSION = "2.0.0"
KINDS = {"day", "calorie", "weight", "fast", "workout", "water"}
MAX_ENTRY = 64 * 1024
MAX_SETTINGS = 512 * 1024
MAX_BODY = 8 * 1024 * 1024
MAX_APK = 200 * 1024 * 1024
LINK_TTL = 15 * 60
HANDOFF_TTL = 10 * 60

# Update channels: the app sends ?app=<channel>. Old Health Tracker builds send nothing and get the
# default channel, which is how they receive the build that moves them to FossFitness.
CHANNELS = {
    "healthtracker": ("apk", "HealthTracker.apk"),
    "fossfitness": ("apk-fossfitness", "FossFitness.apk"),
}
DEFAULT_CHANNEL = "healthtracker"


def channel_of(name):
    name = str(name or "").strip().lower()
    return name if name in CHANNELS else DEFAULT_CHANNEL


def fossfitness_code(tag):
    """FossFitness tags are vA.B.C and the Android versionCode is A*1000 + B*100 + C (2.1.0 -> 2100)."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", str(tag or ""))
    if not m or int(m.group(2)) > 9 or int(m.group(3)) > 99:
        return None, None
    a, b, c = (int(x) for x in m.groups())
    return "%d.%d.%d" % (a, b, c), a * 1000 + b * 100 + c
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
  profile_client_at INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS shared_foods (
  id TEXT PRIMARY KEY,
  data TEXT NOT NULL,
  added_by INTEGER,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  created_by INTEGER,
  updated_by INTEGER,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'msg',
  body TEXT NOT NULL,
  ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
  code TEXT PRIMARY KEY,
  created_by INTEGER REFERENCES users(id) ON DELETE CASCADE,
  created INTEGER NOT NULL,
  expires INTEGER NOT NULL,
  used_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  used_at INTEGER
);
"""

INVITE_TTL = 7 * 24 * 3600 * 1000      # one-time invite codes last a week
INVITE_MAX_OPEN = 10                   # unused codes one person can have at a time
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I


def new_invite_code():
    raw = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def clean_invite(code):
    """'abcd efgh', 'ABCD-EFGH' -> 'ABCD-EFGH' (None if it can't be one of ours)."""
    raw = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
    return raw[:4] + "-" + raw[4:] if len(raw) == 8 else None


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

    def __init__(self, data_dir, invite_code, upload_token=""):
        self.upload_token = upload_token or ""
        self._links = {}  # one-off download links: token -> (expiry time, channel)
        self._handoffs = {}  # one-time codes that move a sign-in to FossFitness: code -> (expiry, user id)
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
            if "profile_client_at" not in {r["name"] for r in conn.execute("PRAGMA table_info(users)")}:
                # When the person's phone last sent settings (as opposed to the server updating them).
                conn.execute("ALTER TABLE users ADD COLUMN profile_client_at INTEGER NOT NULL DEFAULT 0")
            tcols = {r["name"] for r in conn.execute("PRAGMA table_info(tokens)")}
            if "app" not in tcols:
                # Which app (and build) each signed-in device runs, to follow the move to FossFitness.
                conn.execute("ALTER TABLE tokens ADD COLUMN app TEXT")
                conn.execute("ALTER TABLE tokens ADD COLUMN app_version INTEGER")
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(shared_foods)")}
            if "merged_into" not in cols:
                conn.execute("ALTER TABLE shared_foods ADD COLUMN merged_into TEXT")
            # Pick up custom foods that were made before sharing existed.
            for u in conn.execute("SELECT id FROM users").fetchall():
                self._absorb_foods(conn, u["id"])
            self._spread_foods(conn)

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
        invite = str(body.get("invite") or "").strip()
        master = hmac.compare_digest(invite.encode(), self.invite_code.encode())
        one_time = None if master else clean_invite(invite)
        if not master and not (one_time and self._invite_open(one_time)):
            self._note_failure("register")
            raise ApiError(403, "That invite code is not right, or it has been used or has expired.")
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
            if one_time:
                used = conn.execute(
                    "UPDATE invites SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL AND expires>?",
                    (cur.lastrowid, now_ms(), one_time, now_ms()),
                ).rowcount
                if not used:  # taken by someone else a moment ago
                    raise ApiError(403, "That invite code has just been used.")
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

            # Settings: newest copy wins.
            prof = body.get("profile")
            client_at = 0
            if isinstance(prof, dict) and isinstance(prof.get("data"), dict):
                try:
                    client_at = int(prof.get("updatedAt") or 0)
                except (TypeError, ValueError):
                    client_at = 0
                blob = json.dumps(prof["data"], separators=(",", ":"))
                if len(blob) > MAX_SETTINGS:
                    raise ApiError(400, "Settings too large.")
                # Newest copy from any of this person's phones wins. Updates the server made itself
                # (handing out shared foods) don't count, so they can't make a phone's own change look old.
                if client_at > user["profile_client_at"]:
                    conn.execute("UPDATE users SET profile=?, profile_client_at=?, profile_updated=MAX(profile_updated, ?)"
                                 " WHERE id=?", (blob, client_at, client_at, uid))
                # Foods this phone created are kept even if its copy of the settings lost.
                self._absorb_foods(conn, uid, prof["data"].get("customFoods"))
            # Custom foods are shared: take in this phone's new ones, hand everyone the full list.
            self._absorb_foods(conn, uid)
            self._spread_foods(conn)
            row = conn.execute("SELECT profile, profile_updated FROM users WHERE id=?", (uid,)).fetchone()
            profile_out = None
            if row["profile_updated"] and row["profile_updated"] > client_at:
                profile_out = {"data": json.loads(row["profile"]), "updatedAt": row["profile_updated"]}

            stats = body.get("stats")
            if isinstance(stats, dict):
                blob = json.dumps(stats, separators=(",", ":"))
                if len(blob) > 8000:
                    raise ApiError(400, "Stats too large.")
                conn.execute("UPDATE users SET stats=?, stats_updated=? WHERE id=?",
                             (blob, now_ms(), uid))
            conn.execute("UPDATE tokens SET last_used=? WHERE token_hash=?",
                         (now_ms(), token_hash(token)))
            client = body.get("client")
            if isinstance(client, dict):
                try:
                    app_version = int(client.get("version") or 0)
                except (TypeError, ValueError):
                    app_version = 0
                conn.execute("UPDATE tokens SET app=?, app_version=? WHERE token_hash=?",
                             (str(client.get("app") or "")[:80], app_version, token_hash(token)))
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

    # ── one-time invite codes ─────────────────────────────────────────────
    def _invite_open(self, code):
        conn = self.db()
        try:
            return bool(conn.execute("SELECT 1 FROM invites WHERE code=? AND used_by IS NULL AND expires>?", (code, now_ms())).fetchone())
        finally:
            conn.close()

    def _invite_rows(self, conn, me):
        rows = conn.execute(
            "SELECT i.*, u.display_name used_name FROM invites i LEFT JOIN users u ON u.id=i.used_by"
            " WHERE i.created_by=? AND (i.used_by IS NOT NULL OR i.expires>?) ORDER BY i.created DESC LIMIT 30",
            (me["id"], now_ms()),
        )
        return [{"code": r["code"], "created": r["created"], "expires": r["expires"],
                 "usedBy": r["used_name"] if r["used_by"] else None, "usedAt": r["used_at"]} for r in rows]

    def invites(self, token):
        """My invite codes: open ones and the ones that were used."""
        conn = self.db()
        try:
            return {"invites": self._invite_rows(conn, self.authenticate(conn, token))}
        finally:
            conn.close()

    def create_invite(self, token):
        """A one-time code for someone new: works once, for a week, to sign up and to download the app."""
        conn = self.db()
        try:
            me = self.authenticate(conn, token)
            conn.execute("BEGIN IMMEDIATE")
            open_count = conn.execute(
                "SELECT COUNT(*) c FROM invites WHERE created_by=? AND used_by IS NULL AND expires>?", (me["id"], now_ms())
            ).fetchone()["c"]
            if open_count >= INVITE_MAX_OPEN:
                raise ApiError(429, "You already have %d unused invite codes. Share or cancel one of those first." % INVITE_MAX_OPEN)
            t = now_ms()
            for _ in range(5):
                code = new_invite_code()
                if not conn.execute("SELECT 1 FROM invites WHERE code=?", (code,)).fetchone():
                    break
            conn.execute("INSERT INTO invites (code, created_by, created, expires) VALUES (?,?,?,?)", (code, me["id"], t, t + INVITE_TTL))
            conn.execute("COMMIT")
            return {"code": code, "expires": t + INVITE_TTL, "invites": self._invite_rows(conn, me)}
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def cancel_invite(self, token, body):
        conn = self.db()
        try:
            me = self.authenticate(conn, token)
            code = clean_invite(body.get("code"))
            conn.execute("DELETE FROM invites WHERE code=? AND created_by=? AND used_by IS NULL", (code, me["id"]))
            conn.commit()
            return {"invites": self._invite_rows(conn, me)}
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
    # beside it: {"versionCode": 2, "versionName": "2.0.0"}. FossFitness: <HT_DATA>/apk-fossfitness/.
    def _apk_folder(self, channel):
        folder, name = CHANNELS[channel_of(channel)]
        return os.path.join(os.path.dirname(self.db_path), folder), name

    def apk_info(self, channel=DEFAULT_CHANNEL):
        folder, name = self._apk_folder(channel)
        apk = os.path.join(folder, name)
        info = {"available": os.path.isfile(apk), "versionCode": 0, "versionName": ""}
        try:
            with open(os.path.join(folder, "version.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
            info["versionCode"] = int(meta.get("versionCode") or 0)
            info["versionName"] = str(meta.get("versionName") or "")[:20]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return apk, info

    def apk_for_code(self, code, username="", password=""):
        """Path to the APK for the invite code, or for an account's username and password."""
        if self._throttled("apk"):
            raise ApiError(429, "Too many attempts. Try again in a few minutes.")
        ok = False
        if str(username or "").strip():
            conn = self.db()
            try:
                row = conn.execute("SELECT * FROM users WHERE username=?", (str(username).strip().lower(),)).fetchone()
            finally:
                conn.close()
            candidate = hash_password(str(password or ""), row["pw_salt"] if row else b"0" * 16)
            ok = bool(row) and hmac.compare_digest(candidate, row["pw_hash"])
            message = "Wrong username or password."
        else:
            ok = hmac.compare_digest(str(code or "").strip().encode(), self.invite_code.encode())
            if not ok:
                one_time = clean_invite(code)
                ok = bool(one_time) and self._invite_open(one_time)
            message = "That invite code is not right, or it has been used or has expired."
        if not ok:
            self._note_failure("apk")
            raise ApiError(403, message)
        return self._apk_or_404()

    def _apk_or_404(self, channel=DEFAULT_CHANNEL):
        apk, info = self.apk_info(channel)
        if not info["available"]:
            raise ApiError(404, "No app build has been uploaded yet.")
        return apk, info

    def apk_link(self, token, channel=DEFAULT_CHANNEL):
        """A short-lived download link for a signed-in app (a browser can't send our sign-in header)."""
        channel = channel_of(channel)
        conn = self.db()
        try:
            self.authenticate(conn, token)
        finally:
            conn.close()
        _, info = self._apk_or_404(channel)
        t = time.time()
        with self._fail_lock:
            self._links = {k: v for k, v in self._links.items() if v[0] > t}
            link = secrets.token_urlsafe(24)
            self._links[link] = (t + LINK_TTL, channel)
        return {"url": "/dl/" + link, "versionCode": info["versionCode"], "versionName": info["versionName"]}

    def apk_for_link(self, link):
        with self._fail_lock:
            expiry, channel = self._links.get(link) or (0, DEFAULT_CHANNEL)
        if expiry < time.time():
            raise ApiError(404, "This download link has expired. Open the update from the app again.")
        return self._apk_or_404(channel)

    # ── moving a signed-in device from Health Tracker to FossFitness ──────
    def handoff_start(self, token):
        """Signed-in old app: a one-time code the new app on the same phone trades for its own sign-in."""
        conn = self.db()
        try:
            user = self.authenticate(conn, token)
        finally:
            conn.close()
        t = time.time()
        with self._fail_lock:
            self._handoffs = {k: v for k, v in self._handoffs.items() if v[0] > t}
            code = secrets.token_urlsafe(24)
            self._handoffs[code] = (t + HANDOFF_TTL, user["id"])
        return {"code": code, "expiresIn": HANDOFF_TTL}

    def handoff_claim(self, body):
        if self._throttled("handoff"):
            raise ApiError(429, "Too many attempts. Try again in a few minutes.")
        code = str(body.get("code") or "")
        with self._fail_lock:
            expiry, user_id = self._handoffs.pop(code, (0, None))
        if expiry < time.time():
            self._note_failure("handoff")
            raise ApiError(404, "This move has expired. Start it again from the old app.")
        conn = self.db()
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise ApiError(404, "That account no longer exists.")
            token = self._issue_token(conn, row["id"], body.get("device"))
            return {"token": token, "user": self._user_public(row)}
        finally:
            conn.close()

    def store_apk(self, auth_header, stream, length, version_code, version_name):
        """Called by the build (GitHub Actions): replace the APK the app offers as an update."""
        given = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        if not self.upload_token or not hmac.compare_digest(given.encode(), self.upload_token.encode()):
            raise ApiError(403, "Not allowed.")
        return self._install_apk(stream, length, version_code, version_name)

    def _install_apk(self, stream, length, version_code, version_name, channel=DEFAULT_CHANNEL):
        try:
            code = int(version_code)
        except (TypeError, ValueError):
            raise ApiError(400, "versionCode must be a number.")
        if code <= 0 or not re.match(r"^[0-9A-Za-z.\-]{1,20}$", str(version_name or "")):
            raise ApiError(400, "Bad version.")
        if not 1000 < length <= MAX_APK:
            raise ApiError(400, "That is not an APK.")
        folder, name = self._apk_folder(channel)
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, "upload.tmp")
        left = length
        with open(tmp, "wb") as fh:
            first = True
            while left:
                chunk = stream.read(min(left, 1024 * 1024))
                if not chunk:
                    break
                if first and not chunk.startswith(b"PK"):
                    fh.close()
                    os.remove(tmp)
                    raise ApiError(400, "That is not an APK.")
                first = False
                fh.write(chunk)
                left -= len(chunk)
        if left:
            os.remove(tmp)
            raise ApiError(400, "Upload was cut short.")
        _, current = self.apk_info(channel)
        if current["available"] and code < current["versionCode"]:
            os.remove(tmp)
            raise ApiError(409, "The server already has a newer version (%d)." % current["versionCode"])
        os.replace(tmp, os.path.join(folder, name))
        with open(os.path.join(folder, "version.json.tmp"), "w", encoding="utf-8") as fh:
            json.dump({"versionCode": code, "versionName": str(version_name)}, fh)
        os.replace(os.path.join(folder, "version.json.tmp"), os.path.join(folder, "version.json"))
        return {"ok": True, "versionCode": code, "versionName": str(version_name)}
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

    # ── custom foods: one list shared by everyone ─────────────────────────
    # The app keeps custom foods in the settings it syncs (users.profile). The
    # server keeps one shared list (shared_foods): any food a phone uploads that
    # the list has never seen is added, and every person's settings are given
    # the whole list, marked newer, so each phone pulls it on its next sync.
    # The app cannot edit or delete custom foods, so the list is the authority:
    # edits and deletions happen on the web page and apply to everyone.
    FOOD_NUMS = ("kcal", "protein", "carbs", "fat", "fiber")

    @staticmethod
    def _settings(row):
        try:
            s = json.loads(row["profile"] or "{}")
        except ValueError:
            s = {}
        return s if isinstance(s, dict) else {}

    @staticmethod
    def _clean_food(f, fid=None):
        def n(v):
            x = round(max(0.0, min(_num(v), 20000.0)), 1)
            return int(x) if x == int(x) else x
        out = {"id": str(fid or f.get("id"))[:80], "name": str(f.get("name") or "").strip()[:120],
               "servingLabel": str(f.get("servingLabel") or "").strip()[:60] or "1 serving",
               **{k: n(f.get(k)) for k in Hub.FOOD_NUMS}}
        # Optional micronutrients per serving (e.g. read off a label): short names → numbers.
        micro = f.get("micro")
        if isinstance(micro, dict):
            clean = {str(k)[:20]: n(v) for k, v in list(micro.items())[:40] if re.match(r"^[A-Za-z0-9]{1,20}$", str(k))}
            if clean:
                out["micro"] = clean
        return out

    def _absorb_foods(self, conn, uid, foods=None):
        if foods is None:
            row = conn.execute("SELECT profile FROM users WHERE id=?", (uid,)).fetchone()
            foods = self._settings(row).get("customFoods") if row else None
        t = now_ms()
        for f in foods if isinstance(foods, list) else []:
            if not isinstance(f, dict) or not f.get("id") or not str(f.get("name") or "").strip():
                continue
            conn.execute("INSERT OR IGNORE INTO shared_foods (id, data, added_by, created, updated) VALUES (?,?,?,?,?)",
                         (str(f["id"])[:80], json.dumps(self._clean_food(f), separators=(",", ":")), uid, t, t))

    @staticmethod
    def _food_key(f):
        norm = lambda v: re.sub(r"\s+", " ", str(v or "")).strip().lower()
        return (norm(f.get("name")), norm(f.get("servingLabel")), round(_num(f.get("kcal"))), round(_num(f.get("protein"))))

    def _dedupe_foods(self, conn):
        """Identical foods (same name, serving, calories, protein) become one: the oldest is kept."""
        keep = {}
        for r in conn.execute("SELECT id, data FROM shared_foods WHERE deleted=0 ORDER BY created, id").fetchall():
            k = self._food_key(json.loads(r["data"]))
            if k in keep:
                conn.execute("UPDATE shared_foods SET deleted=1, merged_into=?, updated=? WHERE id=?",
                             (keep[k], now_ms(), r["id"]))
            else:
                keep[k] = r["id"]

    def _pool(self, conn):
        # "by": who added the food (user id), so each phone can tell its own foods from the group's.
        rows = conn.execute("SELECT data, added_by FROM shared_foods WHERE deleted=0 ORDER BY created, id").fetchall()
        return [{**json.loads(r["data"]), "by": r["added_by"]} for r in rows]

    def _spread_foods(self, conn):
        """Give every person the full shared list; returns how many settings changed."""
        self._dedupe_foods(conn)
        pool = self._pool(conn)
        gone, moved = set(), {}
        for r in conn.execute("SELECT id, merged_into FROM shared_foods WHERE deleted=1"):
            if r["merged_into"]:
                moved[r["id"]] = r["merged_into"]
            else:
                gone.add(r["id"])
        changed = 0
        for u in conn.execute("SELECT id, profile, profile_updated FROM users").fetchall():
            settings = self._settings(u)
            want = dict(settings)
            want["customFoods"] = pool
            if gone or moved:
                # Deleted foods drop out of favourites and recents; merged ones point at the kept copy.
                if isinstance(want.get("favorites"), list):
                    favs = []
                    for x in want["favorites"]:
                        x = moved.get(x, x)
                        if x not in gone and x not in favs:
                            favs.append(x)
                    want["favorites"] = favs
                if isinstance(want.get("recentFoods"), list):
                    recent, seen = [], set()
                    for r in want["recentFoods"]:
                        if isinstance(r, dict) and r.get("foodId") in moved:
                            r = {**r, "foodId": moved[r["foodId"]]}
                        fid = r.get("foodId") if isinstance(r, dict) else None
                        if fid in gone or (fid is not None and fid in seen):
                            continue
                        seen.add(fid)
                        recent.append(r)
                    want["recentFoods"] = recent
            if not pool and "customFoods" not in settings:
                del want["customFoods"]  # nothing to share yet: leave untouched settings alone
            if want == settings:
                continue
            conn.execute("UPDATE users SET profile=?, profile_updated=? WHERE id=?",
                         (json.dumps(want, separators=(",", ":")), max(now_ms(), u["profile_updated"] + 1), u["id"]))
            changed += 1
        return changed

    def custom_foods(self, token):
        conn = self.db()
        try:
            self.authenticate(conn, token)
            names = {r["id"]: r["display_name"] for r in conn.execute("SELECT id, display_name FROM users")}
            rows = conn.execute("SELECT data, added_by FROM shared_foods WHERE deleted=0 ORDER BY created, id").fetchall()
            return {"foods": [{**json.loads(r["data"]), "addedBy": names.get(r["added_by"], "")} for r in rows]}
        finally:
            conn.close()

    def _edit_pool(self, token, fn):
        conn = self.db()
        try:
            me = self.authenticate(conn, token)
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn, me)
            self._spread_foods(conn)
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def save_custom_food(self, token, body):
        f = body.get("food") if isinstance(body.get("food"), dict) else {}
        if not str(f.get("name") or "").strip():
            raise ApiError(400, "Give the food a name.")
        for k in self.FOOD_NUMS:
            if not 0 <= _num(f.get(k)) <= 20000:
                raise ApiError(400, "Numbers must be between 0 and 20,000.")
        fid = str(f.get("id") or "")

        def apply(conn, me):
            t = now_ms()
            if fid:
                if not conn.execute("SELECT 1 FROM shared_foods WHERE id=? AND deleted=0", (fid,)).fetchone():
                    raise ApiError(404, "That food no longer exists.")
                food = self._clean_food(f, fid)
                conn.execute("UPDATE shared_foods SET data=?, updated=? WHERE id=?",
                             (json.dumps(food, separators=(",", ":")), t, fid))
            else:
                food = self._clean_food(f, "my-" + secrets.token_hex(6))
                conn.execute("INSERT INTO shared_foods (id, data, added_by, created, updated) VALUES (?,?,?,?,?)",
                             (food["id"], json.dumps(food, separators=(",", ":")), me["id"], t, t))
            return {"food": food}
        return self._edit_pool(token, apply)

    def delete_custom_food(self, token, body):
        fid = str(body.get("id") or "")

        def apply(conn, me):
            cur = conn.execute("UPDATE shared_foods SET deleted=1, updated=? WHERE id=? AND deleted=0", (now_ms(), fid))
            if not cur.rowcount:
                raise ApiError(404, "That food no longer exists.")
            return {"ok": True}
        return self._edit_pool(token, apply)

    # ── shared notes (everyone can read and edit) ─────────────────────────
    def notes(self, token):
        conn = self.db()
        try:
            self.authenticate(conn, token)
            rows = conn.execute(
                "SELECT n.*, a.display_name AS by_name, b.display_name AS edit_name FROM notes n"
                " LEFT JOIN users a ON a.id = n.created_by LEFT JOIN users b ON b.id = n.updated_by"
                " WHERE n.deleted=0 ORDER BY n.updated DESC").fetchall()
            return {"notes": [{"id": r["id"], "title": r["title"], "body": r["body"], "createdBy": r["by_name"] or "",
                               "updatedBy": r["edit_name"] or "", "created": r["created"], "updated": r["updated"]}
                              for r in rows]}
        finally:
            conn.close()

    def save_note(self, token, body):
        title = str(body.get("title") or "").strip()[:120]
        text = str(body.get("body") or "")[:20000]
        if not title and not text.strip():
            raise ApiError(400, "The note is empty.")
        conn = self.db()
        try:
            me = self.authenticate(conn, token)
            t = now_ms()
            nid = body.get("id")
            if nid:
                cur = conn.execute("UPDATE notes SET title=?, body=?, updated_by=?, updated=? WHERE id=? AND deleted=0",
                                   (title or "Untitled", text, me["id"], t, int(nid)))
                if not cur.rowcount:
                    raise ApiError(404, "That note was deleted.")
            else:
                nid = conn.execute("INSERT INTO notes (title, body, created_by, updated_by, created, updated) VALUES (?,?,?,?,?,?)",
                                   (title or "Untitled", text, me["id"], me["id"], t, t)).lastrowid
            return {"id": int(nid), "updated": t}
        except (TypeError, ValueError):
            raise ApiError(400, "Bad note id.")
        finally:
            conn.close()

    def delete_note(self, token, body):
        conn = self.db()
        try:
            self.authenticate(conn, token)
            cur = conn.execute("UPDATE notes SET deleted=1, updated=? WHERE id=? AND deleted=0", (now_ms(), int(body.get("id") or 0)))
            if not cur.rowcount:
                raise ApiError(404, "That note was already deleted.")
            return {"ok": True}
        except (TypeError, ValueError):
            raise ApiError(400, "Bad note id.")
        finally:
            conn.close()

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


# ── pulling new builds from GitHub releases ────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # handled by hand so the GitHub token is not sent on to the download host


def check_github_release(hub, repo, token, opener=None, api="https://api.github.com", channel=DEFAULT_CHANNEL):
    """Offer the newest GitHub release's APK if it is newer than what we have.
    Health Tracker tags look like v2.0.<build>, where <build> is the Android versionCode;
    FossFitness tags are vA.B.C (see fossfitness_code)."""
    channel = channel_of(channel)
    asset_name = CHANNELS[channel][1]
    opener = opener or urllib.request.build_opener(_NoRedirect)
    head = {"User-Agent": "HealthHub", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        head["Authorization"] = "Bearer " + token
    req = urllib.request.Request("%s/repos/%s/releases/latest" % (api, repo), headers={**head, "Accept": "application/vnd.github+json"})
    with opener.open(req, timeout=30) as r:
        rel = json.load(r)
    tag = str(rel.get("tag_name") or "")
    if channel == "fossfitness":
        name, code = fossfitness_code(tag)
    else:
        m = re.match(r"^v?((?:\d+\.)*?(\d+))$", tag)
        name, code = (m.group(1), int(m.group(2))) if m else (None, None)
    if code is None:
        return "latest release tag %r is not a version" % rel.get("tag_name")
    _, current = hub.apk_info(channel)
    if current["available"] and code <= current["versionCode"]:
        return "up to date (%s)" % current["versionName"]
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == asset_name), None)
    if not asset:
        return "release %s has no %s" % (name, asset_name)
    req = urllib.request.Request(asset["url"], headers={**head, "Accept": "application/octet-stream"})
    try:
        resp = opener.open(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        resp = urllib.request.urlopen(urllib.request.Request(e.headers["Location"], headers={"User-Agent": "HealthHub"}), timeout=300)
    with resp:
        length = int(resp.headers.get("Content-Length") or asset.get("size") or 0)
        hub._install_apk(resp, length, code, name, channel)
    return "now offering %s" % name


def github_release_loop(hub, repo, token, every=900, channel=DEFAULT_CHANNEL):
    time.sleep(20)
    while True:
        try:
            print("healthhub: %s release check:" % channel, check_github_release(hub, repo, token, channel=channel), flush=True)
        except Exception as e:  # keep going; GitHub or the network may be down for a while
            print("healthhub: github release check failed:", repr(e), file=sys.stderr, flush=True)
        time.sleep(every)


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
    if _num(p.get("calorieGoal")) > 0:  # the person's own target
        return _jsround(_num(p["calorieGoal"]))
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
    """None means that goal is switched off for this person."""
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
    if profile.get("waterTracking") is False:
        hits["water"] = None
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
            base = "FossFitness" if os.path.basename(path) == "FossFitness.apk" else "HealthTracker"
            name = "%s-%s.apk" % (base, re.sub(r"[^0-9A-Za-z.]", "", info["versionName"]) or "latest")
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
                if path.startswith("/dl/"):
                    apk, info = hub.apk_for_link(path[4:])
                    return self._send_apk(apk, info)
                if path == "/get":
                    return self._static("get.html")
                if path == "/healthz":
                    return self._send(200, {"ok": True, "version": VERSION})
                if path == "/api/app":
                    return self._send(200, hub.apk_info(q.get("app", [""])[0])[1])
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
                if path == "/api/notes":
                    return self._send(200, hub.notes(tok))
                if path == "/api/invites":
                    return self._send(200, hub.invites(tok))
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
                    field = lambda k: form.get(k, [""])[0]
                    try:
                        apk, info = hub.apk_for_code(field("code"), field("username"), field("password"))
                    except ApiError as e:  # a browser form: answer with a page, not JSON
                        page = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
                                'initial-scale=1"><title>Health Tracker</title><body style="font:16px system-ui;padding:24px">'
                                '<p>%s</p><p><a href="/get">Try again</a></p>' % e.message.replace("<", "&lt;"))
                        return self._send(e.status, page.encode(), "text/html; charset=utf-8")
                    return self._send_apk(apk, info)
                if path == "/api/app/upload":
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        length = 0
                    return self._send(200, hub.store_apk(self.headers.get("Authorization") or "", self.rfile, length,
                                                         q.get("versionCode", [""])[0], q.get("versionName", [""])[0]))
                if path == "/api/app/link":
                    try:
                        link_body = json.loads(self._raw_body() or b"{}")
                    except ValueError:
                        link_body = {}
                    app = link_body.get("app") if isinstance(link_body, dict) else None
                    return self._send(200, hub.apk_link(tok, app or q.get("app", [""])[0]))
                body = self._body()
                if path == "/api/handoff":
                    return self._send(200, hub.handoff_start(tok))
                if path == "/api/handoff/claim":
                    return self._send(200, hub.handoff_claim(body))
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
                if path == "/api/notes/save":
                    return self._send(200, hub.save_note(tok, body))
                if path == "/api/notes/delete":
                    return self._send(200, hub.delete_note(tok, body))
                if path == "/api/customfoods/save":
                    return self._send(200, hub.save_custom_food(tok, body))
                if path == "/api/customfoods/delete":
                    return self._send(200, hub.delete_custom_food(tok, body))
                if path == "/api/invites":
                    return self._send(200, hub.create_invite(tok))
                if path == "/api/invites/cancel":
                    return self._send(200, hub.cancel_invite(tok, body))
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


def make_server(data_dir, invite_code, bind="127.0.0.1", port=0, upload_token=""):
    hub = Hub(data_dir, invite_code, upload_token)
    httpd = ThreadingHTTPServer((bind, port), make_handler(hub))
    httpd.daemon_threads = True
    httpd.hub = hub
    return httpd


def main():
    invite = os.environ.get("HT_INVITE_CODE", "")
    if not invite:
        sys.exit("HT_INVITE_CODE must be set (the code people need to create an account).")
    httpd = make_server(os.environ.get("HT_DATA", "./data"), invite,
                        os.environ.get("HT_BIND", "0.0.0.0"), int(os.environ.get("HT_PORT", "8787")),
                        os.environ.get("HT_UPLOAD_TOKEN", ""))
    print("HealthHub %s listening on %s:%s" % (VERSION, *httpd.server_address), flush=True)
    if os.environ.get("HT_GITHUB_TOKEN"):
        threading.Thread(target=github_release_loop, daemon=True,
                         args=(httpd.hub, os.environ.get("HT_GITHUB_REPO") or "merlinlcb/HealthTracker",
                               os.environ["HT_GITHUB_TOKEN"])).start()
    if os.environ.get("HT_FOSSFITNESS_REPO"):
        threading.Thread(target=github_release_loop, daemon=True,
                         args=(httpd.hub, os.environ["HT_FOSSFITNESS_REPO"], os.environ.get("HT_GITHUB_TOKEN", "")),
                         kwargs={"channel": "fossfitness"}).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
