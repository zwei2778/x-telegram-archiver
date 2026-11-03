"""离线假 Cookie 测试；不访问浏览器、真实私密目录或网络。"""

from contextlib import redirect_stderr, redirect_stdout
from http.cookiejar import Cookie
import importlib.util
import io
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from yt_dlp.cookies import YoutubeDLCookieJar

import prepare_x_session as app


def fake_cookie(name, value="FAKE_TEST_ONLY", *, domain=".x.com", path="/", expires=None,
                discard=True, secure=True):
    return Cookie(version=0, name=name, value=value, port=None, port_specified=False,
                  domain=domain, domain_specified=domain.startswith("."),
                  domain_initial_dot=domain.startswith("."), path=path, path_specified=True,
                  secure=secure, expires=expires, discard=discard, comment=None,
                  comment_url=None, rest={})


def fake_session():
    return [fake_cookie("auth_token"), fake_cookie("ct0"), fake_cookie("twid", "u%3D1234567890")]


class SessionExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root_patch = patch.object(app, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    @property
    def target(self):
        return self.root / "data" / "private" / "x_cookies.txt"

    def run_tool(self, argv=(), *, source=None, confirm="EXPORT", save="SAVE @archive_test", tty=True, backend=None, defaults=True):
        output, errors = io.StringIO(), io.StringIO()
        argv = list(argv)
        if defaults:
            if "--profile" not in argv:
                argv += ["--profile", "Profile 1"]
            if "--expected-account" not in argv:
                argv += ["--expected-account", "archive_test"]
        if source is None:
            source = fake_session()
        with patch.object(app.sys, "stdin", Mock(isatty=Mock(return_value=tty))):
            with patch("builtins.input", side_effect=[confirm, save]) as user_input:
                with patch.object(app, "extract_browser_cookies", return_value=source) as extractor:
                    if backend is not None:
                        extractor.side_effect = backend
                    with redirect_stdout(output), redirect_stderr(errors):
                        result = app.main(list(argv))
        return result, output.getvalue() + errors.getvalue(), extractor, user_input

    def test_import_has_no_browser_side_effect(self):
        with patch("yt_dlp.cookies.extract_cookies_from_browser") as extract:
            spec = importlib.util.spec_from_file_location("x_session_import_test", app.__file__)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        extract.assert_not_called()

    def test_help_does_not_read_browser_or_create_destination(self):
        with patch.object(app, "extract_browser_cookies") as extract:
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as exit_status:
                    app.main(["--help"])
        self.assertEqual(exit_status.exception.code, 0)
        extract.assert_not_called()
        self.assertFalse((self.root / "data").exists())

    def test_non_interactive_rejected_before_confirmation(self):
        code, output, extract, user_input = self.run_tool(tty=False)
        self.assertNotEqual(code, 0)
        extract.assert_not_called()
        user_input.assert_not_called()
        self.assertFalse((self.root / "data").exists())

    def test_cancel_does_not_read_or_create_files(self):
        for text in ("", "export", "NO"):
            with self.subTest(text=text):
                code, output, extract, _ = self.run_tool(confirm=text)
                self.assertNotEqual(code, 0)
                extract.assert_not_called()
                self.assertFalse((self.root / "data").exists())

    def test_invalid_arguments_are_redacted(self):
        code, output, extract, _ = self.run_tool(["--unsupported=FAKE_TEST_ONLY"])
        self.assertNotEqual(code, 0)
        self.assertNotIn("FAKE_TEST_ONLY", output)
        extract.assert_not_called()

    def test_browser_and_profile_are_passed_only_after_confirmation(self):
        code, _, extract, _ = self.run_tool(["--browser", "chrome", "--profile", "Profile 2"])
        self.assertEqual(code, 0)
        extract.assert_called_once_with("chrome", "Profile 2")

    def test_missing_profile_or_expected_account_is_rejected_before_read(self):
        for argv in ([], ["--profile", "Profile 1"], ["--expected-account", "archive_test"]):
            with self.subTest(argv=argv):
                code, _, extract, prompt = self.run_tool(argv, defaults=False)
                self.assertNotEqual(code, 0)
                extract.assert_not_called()
                prompt.assert_not_called()
                self.assertFalse((self.root / "data").exists())

    def test_profile_rejects_default_search_paths_and_whitespace(self):
        for profile in ("", " ", "Profile 0", "Profile 01", "Profile 1 ", " default", "../Default",
                        "/tmp/Profile 1", "~/Library/Application Support/Google/Chrome", "Profile 1/Network"):
            with self.subTest(profile=profile):
                code, _, extract, prompt = self.run_tool(["--profile", profile])
                self.assertNotEqual(code, 0)
                extract.assert_not_called()
                prompt.assert_not_called()
                self.assertFalse((self.root / "data").exists())

    def test_account_argument_must_be_username_and_does_not_echo_invalid_input(self):
        for account in ("", "FAKE_TEST_ONLY_SECRET_ACCOUNT", "user@example.test", "../archive_test",
                        "archive test", "@@archive_test", "小号"):
            with self.subTest(case=account):
                code, output, extract, prompt = self.run_tool(["--expected-account", account])
                self.assertNotEqual(code, 0)
                self.assertNotIn("FAKE_TEST_ONLY_SECRET_ACCOUNT", output)
                extract.assert_not_called()
                prompt.assert_not_called()

    def test_optional_expected_id_must_be_positive_ascii_integer(self):
        for identity in ("", "0", "01", "123456789012345678901", "１２３", "FAKE_TEST_ONLY", "123\n"):
            with self.subTest(identity=identity):
                code, output, extract, prompt = self.run_tool(["--expected-account-id", identity])
                self.assertNotEqual(code, 0)
                self.assertNotIn("FAKE_TEST_ONLY", output)
                extract.assert_not_called()
                prompt.assert_not_called()

    def test_save_requires_second_exact_confirmation_and_does_not_create_files(self):
        for save in ("", "NO", "SAVE @wrong_user", "save @archive_test", " SAVE @archive_test", "SAVE @archive_test "):
            with self.subTest(save=save):
                code, output, extract, prompt = self.run_tool(save=save)
                self.assertNotEqual(code, 0)
                extract.assert_called_once()
                self.assertEqual(prompt.call_count, 2)
                self.assertIn("原会话文件保持不变", output)
                self.assertFalse((self.root / "data").exists())

    def test_cancel_save_keeps_existing_contents_permissions_and_siblings(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("FAKE_OLD_CONTENT")
        self.target.chmod(0o400)
        sibling = self.target.parent / "untouched.txt"
        sibling.write_text("FAKE_SIBLING")
        code, _, _, _ = self.run_tool(["--replace"], save="NO")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.target.read_text(), "FAKE_OLD_CONTENT")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o400)
        self.assertEqual(sibling.read_text(), "FAKE_SIBLING")
        self.assertEqual(sorted(p.name for p in self.target.parent.iterdir()), ["untouched.txt", "x_cookies.txt"])

    def test_only_account_id_and_manual_check_url_are_shown_not_cookie_values(self):
        code, output, _, prompt = self.run_tool(["--expected-account", "@archive_test"])
        self.assertEqual(code, 0)
        self.assertEqual(prompt.call_count, 2)
        self.assertIn("https://x.com/intent/user?user_id=1234567890", output)
        self.assertIn("不等于服务器已验证登录令牌归属", output)
        self.assertNotIn("FAKE_TEST_ONLY", output)
        self.assertNotIn("u%3D1234567890", output)

    def test_marker_formats_repeated_same_id_and_independent_expected_id(self):
        source = fake_session() + [fake_cookie("twid", "u=1234567890", domain=".twitter.com")]
        code, _, _, _ = self.run_tool(["--expected-account-id", "1234567890"], source=source)
        self.assertEqual(code, 0)

    def test_missing_expired_invalid_or_conflicting_marker_cannot_replace(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("FAKE_OLD_CONTENT")
        self.target.chmod(0o600)
        auth = fake_session()[:2]
        sources = [auth, auth + [fake_cookie("twid", "u=1234567890", expires=1)],
                   fake_session() + [fake_cookie("twid", "u=9876543210", domain=".twitter.com")],
                   fake_session() + [fake_cookie("twid", "u=9876543210")]]
        for value in ("u=123secret", "u=１２３", "u=0", "u=0123", "u%253D123", "u=123&x=1", "", "FAKE_TEST_ONLY"):
            sources.append(auth + [fake_cookie("twid", value)])
        for source in sources:
            with self.subTest(count=len(source)):
                code, output, _, prompt = self.run_tool(["--replace"], source=source)
                self.assertNotEqual(code, 0)
                self.assertEqual(prompt.call_count, 1)
                self.assertEqual(self.target.read_text(), "FAKE_OLD_CONTENT")
                self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
                self.assertEqual([p.name for p in self.target.parent.iterdir()], ["x_cookies.txt"])
                self.assertNotIn("FAKE_TEST_ONLY", output)

    def test_independent_expected_id_mismatch_cannot_replace(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("FAKE_OLD_CONTENT")
        code, output, _, prompt = self.run_tool(["--replace", "--expected-account-id", "9876543210"])
        self.assertNotEqual(code, 0)
        self.assertEqual(prompt.call_count, 1)
        self.assertIn("预期 ID 不一致", output)
        self.assertEqual(self.target.read_text(), "FAKE_OLD_CONTENT")

    def test_zero_decrypted_cookie_result_is_refused_without_save_prompt(self):
        code, output, _, prompt = self.run_tool(source=[])
        self.assertNotEqual(code, 0)
        self.assertEqual(prompt.call_count, 1)
        self.assertIn("未能解密任何 Chrome Cookie", output)
        self.assertFalse((self.root / "data").exists())

    def test_account_marker_accepts_only_exact_encoded_or_plain_forms(self):
        for value, expected in (("u=1", "1"), ("u%3D1234567890", "1234567890"),
                                ("u%3d1234567890", "1234567890"),
                                ("u=12345678901234567890", "12345678901234567890")):
            with self.subTest(value=value):
                jar = app.select_cookies(fake_session()[:2] + [fake_cookie("twid", value)])
                self.assertEqual(app.account_marker(jar), expected)

    def test_authentication_expiring_during_manual_review_cannot_replace(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("FAKE_OLD_CONTENT")
        source = [fake_cookie("auth_token", expires=150, discard=False),
                  fake_cookie("ct0", expires=150, discard=False),
                  fake_cookie("twid", "u=1234567890")]
        with patch.object(app.time, "time", side_effect=[100, 200]):
            code, _, _, prompt = self.run_tool(["--replace"], source=source)
        self.assertNotEqual(code, 0)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(self.target.read_text(), "FAKE_OLD_CONTENT")
        self.assertEqual([p.name for p in self.target.parent.iterdir()], ["x_cookies.txt"])

    def test_interruption_during_second_confirmation_leaves_no_output_file(self):
        code, output, _, prompt = self.run_tool(save=EOFError("FAKE_TEST_ONLY"))
        self.assertNotEqual(code, 0)
        self.assertEqual(prompt.call_count, 2)
        self.assertNotIn("FAKE_TEST_ONLY", output)
        self.assertFalse((self.root / "data").exists())

    def test_domain_boundary_expiry_and_original_cookie_fields(self):
        future = int(time.time()) + 600
        source = fake_session() + [
            fake_cookie("allowed", domain="media.x.com", path="/only", expires=future, discard=False),
            fake_cookie("twitter", domain=".sub.twitter.com", expires=future, discard=False, secure=False),
            fake_cookie("old", expires=int(time.time()) - 1, discard=False),
        ] + [fake_cookie("outside", domain=domain) for domain in
             ("google.com", "evilx.com", "x.com.evil.test", ".twitter.com.evil.test")]
        before = [(c.domain, c.path, c.secure, c.expires, c.discard) for c in source]
        code, output, _, _ = self.run_tool(source=source)
        self.assertEqual(code, 0)
        self.assertNotIn("FAKE_TEST_ONLY", output)
        self.assertEqual(before, [(c.domain, c.path, c.secure, c.expires, c.discard) for c in source])
        loaded = YoutubeDLCookieJar(str(self.target))
        loaded.load(ignore_discard=True, ignore_expires=True)
        cookies = {cookie.name: cookie for cookie in loaded}
        self.assertEqual(set(cookies), {"auth_token", "ct0", "twid", "allowed", "twitter"})
        self.assertEqual((cookies["allowed"].domain, cookies["allowed"].path,
                          cookies["allowed"].secure, cookies["allowed"].expires,
                          cookies["allowed"].discard), ("media.x.com", "/only", True, future, False))
        self.assertEqual(cookies["twitter"].domain, ".sub.twitter.com")
        self.assertFalse(cookies["twitter"].secure)

    def test_session_cookies_round_trip_without_fabricated_expiry(self):
        source = fake_session()
        selected = app.select_cookies(source)
        target = app.prepare_destination(False)
        app.write_atomic(selected, target, False)
        loaded = YoutubeDLCookieJar(str(target))
        loaded.load(ignore_discard=True, ignore_expires=True)
        self.assertEqual(len(loaded), 3)
        self.assertTrue(all(c.expires is None and c.discard for c in loaded))
        self.assertTrue(all(c.expires is None for c in source))

    def test_chrome_microsecond_future_authentication_is_saved_as_same_unix_time(self):
        future = int(time.time()) + 600
        raw = (future + app.WINDOWS_TO_UNIX_SECONDS) * 1_000_000
        source = [fake_cookie("auth_token", expires=raw, discard=False),
                  fake_cookie("ct0", expires=raw, discard=False),
                  fake_cookie("twid", "u=1234567890", expires=raw, discard=False)]
        original = [(c.domain, c.path, c.secure, c.expires, c.discard) for c in source]
        code, output, _, _ = self.run_tool(source=source)
        self.assertEqual(code, 0)
        loaded = YoutubeDLCookieJar(str(self.target))
        loaded.load(ignore_discard=True, ignore_expires=True)
        self.assertTrue(all(c.expires == future and not c.discard for c in loaded))
        self.assertEqual(original, [(c.domain, c.path, c.secure, c.expires, c.discard) for c in source])
        self.assertNotIn(str(raw), output)
        self.assertNotIn("FAKE_TEST_ONLY", output)

    def test_chrome_microsecond_expired_authentication_is_rejected(self):
        expired = int(time.time()) - 600
        raw = (expired + app.WINDOWS_TO_UNIX_SECONDS) * 1_000_000
        source = [fake_cookie("auth_token", expires=raw, discard=False), fake_cookie("ct0")]
        code, output, _, _ = self.run_tool(source=source)
        self.assertNotEqual(code, 0)
        self.assertFalse(self.target.exists())
        self.assertEqual(source[0].expires, raw)
        self.assertNotIn(str(raw), output)

    def test_chrome_normalization_preserves_session_and_unix_expiry(self):
        future = int(time.time()) + 600
        session = fake_cookie("auth_token")
        unix = fake_cookie("ct0", expires=future, discard=False)
        selected = app.select_cookies([session, unix], from_chrome=True)
        cookies = {cookie.name: cookie for cookie in selected}
        self.assertIsNone(cookies["auth_token"].expires)
        self.assertEqual(cookies["ct0"].expires, future)
        self.assertIsNone(session.expires)
        self.assertEqual(unix.expires, future)

    def test_chrome_microseconds_are_rounded_down_without_extending_expiry(self):
        future = int(time.time()) + 600
        cookie = fake_cookie("auth_token", expires=(future + app.WINDOWS_TO_UNIX_SECONDS) * 1_000_000)
        cookie.expires += 999_999
        result = app.chrome_cookie_copy(cookie)
        self.assertEqual(result.expires, future)
        self.assertNotEqual(result.expires, cookie.expires)

    def test_invalid_chrome_expiry_is_redacted_and_refused(self):
        invalid = ("FAKE_TEST_ONLY", True, float("nan"), float("inf"), float("-inf"),
                   -1, app.MAX_UNIX_EXPIRY + 1, 10**1000)
        for index, value in enumerate(invalid):
            with self.subTest(case=index):
                source = fake_session()
                source[0].expires = value
                code, output, _, _ = self.run_tool(source=source)
                self.assertNotEqual(code, 0)
                self.assertFalse(self.target.exists())
                self.assertIn("到期时间格式或范围无效", output)
                self.assertNotIn("FAKE_TEST_ONLY", output)

    def test_domain_syntax_matches_nas_validation(self):
        for domain in ("x.com", ".x.com", ".TWITTER.COM", "media.x.com", ".sub.twitter.com"):
            with self.subTest(domain=domain):
                self.assertTrue(app.x_domain(domain))
        invalid = ("..x.com", "bad..x.com", "bad_.x.com", "bad/.x.com", "@x.com", " x.com",
                   "x.com.", "x.com\\", "bad\tx.com", "bad\nx.com", "💥.x.com", "", None)
        for index, domain in enumerate(invalid):
            with self.subTest(case=index):
                self.assertFalse(app.x_domain(domain))
        selected = app.select_cookies(fake_session() + [fake_cookie("invalid", domain="bad..x.com")],
                                      from_chrome=True)
        self.assertEqual({c.name for c in selected}, {"auth_token", "ct0", "twid"})

    def test_requires_nonempty_auth_token_and_ct0_on_x_domains(self):
        sources = [[], [fake_cookie("auth_token")],
                   [fake_cookie("auth_token", ""), fake_cookie("ct0")],
                   [fake_cookie("auth_token", "   "), fake_cookie("ct0")],
                   [fake_cookie("auth_token", domain="google.com"), fake_cookie("ct0")],
                   [fake_cookie("auth_token", expires=1), fake_cookie("ct0")]]
        for source in sources:
            with self.subTest(source_count=len(source)):
                code, output, _, _ = self.run_tool(source=source)
                self.assertNotEqual(code, 0)
                self.assertFalse(self.target.exists())
                self.assertNotIn("FAKE_TEST_ONLY", output)

    def test_permissions_and_no_other_cookie_files(self):
        code, _, _, _ = self.run_tool()
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.target.parent.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in self.target.parent.iterdir()], ["x_cookies.txt"])

    def test_existing_target_refused_before_browser_read(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("FAKE_OLD_CONTENT")
        code, output, extract, _ = self.run_tool()
        self.assertNotEqual(code, 0)
        self.assertIn("--replace", output)
        extract.assert_not_called()
        self.assertEqual(self.target.read_text(), "FAKE_OLD_CONTENT")
        self.assertNotIn("FAKE_OLD_CONTENT", output)

    def test_explicit_replace_requires_confirmation_and_only_replaces_target(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("FAKE_OLD_CONTENT")
        sibling = self.target.parent / "leave-alone.txt"
        sibling.write_text("FAKE_SIBLING")
        code, _, extract, _ = self.run_tool(["--replace"], confirm="NO")
        self.assertNotEqual(code, 0)
        extract.assert_not_called()
        self.assertEqual(self.target.read_text(), "FAKE_OLD_CONTENT")
        code, output, _, _ = self.run_tool(["--replace"])
        self.assertEqual(code, 0)
        self.assertNotIn("FAKE_OLD_CONTENT", self.target.read_text())
        self.assertNotIn("FAKE_TEST_ONLY", output)
        self.assertEqual(sibling.read_text(), "FAKE_SIBLING")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_rejects_data_and_private_symlinks(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.root / "data").symlink_to(elsewhere, target_is_directory=True)
        code, _, extract, _ = self.run_tool()
        self.assertNotEqual(code, 0)
        extract.assert_not_called()
        (self.root / "data").unlink()
        (self.root / "data").mkdir()
        (self.root / "data" / "private").symlink_to(elsewhere, target_is_directory=True)
        code, _, extract, _ = self.run_tool(["--replace"])
        self.assertNotEqual(code, 0)
        extract.assert_not_called()

    def test_rejects_target_symlink_directory_and_fifo(self):
        self.target.parent.mkdir(parents=True)
        untouched = self.root / "untouched.txt"
        untouched.write_text("FAKE_UNTOUCHED")
        self.target.symlink_to(untouched)
        for replace in ([], ["--replace"]):
            code, _, extract, _ = self.run_tool(replace)
            self.assertNotEqual(code, 0)
            extract.assert_not_called()
        self.assertEqual(untouched.read_text(), "FAKE_UNTOUCHED")
        self.target.unlink()
        self.target.mkdir()
        code, _, extract, _ = self.run_tool(["--replace"])
        self.assertNotEqual(code, 0)
        extract.assert_not_called()
        self.target.rmdir()
        os.mkfifo(self.target)
        code, _, extract, _ = self.run_tool(["--replace"])
        self.assertNotEqual(code, 0)
        extract.assert_not_called()

    def test_backend_stdout_stderr_and_raw_errors_are_suppressed(self):
        def noisy(*args):
            print("FAKE_TEST_ONLY")
            print("FAKE_TEST_ONLY", file=sys.stderr)
            raise RuntimeError("FAKE_TEST_ONLY")
        import sys
        code, output, _, _ = self.run_tool(backend=noisy)
        self.assertNotEqual(code, 0)
        self.assertNotIn("FAKE_TEST_ONLY", output)
        self.assertFalse(self.target.exists())

    def test_backend_system_exit_message_is_suppressed(self):
        def stopped(*args):
            raise SystemExit("FAKE_TEST_ONLY")
        code, output, _, _ = self.run_tool(backend=stopped)
        self.assertNotEqual(code, 0)
        self.assertNotIn("FAKE_TEST_ONLY", output)
        self.assertFalse(self.target.exists())

    def test_partial_write_cleanup_keeps_existing_file(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("FAKE_OLD_CONTENT")
        jar = Mock()
        def broken_save(stream, **kwargs):
            stream.write("FAKE_PARTIAL")
            raise OSError("FAKE_TEST_ONLY")
        jar.save.side_effect = broken_save
        with self.assertRaises(OSError):
            app.write_atomic(jar, self.target, True)
        self.assertEqual(self.target.read_text(), "FAKE_OLD_CONTENT")
        self.assertEqual([p.name for p in self.target.parent.iterdir()], ["x_cookies.txt"])

    def test_atomic_default_publication_does_not_overwrite_a_late_existing_file(self):
        target = app.prepare_destination(False)
        source = app.select_cookies(fake_session())
        original_link = os.link
        def late_existing(src, dst):
            Path(dst).write_text("FAKE_LATE_EXISTING")
            return original_link(src, dst)
        with patch.object(app.os, "link", side_effect=late_existing):
            with self.assertRaises(app.ExportError):
                app.write_atomic(source, target, False)
        self.assertEqual(target.read_text(), "FAKE_LATE_EXISTING")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["x_cookies.txt"])

    def test_line_or_column_injection_refuses_export(self):
        for value in ("FAKE\tVALUE", "FAKE\nVALUE", "FAKE\rVALUE"):
            with self.subTest(separator=repr(value)):
                code, _, _, _ = self.run_tool(source=fake_session() + [fake_cookie("extra", value)])
                self.assertNotEqual(code, 0)
                self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
