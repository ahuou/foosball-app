"""
Read-only mode: a deployment with READ_ONLY=1 must read the real data and
never change it, whatever a visitor POSTs.

Vocabulary:
  store     - the storage backend (LocalStore = CSV files on disk here).
  trial     - sandbox session; its matches live only in a signed cookie.
  write path - any route that would call a store write method.

Run:  python3 -m unittest discover -s tests
"""

import hashlib
import os
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("APP_SECRET", "test-secret")

import core      # noqa: E402
import store     # noqa: E402
import webapp    # noqa: E402

# Every write-capable POST route with a plausible form body.
WRITE_POSTS = [
    ("/edit", {"id": "1", "format": "1v1", "a1": "Zizou", "b1": "Mbappe",
               "score_a": "10", "score_b": "0"}),
    ("/delete", {"id": "1"}),
    ("/player/remove", {"name": "Zizou"}),
    ("/requests/new", {"title": "hack", "content": "x"}),
    ("/requests/reply", {"request_id": "1", "content": "x"}),
    ("/requests/status", {"id": "1", "status": "done"}),
    ("/requests/edit", {"id": "1", "title": "x", "content": "x"}),
    ("/requests/delete", {"id": "1"}),
]
# Markers of write controls that must never be rendered in read-only mode.
WRITE_MARKERS = ["action='/delete'", "action='/player/remove'",
                 "action='/requests/new'", "action='/requests/reply'",
                 "action='/requests/status'", "action='/requests/delete'",
                 "href='/edit?", "seed_singles"]


def _body(fields):
    return urllib.parse.urlencode(fields).encode()


def _cookies(headers, jar):
    for k, v in headers:
        if k == "Set-Cookie":
            name, _, rest = v.partition("=")
            jar[name] = rest.split(";", 1)[0]
    return "; ".join(f"{k}={v}" for k, v in jar.items() if v)


def _hash_dir(path):
    h = hashlib.sha256()
    for name in sorted(os.listdir(path)):
        with open(os.path.join(path, name), "rb") as f:
            h.update(name.encode() + f.read())
    return h.hexdigest()


class ReadOnlyModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = os.path.join(self.tmp.name, "data")
        # Seed real data with a normal (writable) store first.
        rw = store.LocalStore(data_dir=data, app_dir=self.tmp.name)
        for n in ("Zizou", "Mbappe"):
            rw.write_player(n)
        rw.append_match({"timestamp_iso": "2026-08-01T12:00:00+00:00",
                         "format": "1v1", "team_a": "Zizou", "team_b": "Mbappe",
                         "score_a": 10, "score_b": 6, "recorded_by": "Zizou"})
        rw.append_request({"author": "Zizou", "title": "dark mode", "content": ""})
        rw.append_reply({"request_id": "1", "author": "Mbappe", "content": "+1"})
        self.data = data
        self.before = _hash_dir(data)
        self.ro = store.ReadOnlyStore(rw)
        patches = [mock.patch.object(webapp, "READ_ONLY", True),
                   mock.patch.object(core, "READ_ONLY", True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def req(self, method, path, fields=None, cookie=None, query=""):
        return webapp.handle(method, path, query, cookie,
                             _body(fields or {}), self.ro)

    def login(self, name="Zizou"):
        jar = {}
        status, headers, _ = self.req("POST", "/login", {"name": name})
        self.assertEqual(status, 303)
        return _cookies(headers, jar), jar

    def test_store_wrapper_refuses_every_write(self):
        for attr in store._WRITE_METHODS:
            with self.assertRaises(store.ReadOnlyError, msg=attr):
                getattr(self.ro, attr)("x")
        self.assertEqual(len(self.ro.read_matches()), 1)

    def test_login_creates_no_player_and_opens_sandbox(self):
        cookie, jar = self.login("Brand New Person")
        self.assertIn("trial", jar)
        self.assertEqual(_hash_dir(self.data), self.before)

    def test_every_write_route_is_refused(self):
        cookie, _ = self.login()
        for path, fields in WRITE_POSTS:
            status, _, _ = self.req("POST", path, fields, cookie)
            self.assertEqual(status, 403, path)
            # Also refused with no cookie at all.
            status, _, _ = self.req("POST", path, fields, None)
            self.assertEqual(status, 403, path)
        self.assertEqual(_hash_dir(self.data), self.before)

    def test_record_goes_to_cookie_not_store(self):
        cookie, jar = self.login()
        status, headers, body = self.req(
            "POST", "/record", {"format": "1v1", "a1": "Zizou", "b1": "Mbappe",
                                "score_a": "10", "score_b": "3"}, cookie)
        self.assertEqual(status, 200)
        self.assertTrue(any(k == "Set-Cookie" and v.startswith("trial=")
                            for k, v in headers))
        self.assertEqual(_hash_dir(self.data), self.before)

    def test_pages_render_real_data_without_write_controls(self):
        cookie, _ = self.login()
        pages = [("/", ""), ("/scores", ""), ("/history", ""), ("/matrix", ""),
                 ("/requests", ""), ("/record", ""), ("/how", ""),
                 ("/player", "name=Zizou")]
        for path, q in pages:
            status, _, body = self.req("GET", path, cookie=cookie, query=q)
            html = body.decode()
            self.assertEqual(status, 200, path)
            self.assertIn("READ-ONLY DEMO", html, path)
            for marker in WRITE_MARKERS:
                self.assertNotIn(marker, html, f"{marker} on {path}")
        _, _, body = self.req("GET", "/history", cookie=cookie)
        self.assertIn("Mbappe", body.decode())   # the real match is visible
        _, _, body = self.req("GET", "/")        # logged-out login screen
        self.assertNotIn("seed_singles", body.decode())

    def test_edit_pages_redirect(self):
        cookie, _ = self.login()
        for path in ("/edit", "/requests/edit"):
            status, _, _ = self.req("GET", path, cookie=cookie, query="id=1")
            self.assertEqual(status, 303, path)

    def test_old_real_login_cookie_is_downgraded_to_sandbox(self):
        real = "who=" + webapp.encode_signed({"n": "Zizou"})
        status, _, body = self.req("GET", "/history", cookie=real)
        self.assertEqual(status, 200)
        self.assertNotIn("action='/delete'", body.decode())
        status, _, _ = self.req("POST", "/record",
                                {"format": "1v1", "a1": "Zizou", "b1": "Mbappe",
                                 "score_a": "10", "score_b": "3"}, real)
        self.assertEqual(status, 200)
        self.assertEqual(_hash_dir(self.data), self.before)


class WritableModeUnchangedTest(unittest.TestCase):
    """Without the flag the app still writes (guards against over-blocking)."""

    def test_record_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = store.LocalStore(data_dir=os.path.join(tmp, "data"), app_dir=tmp)
            status, headers, _ = webapp.handle("POST", "/login", "", None,
                                               _body({"name": "Zizou"}), s)
            cookie = _cookies(headers, {})
            status, _, _ = webapp.handle(
                "POST", "/record", "", cookie,
                _body({"format": "1v1", "a1": "Zizou", "b1": "Mbappe",
                       "score_a": "10", "score_b": "3",
                       "confirm_b1": "1"}), s)
            self.assertEqual(status, 200)
            self.assertEqual(len(s.read_matches()), 1)


class RatingMathTest(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(core.bucket_x(50), (20, 20))
        self.assertEqual(core.bucket_x(51), (15, 25))
        self.assertEqual(core.bucket_x(300), (10, 30))
        self.assertEqual(core.bucket_x(301), (5, 35))

    def test_readme_example_is_zero_sum(self):
        a, b, _ = core.rating_delta(1200, 1000, a_won=True)
        self.assertEqual((a, b), (10, -10))      # expected win moves fav X
        a, b, _ = core.rating_delta(1200, 1000, a_won=False)
        self.assertEqual((a, b), (-30, 30))      # upset moves underdog X

    def test_margin_multiplier(self):
        self.assertAlmostEqual(core.margin_multiplier(10, 6), 1.0)
        self.assertAlmostEqual(core.margin_multiplier(10, 8), 0.8)
        a, b, _ = core.rating_delta(1000, 1000, True, core.margin_multiplier(10, 0))
        self.assertEqual((a, b), (32, -32))


if __name__ == "__main__":
    unittest.main()
