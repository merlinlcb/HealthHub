"""Tests for the HealthHub server. Run: python3 -m unittest server/test_healthhub.py"""
import json
import os
import sys
import tempfile
import threading
import time
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

    def test_goal_maths_match_the_app(self):
        # Reference numbers printed by app/src/lib/calc.js for the same profiles.
        ps = [
            {"sex": "male", "age": 34, "heightCm": 180, "weightKg": 110, "goal": "lose", "paceKgPerWeek": 0.5,
             "activityLevel": "light", "goalWeightKg": 90, "proteinGoal": None},
            {"sex": "female", "age": 29, "heightCm": 165, "weightKg": 70, "goal": "maintain", "paceKgPerWeek": 0.5,
             "activityLevel": "moderate", "proteinGoal": 0, "climate": "hot"},
            {"sex": "male", "age": 40, "heightCm": 175, "weightKg": 80, "goal": "gain", "paceKgPerWeek": 1,
             "activityLevel": "very", "proteinGoal": 170},
        ]
        got = [[healthhub.calorie_target(p), healthhub.protein_goal(p), healthhub.water_goal_ml(p, 0),
                healthhub.water_goal_ml(p, 45)] for p in ps]
        self.assertEqual(got, [[2283, 153, 3850, 4400], [2209, 83, 2950, 3500], [3431, 170, 2800, 3350]])
        self.assertEqual(healthhub.calorie_target({**ps[0], "calorieGoal": 2650}), 2650)
        self.assertEqual(healthhub.calorie_target({**ps[0], "calorieGoal": None}), 2283)

    def test_compare_shares_only_weights_and_goal_hits(self):
        tok, _ = self.signup("merlin")
        tok_b, _ = self.signup("bro")
        profile = {"sex": "male", "age": 34, "heightCm": 180, "weightKg": 100, "goal": "lose", "paceKgPerWeek": 0.5,
                   "activityLevel": "light", "goalWeightKg": 90, "proteinGoal": 150, "units": "imperial"}
        day = {"water": [{"ml": 4000}], "food": [{"kcal": 1500, "protein": 160, "name": "secret burrito"}],
               "exercise": [], "workout": {"done": True}}
        self.call("POST", "/api/sync", {
            "profile": {"data": {"profile": profile, "fasting": {"enabled": True}}, "updatedAt": 5},
            "stats": {"level": 7, "streak": 3},
            "push": [
                {"id": "day-2026-11-13", "kind": "day", "day": "2026-11-13", "ts": 1, "data": day, "updatedAt": 1},
                {"id": "day-2026-11-12", "kind": "day", "day": "2026-11-12", "ts": 1,
                 "data": {"water": [{"ml": 500}], "food": [{"kcal": 3000, "protein": 10}]}, "updatedAt": 1},
                {"id": "wt-a", "kind": "weight", "day": "2026-11-01", "ts": 1, "data": {"kg": 102}, "updatedAt": 1},
                {"id": "wt-b", "kind": "weight", "day": "2026-11-10", "ts": 2, "data": {"kg": 101}, "updatedAt": 1},
                {"id": "wt-c", "kind": "weight", "day": "2026-11-13", "ts": 3, "data": {"kg": 100}, "updatedAt": 1},
                {"id": "fast-1", "kind": "fast", "day": "2026-11-13", "ts": 1,
                 "data": {"start": "2026-11-12T20:00:00.000Z", "end": "2026-11-13T09:00:00.000Z"}, "updatedAt": 1},
            ]}, tok)
        self.call("GET", "/api/compare", expect=401)
        # The non-admin account can open it and sees both people.
        hub = self.httpd.hub
        r = hub.compare(tok_b, 7, today="2026-11-13")
        self.assertEqual(r["days"][0], "2026-11-13")
        self.assertEqual(len(r["days"]), 7)
        m = next(p for p in r["people"] if p["displayName"] == "Merlin")
        self.assertFalse(m["isMe"])
        self.assertEqual((m["currentKg"], m["startKg"], m["goalKg"], m["level"]), (100, 102, 90, 7))
        self.assertEqual(m["days"][0]["hits"],
                         {"water": True, "food": True, "protein": True, "move": True, "weigh": True, "fast": True})
        self.assertEqual(m["days"][1]["hits"],
                         {"water": False, "food": False, "protein": False, "move": False, "weigh": False, "fast": False})
        # Nothing but yes/no and weights leaves the server.
        raw = json.dumps(r)
        for leak in ("secret burrito", "1500", "4000", "160"):
            self.assertNotIn(leak, raw)
        b = next(p for p in r["people"] if p["displayName"] == "Bro")
        self.assertIsNone(b["currentKg"])
        # A shorter window starting after the first weigh-in uses the last weight before it.
        self.assertEqual(next(p for p in hub.compare(tok, 3, today="2026-11-13")["people"] if p["isMe"])["startKg"], 101)
        # Over HTTP too.
        self.assertEqual(len(self.call("GET", "/api/compare?days=14", token=tok_b)["days"]), 14)
        self.assertEqual(self.call("GET", "/api/compare?days=2&today=2026-11-13", token=tok_b)["days"], ["2026-11-13", "2026-11-12"])
        self.assertEqual(len(self.call("GET", "/api/compare?days=2&today=bogus", token=tok_b)["days"]), 2)

    def test_custom_foods_are_shared_automatically(self):
        tok, merlin = self.signup("merlin")
        tok_b, bro = self.signup("bro")
        m_settings = {"profile": {"name": "Merlin"}, "customFoods": [], "favorites": []}
        self.call("POST", "/api/sync", {"profile": {"data": m_settings, "updatedAt": 900}}, tok)
        chili = {"id": "my-abc", "name": "Chili", "servingLabel": "1 bowl", "kcal": 400, "protein": 30,
                 "carbs": 20, "fat": 15, "fiber": 5}
        b_settings = {"profile": {"name": "Bro"}, "customFoods": [chili],
                      "favorites": ["my-abc", "db-banana"], "recentFoods": [{"foodId": "my-abc", "qty": 1}]}
        # Bro's phone uploads a new custom food; his own list comes back unchanged (nothing new for him).
        r = self.call("POST", "/api/sync", {"profile": {"data": b_settings, "updatedAt": 1000}}, tok_b)
        self.assertIsNone(r["profile"])
        # Merlin's phone (settings unchanged since 900) gets Bro's food on its next sync.
        r = self.call("POST", "/api/sync", {"profile": {"data": m_settings, "updatedAt": 900}}, tok)
        self.assertEqual(r["profile"]["data"]["customFoods"], [chili])
        self.assertEqual(r["profile"]["data"]["profile"], {"name": "Merlin"})  # nothing else touched
        m_settings, m_at = r["profile"]["data"], r["profile"]["updatedAt"]
        # Merlin creates a food in his app; it reaches Bro.
        toast = {"id": "my-xyz", "name": "Toast", "servingLabel": "1 slice", "kcal": 90, "protein": 3,
                 "carbs": 15, "fat": 1, "fiber": 1}
        m_settings = {**m_settings, "customFoods": m_settings["customFoods"] + [toast]}
        self.assertIsNone(self.call("POST", "/api/sync", {"profile": {"data": m_settings, "updatedAt": m_at + 5}}, tok)["profile"])
        r = self.call("POST", "/api/sync", {"profile": {"data": b_settings, "updatedAt": 1000}}, tok_b)
        self.assertEqual([f["name"] for f in r["profile"]["data"]["customFoods"]], ["Chili", "Toast"])
        b_settings, b_at = r["profile"]["data"], r["profile"]["updatedAt"]
        # The web page shows one list with who added each food.
        self.call("GET", "/api/customfoods", expect=401)
        foods = self.call("GET", "/api/customfoods", token=tok)["foods"]
        self.assertEqual([(f["name"], f["addedBy"]) for f in foods], [("Chili", "Bro"), ("Toast", "Merlin")])
        # Editing on the web changes it for everyone, and an older copy from a phone can't undo it.
        self.call("POST", "/api/customfoods/save", {"food": {**chili, "name": "Chili (big)", "kcal": 520}}, tok)
        self.call("POST", "/api/sync", {"profile": {"data": {**b_settings, "customFoods": [chili, toast]},
                                                    "updatedAt": b_at + 1}}, tok_b)
        r = self.call("POST", "/api/sync", {"profile": {"data": m_settings, "updatedAt": m_at + 5}}, tok)
        self.assertEqual(r["profile"]["data"]["customFoods"][0]["name"], "Chili (big)")
        self.assertEqual(r["profile"]["data"]["customFoods"][0]["kcal"], 520)
        # Adding on the web reaches everyone; bad input is refused.
        self.call("POST", "/api/customfoods/save", {"food": {"name": "Pancakes", "kcal": 300}}, tok_b)
        self.call("POST", "/api/customfoods/save", {"food": {"name": ""}}, tok, expect=400)
        self.call("POST", "/api/customfoods/save", {"food": {"id": "nope", "name": "x"}}, tok, expect=404)
        # Deleting removes it for everyone (including favourites and recents), and it stays deleted
        # even when a phone that has not synced yet still has it.
        self.call("POST", "/api/customfoods/delete", {"id": "my-abc"}, tok)
        self.call("POST", "/api/customfoods/delete", {"id": "my-abc"}, tok, expect=404)
        r = self.call("POST", "/api/sync", {"profile": {"data": {**b_settings, "customFoods": [chili, toast]},
                                                        "updatedAt": b_at + 50}}, tok_b)
        got = r["profile"]["data"]
        self.assertEqual([f["name"] for f in got["customFoods"]], ["Toast", "Pancakes"])
        self.assertEqual(got["favorites"], ["db-banana"])
        self.assertEqual(got["recentFoods"], [])
        self.assertEqual([f["name"] for f in self.call("GET", "/api/customfoods", token=tok)["foods"]], ["Toast", "Pancakes"])

    def test_identical_foods_are_merged(self):
        tok, _ = self.signup("merlin")
        tok_b, _ = self.signup("bro")
        jerky = {"name": "Lil landjager", "servingLabel": "1 serving", "kcal": 120, "protein": 7, "carbs": 1, "fat": 10}
        self.call("POST", "/api/sync", {"profile": {"data": {"customFoods": [{**jerky, "id": "my-b1"}],
                  "favorites": ["my-b1"]}, "updatedAt": 100}}, tok_b)
        # The same food made separately on the other phone (different id, different spacing/case).
        self.call("POST", "/api/sync", {"profile": {"data": {"customFoods": [
            {**jerky, "id": "my-m1", "name": "lil  Landjager "}, {**jerky, "id": "my-m2", "name": "Other", "kcal": 5}],
            "favorites": ["my-m1", "db-banana"], "recentFoods": [{"foodId": "my-m1", "qty": 2}]}, "updatedAt": 200}}, tok)
        # Two identical web adds, too.
        for _ in range(2):
            self.call("POST", "/api/customfoods/save", {"food": {"name": "Winco Chicken Breast", "servingLabel": "56 g",
                                                                  "kcal": 50, "protein": 11}}, tok_b)
        names = [f["name"] for f in self.call("GET", "/api/customfoods", token=tok)["foods"]]
        self.assertEqual(names, ["Lil landjager", "Other", "Winco Chicken Breast"])
        got = self.call("POST", "/api/sync", {}, tok)["profile"]["data"]
        self.assertEqual([f["id"] for f in got["customFoods"]][0], "my-b1")
        self.assertEqual(got["favorites"], ["my-b1", "db-banana"])          # pointed at the kept copy
        self.assertEqual(got["recentFoods"], [{"foodId": "my-b1", "qty": 2}])
        # Different nutrition is a different food.
        self.call("POST", "/api/customfoods/save", {"food": {**jerky, "kcal": 130}}, tok)
        self.assertEqual(len(self.call("GET", "/api/customfoods", token=tok)["foods"]), 4)

    def test_a_phone_change_made_before_foods_were_handed_out_is_kept(self):
        tok, _ = self.signup("merlin")
        tok_b, _ = self.signup("bro")
        self.call("POST", "/api/sync", {"profile": {"data": {"profile": {"name": "Merlin"}, "customFoods": []},
                                                    "updatedAt": 100}}, tok)
        # Bro adds a food: the server hands it to Merlin, marking Merlin's settings newer (now).
        self.call("POST", "/api/sync", {"profile": {"data": {"customFoods": [
            {"id": "my-b1", "name": "Oats", "kcal": 300}]}, "updatedAt": 100}}, tok_b)
        # Merlin's phone, offline meanwhile, changed his name and made a food at time 200 (before "now").
        r = self.call("POST", "/api/sync", {"profile": {"data": {"profile": {"name": "Merlin L"}, "customFoods": [
            {"id": "my-m1", "name": "Toast", "kcal": 90}]}, "updatedAt": 200}}, tok)
        got = r["profile"]["data"]
        self.assertEqual(got["profile"], {"name": "Merlin L"})
        self.assertEqual(sorted(f["name"] for f in got["customFoods"]), ["Oats", "Toast"])
        self.assertGreater(r["profile"]["updatedAt"], 200)
        # A stale copy from another phone (older than the last one the server took) still loses.
        r = self.call("POST", "/api/sync", {"profile": {"data": {"profile": {"name": "old"}}, "updatedAt": 150}}, tok)
        self.assertEqual(r["profile"]["data"]["profile"], {"name": "Merlin L"})

    def test_foods_made_before_sharing_are_picked_up_at_start(self):
        tok, _ = self.signup("merlin")
        self.call("POST", "/api/sync", {"profile": {"data": {"customFoods": [
            {"id": "my-1", "name": "Old food", "kcal": 10}]}, "updatedAt": 5}}, tok)
        hub = healthhub.Hub(self.tmp.name, "letmein")  # a restart
        self.assertEqual([f["name"] for f in hub.custom_foods(tok)["foods"]], ["Old food"])

    def test_update_flow_without_invite_code(self):
        tok, _ = self.signup("merlin")
        apk = b"PK\x03\x04" + os.urandom(300000)
        # Nothing uploaded yet.
        self.call("POST", "/api/app/link", {}, tok, expect=404)
        # The build uploads with the upload token; anything else is refused.
        hub = self.httpd.hub
        hub.upload_token = "up-secret"
        def upload(data, code, name="2.0.%s", token="up-secret"):
            req = urllib.request.Request(self.base + "/api/app/upload?versionCode=%s&versionName=%s" % (code, name % code),
                                         data=data, method="POST", headers={"Authorization": "Bearer " + token})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        self.assertEqual(upload(apk, 14, token="wrong")[0], 403)
        self.assertEqual(upload(b"not an apk" * 200, 14)[0], 400)
        self.assertEqual(upload(apk, "x")[0], 400)
        self.assertEqual(upload(apk, 14), (200, {"ok": True, "versionCode": 14, "versionName": "2.0.14"}))
        self.assertEqual(upload(apk, 12)[0], 409)  # never go backwards by accident
        self.assertEqual(self.call("GET", "/api/app"), {"available": True, "versionCode": 14, "versionName": "2.0.14"})
        # The signed-in app gets a one-off link; it works without any header, for a while.
        self.call("POST", "/api/app/link", {}, expect=401)
        link = self.call("POST", "/api/app/link", {}, tok)
        self.assertEqual((link["versionCode"], link["versionName"]), (14, "2.0.14"))
        self.assertTrue(link["url"].startswith("/dl/"))
        with urllib.request.urlopen(self.base + link["url"]) as r:
            self.assertEqual(r.read(), apk)
            self.assertIn("HealthTracker-2.0.14.apk", r.headers["Content-Disposition"])
        self.call("GET", "/dl/made-up", expect=404)
        hub._links[link["url"][4:]] = time.time() - 1
        self.call("GET", link["url"], expect=404)
        # The /get page also takes an account instead of the invite code.
        status, _, body = self.post_form("/download", {"username": "Merlin", "password": "password1"})
        self.assertEqual((status, body), (200, apk))
        status, _, body = self.post_form("/download", {"username": "merlin", "password": "nope"})
        self.assertEqual(status, 403)
        self.assertIn(b"Wrong username or password", body)
        self.assertIn(b"<a href=\"/get\">", body)
        # Without an upload token configured, uploads are off.
        hub.upload_token = ""
        self.assertEqual(upload(apk, 15)[0], 403)

    def test_pulls_new_builds_from_github_releases(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        apk = b"PK\x03\x04" + os.urandom(200000)
        state = {"tag": "v2.0.14", "seen_auth_on_download": None}

        class FakeGitHub(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                base = "http://127.0.0.1:%d" % self.server.server_address[1]
                if self.path == "/repos/me/app/releases/latest":
                    assert self.headers["Authorization"] == "Bearer gh-token"
                    body = json.dumps({"tag_name": state["tag"], "assets": [
                        {"name": "HealthTracker.apk", "url": base + "/asset/1", "size": len(apk)}]}).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/asset/1":
                    self.send_response(302); self.send_header("Location", base + "/s3/file"); self.end_headers()
                elif self.path == "/s3/file":
                    state["seen_auth_on_download"] = self.headers.get("Authorization")
                    self.send_response(200); self.send_header("Content-Length", str(len(apk))); self.end_headers()
                    self.wfile.write(apk)

        gh = HTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=gh.serve_forever, daemon=True).start()
        api = "http://127.0.0.1:%d" % gh.server_address[1]
        hub = self.httpd.hub
        try:
            msg = healthhub.check_github_release(hub, "me/app", "gh-token", api=api)
            self.assertEqual(msg, "now offering 2.0.14")
            self.assertIsNone(state["seen_auth_on_download"])  # the token never leaves GitHub's API
            self.assertEqual(self.call("GET", "/api/app"), {"available": True, "versionCode": 14, "versionName": "2.0.14"})
            path, _ = hub.apk_info()
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), apk)
            self.assertEqual(healthhub.check_github_release(hub, "me/app", "gh-token", api=api), "up to date (2.0.14)")
            state["tag"] = "nightly"
            self.assertIn("not a version", healthhub.check_github_release(hub, "me/app", "gh-token", api=api))
        finally:
            gh.shutdown()
            gh.server_close()

    def test_bad_requests(self):
        self.call("GET", "/nope", expect=404)
        self.call("GET", "/healthz")
        req = urllib.request.Request(self.base + "/api/login", data=b"not json", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
