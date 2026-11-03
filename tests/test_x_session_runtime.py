"""仅使用临时目录和假会话数据；不读取浏览器或请求网络。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import uuid
import warnings

from yt_dlp.cookies import YoutubeDLCookieJar

import archiver as app


class XSessionRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="x-session-offline-test-")
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        data_patch = patch.object(app, "DATA", self.data)
        data_patch.start()
        self.addCleanup(data_patch.stop)
        env_patch = patch.dict(os.environ, {"ARCHIVER_X_COOKIE_FILE": "", "ARCHIVER_PROXY_URL": ""})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.private = self.data / "private"
        self.private.mkdir(mode=0o700)
        self.path = self.private / "x_cookies.txt"
        self.future = int(time.time()) + 86400

    def rows(self, expiry=None):
        expiry = self.future if expiry is None else expiry
        return [(".x.com", "auth_token", "FAKE_AUTH_COOKIE", expiry),
                (".x.com", "ct0", "FAKE_CSRF_COOKIE", expiry)]

    def write_cookies(self, rows=None, mode=0o600):
        rows = self.rows() if rows is None else rows
        content = "# Netscape HTTP Cookie File\n" + "".join(
            f"{domain}\t{'TRUE' if domain.startswith('.') else 'FALSE'}\t/\tTRUE\t{expiry}\t{name}\t{value}\n"
            for domain, name, value, expiry in rows)
        self.path.write_text(content)
        self.path.chmod(mode)
        return content.encode()

    @staticmethod
    def client():
        return SimpleNamespace(params={"cookiefile": None}, cookiejar=YoutubeDLCookieJar())

    def load(self, client=None):
        client = self.client() if client is None else client
        with patch.dict(os.environ, {"ARCHIVER_X_COOKIE_FILE": str(self.path)}):
            result = app.load_x_session(client)
        return result, client

    def assert_fixed_error(self, action):
        with self.assertRaises(app.SafeError) as caught:
            action()
        self.assertIn("X 会话文件无效", str(caught.exception))
        self.assertNotIn("FAKE_", str(caught.exception))
        self.assertNotIn(str(self.path), str(caught.exception))

    def test_unset_does_not_access_private_paths_or_cookiejar(self):
        with patch.object(app, "DATA", object()), patch.object(app.os, "open") as opener:
            self.assertFalse(app.load_x_session(object()))
        opener.assert_not_called()

    def test_valid_session_is_loaded_in_memory_without_writeback(self):
        original = self.write_cookies()
        with app.YoutubeDL({"cookiefile": None, "cachedir": False, "logger": app.QuietLogger()},
                           auto_init=False) as client:
            jar = client.cookiejar
            with patch.object(jar, "save") as save:
                self.assertTrue(self.load(client)[0])
                self.assertEqual({cookie.name for cookie in jar}, {"auth_token", "ct0"})
                self.assertIsNone(jar.filename)
                client.close()
                save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.private.iterdir()), [self.path])

    def test_session_expiry_zero_survives_as_session_cookie(self):
        self.write_cookies(self.rows(expiry=0))
        result, client = self.load()
        self.assertTrue(result)
        self.assertTrue(all(cookie.expires is None and cookie.discard for cookie in client.cookiejar))

    def test_read_only_owner_permission_is_accepted(self):
        self.write_cookies(mode=0o400)
        self.assertTrue(self.load()[0])

    def test_x_and_twitter_subdomains_are_allowed(self):
        rows = [(".api.x.com", "auth_token", "FAKE_AUTH", self.future),
                (".api.x.com", "ct0", "FAKE_CSRF", self.future),
                (".twitter.com", "lang", "en", self.future)]
        self.write_cookies(rows)
        self.assertTrue(self.load()[0])

    def test_foreign_domain_is_rejected_even_if_mixed_with_valid_session(self):
        for domain in ("example.com", "evilx.com", ".x.com.evil.test", ".othertwitter.com"):
            with self.subTest(domain=domain):
                self.write_cookies(self.rows() + [(domain, "unrelated", "FAKE_PRIVATE", self.future)])
                self.assert_fixed_error(self.load)

    def test_expired_auth_is_rejected_without_anonymous_fallback(self):
        self.write_cookies(self.rows(expiry=1))
        self.assert_fixed_error(self.load)

    def test_expired_non_auth_cookie_is_discarded_without_invalidating_session(self):
        original = self.write_cookies(self.rows() + [(".x.com", "optional", "FAKE_OPTIONAL", 1)])
        result, client = self.load()
        self.assertTrue(result)
        self.assertEqual({cookie.name for cookie in client.cookiejar}, {"auth_token", "ct0"})
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_or_empty_required_cookie_is_rejected(self):
        cases = [self.rows()[:1],
                 [(".x.com", "auth_token", "", self.future), self.rows()[1]],
                 [self.rows()[0], (".x.com", "ct0", " ", self.future)]]
        for rows in cases:
            with self.subTest(rows=len(rows)):
                self.write_cookies(rows)
                self.assert_fixed_error(self.load)

    def test_permissive_or_executable_cookie_file_is_rejected(self):
        for mode in (0o644, 0o640, 0o660, 0o604, 0o700):
            with self.subTest(mode=oct(mode)):
                self.write_cookies(mode=mode)
                self.assert_fixed_error(self.load)

    def test_noncanonical_path_is_refused_before_opening_private_file(self):
        for path in (str(self.private / "other.txt"), str(self.private / ".." / "private" / "x_cookies.txt")):
            with self.subTest(path_kind="noncanonical"), patch.dict(os.environ, {"ARCHIVER_X_COOKIE_FILE": path}):
                with patch.object(app.os, "open") as opener:
                    self.assert_fixed_error(lambda: app.load_x_session(self.client()))
                opener.assert_not_called()

    def test_symbolic_cookie_file_is_refused(self):
        target = self.data / "elsewhere.txt"
        target.write_text("FAKE_PRIVATE")
        target.chmod(0o600)
        self.path.symlink_to(target)
        self.assert_fixed_error(self.load)
        self.assertEqual(target.read_text(), "FAKE_PRIVATE")

    def test_symbolic_private_directory_is_refused(self):
        alternate = self.data / "alternate"
        alternate.mkdir()
        self.private.rmdir()
        self.private.symlink_to(alternate, target_is_directory=True)
        self.assert_fixed_error(self.load)
        self.assertEqual(list(alternate.iterdir()), [])

    def test_nonregular_and_oversized_files_are_refused(self):
        self.path.mkdir()
        self.assert_fixed_error(self.load)
        self.path.rmdir()
        with self.path.open("wb") as stream:
            stream.truncate(app.X_COOKIE_MAX_BYTES + 1)
        self.path.chmod(0o600)
        self.assert_fixed_error(self.load)

    def test_parser_logs_warnings_and_errors_never_escape(self):
        self.write_cookies()
        client = self.client()
        def bad_parser(**kwargs):
            print("FAKE_COOKIE_STDOUT")
            import sys
            print("FAKE_COOKIE_STDERR", file=sys.stderr)
            warnings.warn("FAKE_COOKIE_WARNING")
            raise app.SafeError("FAKE_COOKIE_EXCEPTION")
        client.cookiejar.load = Mock(side_effect=bad_parser)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assert_fixed_error(lambda: self.load(client))
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_malformed_lines_are_rejected_without_skipping_or_disclosing_them(self):
        original = self.write_cookies()
        self.path.write_bytes(original + b"FAKE_PRIVATE_BAD_LINE\n")
        self.assert_fixed_error(self.load)

    def test_download_loads_session_before_extract_and_keeps_best_format(self):
        self.write_cookies()
        jobs = self.data / "jobs"
        jobs.mkdir()
        folder = jobs / uuid.uuid4().hex
        folder.mkdir()
        client = MagicMock()
        client.__enter__.return_value = client
        client.cookiejar = YoutubeDLCookieJar()
        options_seen = []
        def factory(options):
            options_seen.append(options)
            client.params = options
            return client
        def extract(*args, **kwargs):
            self.assertEqual({cookie.name for cookie in client.cookiejar}, {"auth_token", "ct0"})
            return {"id": "fake-video"}
        def finish(info, **kwargs):
            (folder / "video-001.mp4").write_bytes(b"fake-video")
            return info
        client.extract_info.side_effect = extract
        client.process_ie_result.side_effect = finish
        with patch.dict(os.environ, {"ARCHIVER_X_COOKIE_FILE": str(self.path)}):
            with patch.object(app, "YoutubeDL", side_effect=factory), patch.object(app.shutil, "disk_usage") as usage:
                usage.return_value.free = 20_000_000_000
                self.assertEqual(app.download("fake-url", folder, max_video=app.USER_MAX_VIDEO),
                                 [folder / "video-001.mp4"])
        self.assertEqual(options_seen[0]["format"], "bv*+ba/b/bv*")
        self.assertEqual(options_seen[0]["max_filesize"], 1_900_000_000)
        self.assertIsNone(options_seen[0]["cookiefile"])
        self.assertNotIn("cookiesfrombrowser", options_seen[0])

    def test_invalid_session_stops_before_extraction(self):
        self.write_cookies(mode=0o644)
        jobs = self.data / "jobs"
        jobs.mkdir()
        folder = jobs / uuid.uuid4().hex
        folder.mkdir()
        with patch.dict(os.environ, {"ARCHIVER_X_COOKIE_FILE": str(self.path)}):
            with patch.object(app, "YoutubeDL") as factory, patch.object(app.shutil, "disk_usage") as usage:
                usage.return_value.free = 20_000_000_000
                client = factory.return_value.__enter__.return_value
                client.params = {"cookiefile": None}
                self.assert_fixed_error(lambda: app.download("fake-url", folder))
                client.extract_info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
