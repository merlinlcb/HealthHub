"""Tests for the HealthHub server. Run: python3 -m unittest server/test_healthhub.py"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import healthhub  # noqa: E402


def entry(eid, kind="weight", ts=1000, day="2026-11-01", data=None, updated=None, deleted=False):
    return {"id": eid, "kind": kind, "ts": ts, "day": day, "data": data or {"weight": 180},
            "updatedAt": updated or ts, "deleted": deleted}


class HubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = healthhub.make_server(self.tmp.name, "letmein")
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, token=None, expect=200):
        req = urllib.request.Request(self.base + path, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=data) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        self.assertEqual(status, expect, raw)
        return json.loads(raw) if raw[:1] in (b"{", b"[") else raw

    def signup(self, name, pw="password1"):
        r = self.call("POST", "/api/register",
                      {"invite": "letmein", "username": name, "displayName": name.title(), "password": pw})
        return r["token"], r["user"]

    def test_register_needs_invite_and_first_user_is_admin(self):
        self.call("POST", "/api/register",
                  {"invite": "nope", "username": "x1", "password": "password1"}, expect=403)
        _, merlin = self.signup("merlin")
        _, bro = self.signup("bro")
        self.assertTrue(merlin["isAdmin"])
        self.assertFalse(bro["isAdmin"])
        self.call("POST", "/api/register",
                  {"invite": "letmein", "username": "merlin", "password": "password1"}, expect=409)
        self.call("POST", "/api/register",
                  {"invite": "letmein", "username": "ab", "password": "short"}, expect=400)

    def test_login_logout_and_throttle(self):
        self.signup("merlin")
        tok = self.call("POST", "/api/login", {"username": "Merlin", "password": "password1"})["token"]
        self.assertEqual(self.call("GET", "/api/me", token=tok)["user"]["username"], "merlin")
        self.call("POST", "/api/logout", {}, token=tok)
        self.call("GET", "/api/me", token=tok, expect=401)
        for _ in range(8):
            self.call("POST", "/api/login", {"username": "merlin", "password": "wrong"}, expect=401)
        self.call("POST", "/api/login", {"username": "merlin", "password": "password1"}, expect=429)

    def test_everything_needs_a_token(self):
        for path in ("/api/me", "/api/friends", "/api/chat", "/api/dashboard/users",
                     "/api/dashboard/data?user=1"):
            self.call("GET", path, expect=401)
        self.call("POST", "/api/sync", {}, expect=401)
        self.call("POST", "/api/chat", {"body": "hi"}, expect=401)

    def test_sync_push_pull_between_two_devices(self):
        tok, _ = self.signup("merlin")
        tok2 = self.call("POST", "/api/login", {"username": "merlin", "password": "password1"})["token"]
        r = self.call("POST", "/api/sync", {"since": 0, "push": [entry("a"), entry("b", ts=2000)]}, tok)
        self.assertEqual(r["entries"], [])
        self.assertEqual(r["accepted"], 2)
        rev1 = r["rev"]
        # The second device gets both.
        r2 = self.call("POST", "/api/sync", {"since": 0, "push": []}, tok2)
        self.assertEqual([e["id"] for e in r2["entries"]], ["a", "b"])
        self.assertEqual(r2["rev"], rev1)
        # Nothing new the next time.
        self.assertEqual(self.call("POST", "/api/sync", {"since": rev1}, tok2)["entries"], [])
        # Device two deletes "a"; device one sees the deletion.
        self.call("POST", "/api/sync", {"since": rev1, "push": [entry("a", updated=5000, deleted=True)]}, tok2)
        r3 = self.call("POST", "/api/sync", {"since": rev1}, tok)
        self.assertEqual([(e["id"], e["deleted"]) for e in r3["entries"]], [("a", True)])

    def test_sync_newest_edit_wins(self):
        tok, _ = self.signup("merlin")
        self.call("POST", "/api/sync", {"push": [entry("a", data={"weight": 180}, updated=5000)]}, tok)
        r = self.call("POST", "/api/sync", {"push": [entry("a", data={"weight": 999}, updated=4000)]}, tok)
        self.assertEqual(r["accepted"], 0)
        got = self.call("POST", "/api/sync", {"since": 0}, tok)["entries"]
        self.assertEqual(got[0]["data"], {"weight": 180})

    def test_sync_is_per_user(self):
        tok, _ = self.signup("merlin")
        tok_b, _ = self.signup("bro")
        self.call("POST", "/api/sync", {"push": [entry("a")]}, tok)
        self.assertEqual(self.call("POST", "/api/sync", {"since": 0}, tok_b)["entries"], [])
        # Same id for a different person is a different entry.
        self.call("POST", "/api/sync", {"push": [entry("a", data={"weight": 150})]}, tok_b)
        self.assertEqual(self.call("POST", "/api/sync", {"since": 0}, tok)["entries"][0]["data"], {"weight": 180})

    def test_sync_rejects_bad_entries(self):
        tok, _ = self.signup("merlin")
        for bad in (entry("a", kind="nonsense"), entry("bad id!"), entry("a", day="yesterday"),
                    {"id": "a", "kind": "weight", "day": "2026-11-01", "ts": "x"}):
            self.call("POST", "/api/sync", {"push": [bad]}, tok, expect=400)
        self.assertEqual(self.call("POST", "/api/sync", {"since": 0}, tok)["entries"], [])

    def test_profile_newest_wins_and_restores(self):
        tok, _ = self.signup("merlin")
        self.call("POST", "/api/sync", {"profile": {"data": {"weight": 180}, "updatedAt": 100}}, tok)
        # An older copy does not overwrite; the server sends its newer one back.
        r = self.call("POST", "/api/sync", {"profile": {"data": {"weight": 1}, "updatedAt": 50}}, tok)
        self.assertEqual(r["profile"], {"data": {"weight": 180}, "updatedAt": 100})
        # A fresh install sends no profile and gets the stored one.
        r = self.call("POST", "/api/sync", {}, tok)
        self.assertEqual(r["profile"]["data"], {"weight": 180})
        # Same timestamp: nothing to send back.
        r = self.call("POST", "/api/sync", {"profile": {"data": {"weight": 180}, "updatedAt": 100}}, tok)
        self.assertIsNone(r["profile"])

    def test_friends_show_each_others_stats(self):
        tok, _ = self.signup("merlin")
        tok_b, _ = self.signup("bro")
        self.call("POST", "/api/sync", {"stats": {"xp": 420, "level": 3}}, tok)
        people = self.call("GET", "/api/friends", token=tok_b)["people"]
        self.assertEqual([(p["displayName"], p["isMe"], p["stats"].get("xp")) for p in people],
                         [("Merlin", False, 420), ("Bro", True, None)])

    def test_chat(self):
        tok, _ = self.signup("merlin")
        tok_b, _ = self.signup("bro")
        self.call("POST", "/api/chat", {"body": "  "}, tok, expect=400)
        self.call("POST", "/api/chat", {"body": "x" * 2001}, tok, expect=400)
        first = self.call("POST", "/api/chat", {"body": "leg day?"}, tok)["id"]
        self.call("POST", "/api/chat", {"body": "Bro reached level 2", "kind": "event"}, tok_b)
        msgs = self.call("GET", "/api/chat", token=tok_b)["messages"]
        self.assertEqual([(m["name"], m["mine"], m["kind"], m["body"]) for m in msgs],
                         [("Merlin", False, "msg", "leg day?"), ("Bro", True, "event", "Bro reached level 2")])
        newer = self.call("GET", "/api/chat?since=%d" % first, token=tok)["messages"]
        self.assertEqual(len(newer), 1)

    def test_dashboard_admin_sees_everyone_others_see_themselves(self):
        tok, merlin = self.signup("merlin")
        tok_b, bro = self.signup("bro")
        self.call("POST", "/api/sync", {"push": [entry("a"), entry("gone", deleted=True)]}, tok_b)
        self.assertEqual(len(self.call("GET", "/api/dashboard/users", token=tok)["users"]), 2)
        self.assertEqual([u["username"] for u in self.call("GET", "/api/dashboard/users", token=tok_b)["users"]],
                         ["bro"])
        data = self.call("GET", "/api/dashboard/data?user=%d" % bro["id"], token=tok)
        self.assertEqual([e["id"] for e in data["entries"]], ["a"])  # deleted ones are left out
        self.call("GET", "/api/dashboard/data?user=%d" % merlin["id"], token=tok_b, expect=403)
        self.call("GET", "/api/dashboard/data?user=999", token=tok, expect=404)

    def test_change_password_signs_other_devices_out(self):
        tok, _ = self.signup("merlin")
        tok2 = self.call("POST", "/api/login", {"username": "merlin", "password": "password1"})["token"]
        self.call("POST", "/api/password", {"oldPassword": "bad", "newPassword": "password2"}, tok, expect=401)
        self.call("POST", "/api/password", {"oldPassword": "password1", "newPassword": "password2"}, tok)
        self.call("GET", "/api/me", token=tok)
        self.call("GET", "/api/me", token=tok2, expect=401)
        self.call("POST", "/api/login", {"username": "merlin", "password": "password2"})

    def post_form(self, path, fields):
        from urllib.parse import urlencode
        req = urllib.request.Request(self.base + path, data=urlencode(fields).encode(), method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_apk_download_needs_the_invite_code(self):
        self.assertEqual(self.call("GET", "/api/app"), {"available": False, "versionCode": 0, "versionName": ""})
        self.assertEqual(self.post_form("/download", {"code": "letmein"})[0], 404)  # nothing uploaded yet
        folder = os.path.join(self.tmp.name, "apk")
        os.makedirs(folder)
        payload = b"PK\x03\x04" + os.urandom(600000)
        with open(os.path.join(folder, "HealthTracker.apk"), "wb") as fh:
            fh.write(payload)
        with open(os.path.join(folder, "version.json"), "w") as fh:
            json.dump({"versionCode": 2, "versionName": "2.0.0"}, fh)
        self.assertEqual(self.call("GET", "/api/app"), {"available": True, "versionCode": 2, "versionName": "2.0.0"})
        self.assertEqual(self.post_form("/download", {"code": "wrong"})[0], 403)
        self.assertEqual(self.post_form("/download", {})[0], 403)
        status, headers, body = self.post_form("/download", {"code": " letmein "})
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertEqual(headers["Content-Type"], "application/vnd.android.package-archive")
        self.assertIn("HealthTracker-2.0.0.apk", headers["Content-Disposition"])
        self.assertIn(b"invite", self.call("GET", "/get").lower())
        # There is no way to fetch it without the code.
        for path in ("/apk/HealthTracker.apk", "/download", "/HealthTracker.apk"):
            self.call("GET", path, expect=404)
        for _ in range(8):
            self.post_form("/download", {"code": "guess"})
        self.assertEqual(self.post_form("/download", {"code": "letmein"})[0], 429)

    def test_bad_requests(self):
        self.call("GET", "/nope", expect=404)
        self.call("GET", "/healthz")
        req = urllib.request.Request(self.base + "/api/login", data=b"not json", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
