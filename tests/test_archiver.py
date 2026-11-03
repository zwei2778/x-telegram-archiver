"""离线测试，不使用真实 Telegram 令牌、用户信息或 X Cookie。"""
import unittest
import tempfile
from collections import defaultdict
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock, Mock, call, patch
import requests

import archiver as app


class ArchiverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="archiver-offline-test-")
        self.addCleanup(temporary.cleanup)
        data_patch = patch.object(app, "DATA", app.Path(temporary.name))
        data_patch.start()
        self.addCleanup(data_patch.stop)
        proxy_env = patch.dict(app.os.environ, {"ARCHIVER_PROXY_URL": ""})
        proxy_env.start()
        self.addCleanup(proxy_env.stop)
        self.db = app.database(":memory:")
        self.settings = {"owner_id": 1, "chat_id": -2}
        (app.DATA / "jobs").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.db.close()

    def test_urls_normalized_without_query(self):
        self.assertEqual(app.links("https://twitter.com/name/status/123?s=20 https://x.com/i/web/status/123"),
                         {"123": "https://x.com/i/status/123"})
        for url in ("https://x.com.evil.test/a/status/123", "https://x.com@evil.test/a/status/123",
                    "https://evil.test/a/status/123", "https://x.com/a/status/123/other",
                    "https://x.com:444/a/status/123", "https://t.co/abc"):
            self.assertEqual(app.links(url), {})

    def test_success_dedup_and_failed_retry(self):
        app.enqueue(self.db, "123", "https://x.com/i/status/123")
        self.db.execute("UPDATE jobs SET status='success'")
        self.assertIn("已上传", app.enqueue(self.db, "123", "url"))
        self.db.execute("UPDATE jobs SET status='failed'")
        self.assertIn("已加入", app.enqueue(self.db, "123", "url"))

    def test_uncertain_requires_explicit_retry(self):
        app.enqueue(self.db, "123", "url")
        self.db.execute("UPDATE jobs SET status='uncertain'")
        self.assertIn("/retry", app.enqueue(self.db, "123", "url"))
        self.assertIn("已加入", app.enqueue(self.db, "123", "url", retry=True))

    def fake_download(self, url, folder, **kwargs):
        first, second = folder / "video-001.mp4", folder / "video-002.mp4"
        first.write_bytes(b"test-video")
        second.write_bytes(b"test-video")
        self.folder = folder
        return [first, second]

    def test_upload_success_records_then_cleans(self):
        bot = Mock()
        bot.upload.return_value = 10
        app.enqueue(self.db, "123", "url")
        with patch.object(app, "download", side_effect=self.fake_download):
            app.process_one(self.db, bot, self.settings)
        self.assertEqual(bot.upload.call_count, 2)
        self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "success")
        self.assertEqual(self.db.execute("SELECT count(*) FROM sent").fetchone()[0], 2)
        self.assertFalse(self.folder.exists())
        bot.say.assert_called_once_with(1, "上传完成，已附原帖链接。")

    def test_personal_completion_replies_survive_restart_and_keep_each_source(self):
        path = app.DATA / "test-queue.sqlite3"
        db = app.database(path)
        try:
            for message in (
                SimpleNamespace(id=101, out=True, sender_id=1,
                                raw_text="https://x.com/a/status/123 https://x.com/b/status/456"),
                SimpleNamespace(id=102, out=True, sender_id=1,
                                raw_text="https://x.com/c/status/789"),
            ):
                app.accept_saved_message(db, message, 1)
        finally:
            db.close()
        db = app.database(path)
        try:
            account = Mock()
            account.upload.return_value = 10
            with patch.object(app, "download", side_effect=self.fake_download):
                for _ in range(3):
                    app.process_one(db, account, self.settings,
                                    max_video=app.USER_MAX_VIDEO, reply_to_source=True)
                    self.assertFalse(self.folder.exists())
            self.assertEqual(account.say.call_args_list, [
                call(1, "上传完成，已附原帖链接。", reply_to=101),
                call(1, "上传完成，已附原帖链接。", reply_to=101),
                call(1, "上传完成，已附原帖链接。", reply_to=102),
            ])
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE status='success'").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT count(*) FROM sent").fetchone()[0], 6)
        finally:
            db.close()

    def test_duplicate_message_does_not_replace_original_completion_source(self):
        app.enqueue(self.db, "123", "url", source_message_id=101)
        for status in ("queued", "processing", "success", "uncertain"):
            with self.subTest(status=status):
                self.db.execute("UPDATE jobs SET status=?", (status,))
                app.enqueue(self.db, "123", "url", source_message_id=102)
                self.assertEqual(self.db.execute("SELECT message_id FROM job_sources").fetchone()[0], 101)
                self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], status)

    def test_retry_completion_replies_to_new_request(self):
        app.enqueue(self.db, "123", "url", source_message_id=101)
        self.db.execute("UPDATE jobs SET status='uncertain'")
        app.enqueue(self.db, "123", "url", retry=True, source_message_id=102)
        account = Mock()
        account.upload.return_value = 10
        with patch.object(app, "download", side_effect=self.fake_download):
            app.process_one(self.db, account, self.settings,
                            max_video=app.USER_MAX_VIDEO, reply_to_source=True)
        account.say.assert_called_once_with(1, "上传完成，已附原帖链接。", reply_to=102)

    def test_legacy_queue_migrates_without_losing_jobs_or_inventing_source(self):
        path = app.DATA / "legacy-queue.sqlite3"
        db = app.sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE state (key TEXT PRIMARY KEY, value INTEGER);
            CREATE TABLE jobs (post_id TEXT PRIMARY KEY, url TEXT, status TEXT);
            CREATE TABLE sent (post_id TEXT, media TEXT, message_id INTEGER, PRIMARY KEY(post_id,media));
            INSERT INTO jobs VALUES ('old','url','success');
            INSERT INTO sent VALUES ('old','video-001',10);
            INSERT INTO jobs VALUES ('123','url','queued');
        """)
        db.close()
        db = app.database(path)
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM job_sources").fetchone()[0], 0)
            account = Mock()
            account.upload.return_value = 11
            with patch.object(app, "download", side_effect=self.fake_download):
                app.process_one(db, account, self.settings,
                                max_video=app.USER_MAX_VIDEO, reply_to_source=True)
            account.say.assert_called_once_with(1, "上传完成，已附原帖链接。")
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE status='success'").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM sent").fetchone()[0], 3)
        finally:
            db.close()

    def test_partial_upload_retry_skips_confirmed_video(self):
        bot = Mock()
        bot.upload.side_effect = [10, app.Uncertain("未确认")]
        app.enqueue(self.db, "123", "url")
        with patch.object(app, "download", side_effect=self.fake_download):
            app.process_one(self.db, bot, self.settings)
            self.assertFalse(self.folder.exists())
            self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "uncertain")
            app.enqueue(self.db, "123", "url", retry=True)
            bot.upload.side_effect = None
            bot.upload.return_value = 11
            app.process_one(self.db, bot, self.settings)
        self.assertEqual(bot.upload.call_count, 3)

    def test_interruption_is_uncertain_and_cleans(self):
        bot = Mock()
        bot.upload.side_effect = KeyboardInterrupt
        app.enqueue(self.db, "123", "url")
        with patch.object(app, "download", side_effect=self.fake_download):
            with self.assertRaises(KeyboardInterrupt):
                app.process_one(self.db, bot, self.settings)
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "uncertain")

    def test_exception_does_not_leak_secret(self):
        bot = app.Telegram("FAKE_TEST_CREDENTIAL")
        bot.session.post = Mock(side_effect=requests.ConnectionError("FAKE_TEST_CREDENTIAL"))
        with self.assertRaises(app.SafeError) as caught:
            bot.call("getMe")
        self.assertNotIn("FAKE_TEST_CREDENTIAL", str(caught.exception))
        with self.assertRaises(app.Uncertain):
            bot.call("sendVideo", files={"video": "fake"})

    def test_explicit_proxy_routes_telegram_without_environment_auth(self):
        for proxy in ("http://proxy.example.test:7890", "https://proxy.example.test:8443",
                      "http://[::1]:7890", ""):
            with self.subTest(proxy=proxy), patch.dict(app.os.environ, {"ARCHIVER_PROXY_URL": proxy}):
                bot = app.Telegram("FAKE_TEST_CREDENTIAL")
                bot.session.post = Mock()
                bot.session.post.return_value.json.return_value = {"ok": True, "result": {"id": 1}}
                self.assertEqual(bot.call("getMe"), {"id": 1})
                self.assertFalse(bot.session.trust_env)
                self.assertEqual(bot.session.proxies,
                                 {"http": proxy, "https": proxy} if proxy else {})
                self.assertFalse(bot.session.post.call_args.kwargs["allow_redirects"])

    def test_invalid_explicit_proxy_rejected_before_network(self):
        invalid = ("socks5://proxy.example.test:7890", "http://proxy.example.test",
                   "http://proxy.example.test:0", "http://proxy.example.test:65536",
                   "http://proxy.example.test:notaport", "http://proxy.example.test:7890/",
                   "http://proxy.example.test:7890?", "http://proxy.example.test:7890#",
                   "http://proxy.example.test:7890?key=FAKE_TEST_CREDENTIAL",
                   "http://proxy.example.test:7890#FAKE_TEST_CREDENTIAL",
                   "http://user:FAKE_TEST_CREDENTIAL@proxy.example.test:7890",
                   "http://@proxy.example.test:7890", "http://proxy.example.test:7890\n",
                   "http://proxy.example.test:7890/path", "http://proxy\\example.test:7890")
        with patch.object(app.requests, "Session") as session:
            for proxy in invalid:
                with self.subTest(proxy=proxy), patch.dict(app.os.environ, {"ARCHIVER_PROXY_URL": proxy}):
                    with self.assertRaises(app.SafeError) as caught:
                        app.Telegram("FAKE_TEST_CREDENTIAL")
                    self.assertNotIn(proxy, str(caught.exception))
                    self.assertNotIn("FAKE_TEST_CREDENTIAL", str(caught.exception))
            session.assert_not_called()

    def test_explicit_proxy_download_options_and_redacted_failure(self):
        folder = app.DATA / "jobs" / uuid.uuid4().hex
        folder.mkdir()
        try:
            path = folder / "video-001.mp4"
            path.write_bytes(b"fake-video")
            for proxy in ("http://proxy.example.test:7890", ""):
                with self.subTest(proxy=proxy), patch.dict(app.os.environ, {"ARCHIVER_PROXY_URL": proxy}):
                    with patch.object(app, "YoutubeDL") as factory, patch.object(app.shutil, "disk_usage") as usage:
                        usage.return_value.free = 20_000_000_000
                        fake = factory.return_value.__enter__.return_value
                        fake.extract_info.return_value = {"id": "fake-video"}
                        fake.process_ie_result.return_value = {"id": "fake-video"}
                        self.assertEqual(app.download("fake-url", folder), [path])
                        options = factory.call_args.args[0]
                        if proxy:
                            self.assertEqual(options["proxy"], proxy)
                        else:
                            self.assertNotIn("proxy", options)
                        fake.extract_info.side_effect = RuntimeError("FAKE_TEST_CREDENTIAL " + proxy)
                        with self.assertRaises(app.SafeError) as caught:
                            app.download("fake-url", folder)
                        self.assertNotIn("FAKE_TEST_CREDENTIAL", str(caught.exception))
                        self.assertNotIn("proxy.example.test", str(caught.exception))
            with patch.dict(app.os.environ, {"ARCHIVER_PROXY_URL": "http://user:FAKE_TEST_CREDENTIAL@proxy.example.test:7890"}):
                with patch.object(app, "YoutubeDL") as factory, patch.object(app.shutil, "disk_usage") as usage:
                    usage.return_value.free = 20_000_000_000
                    with self.assertRaises(app.SafeError):
                        app.download("fake-url", folder)
                    factory.assert_not_called()
        finally:
            app.clean_job(folder)

    def test_only_owner_private_messages_enqueue(self):
        bot = Mock()
        updates = [
            {"update_id": 1, "message": {"chat": {"type": "private"}, "from": {"id": 999}, "text": "https://x.com/u/status/123"}},
            {"update_id": 2, "message": {"chat": {"type": "group"}, "from": {"id": 1}, "text": "https://x.com/u/status/124"}},
        ]
        bot.updates.side_effect = [updates, KeyboardInterrupt]
        settings = dict(self.settings, initial_offset=0)
        with patch.object(app, "database", return_value=self.db):
            with self.assertRaises(KeyboardInterrupt):
                app.run(bot, settings)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def channel_update(self, code="test-code"):
        return {"channel_post": {"chat": {"type": "channel", "id": -100123}, "text": "/bind " + code}}

    def test_channel_without_sender_verifies_bot_and_owner(self):
        bot = Mock()
        bot.call.side_effect = [{"status": "administrator", "can_post_messages": True}, {"status": "creator"}]
        update = self.channel_update()
        self.assertEqual(app.binding_message(bot, 10, 1, update, "test-code"), update["channel_post"])
        self.assertEqual(bot.call.call_args_list[1].args, ("getChatMember", {"chat_id": -100123, "user_id": 1}))

    def test_channel_wrong_code_does_not_query_permissions(self):
        bot = Mock()
        self.assertIsNone(app.binding_message(bot, 10, 1, self.channel_update("wrong"), "test-code"))
        bot.call.assert_not_called()

    def test_channel_bot_must_be_admin_with_post_permission(self):
        for member in ({"status": "member"}, {"status": "administrator", "can_post_messages": False}):
            bot = Mock()
            bot.call.return_value = member
            with self.assertRaises(app.SafeError):
                app.binding_message(bot, 10, 1, self.channel_update(), "test-code")

    def test_channel_paired_owner_must_be_admin(self):
        bot = Mock()
        bot.call.side_effect = [{"status": "administrator", "can_post_messages": True}, {"status": "member"}]
        with self.assertRaises(app.SafeError):
            app.binding_message(bot, 10, 1, self.channel_update(), "test-code")

    def test_group_binding_still_requires_owner_sender(self):
        bot = Mock()
        message = {"chat": {"id": -2, "type": "supergroup"}, "from": {"id": 2}, "text": "/bind test-code"}
        self.assertIsNone(app.binding_message(bot, 10, 1, {"message": message}, "test-code"))
        message["from"]["id"] = 1
        self.assertEqual(app.binding_message(bot, 10, 1, {"message": message}, "test-code"), message)
        bot.call.assert_not_called()

    def test_setup_updates_include_channels_only_when_requested(self):
        bot = app.Telegram("FAKE_TEST_CREDENTIAL")
        bot.call = Mock(return_value=[])
        bot.updates(0, include_channels=True)
        self.assertIn("channel_post", bot.call.call_args.args[1]["allowed_updates"])
        bot.updates(0)
        self.assertNotIn("channel_post", bot.call.call_args.args[1]["allowed_updates"])

    def test_headless_token_file_uses_only_explicit_path(self):
        with patch.object(app.Path, "read_text", return_value="12345:FAKE_TEST_ONLY\n") as read:
            with patch.object(app.getpass, "getpass") as prompt:
                self.assertEqual(app.load_token("/fake/explicit-secret"), "12345:FAKE_TEST_ONLY")
        read.assert_called_once_with(encoding="utf-8")
        prompt.assert_not_called()

    def test_token_file_errors_do_not_echo_path_or_content(self):
        for error in (OSError("DO_NOT_LOG_PRIVATE_PATH"), UnicodeError("DO_NOT_LOG_DATA")):
            with patch.object(app.Path, "read_text", side_effect=error):
                with self.assertRaises(app.SafeError) as caught:
                    app.load_token("/fake/secret")
            self.assertNotIn("DO_NOT_LOG", str(caught.exception))
        with patch.object(app.Path, "read_text", return_value="DO_NOT_LOG_SECRET"):
            with self.assertRaises(app.SafeError) as caught:
                app.load_token("/fake/secret")
        self.assertNotIn("DO_NOT_LOG", str(caught.exception))

    def test_noninteractive_launch_without_secret_has_actionable_error(self):
        with patch.object(app.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(app.SafeError) as caught:
                app.load_token()
        self.assertIn("BOT_TOKEN_FILE", str(caught.exception))

    def test_sigterm_uses_existing_interruption_cleanup_path(self):
        with self.assertRaises(KeyboardInterrupt):
            app.stop_requested(15, None)

    def test_personal_account_accepts_simulated_60mb_video(self):
        folder = app.DATA / "jobs" / uuid.uuid4().hex
        folder.mkdir()
        try:
            path = folder / "video-001.mp4"
            with path.open("wb") as stream:
                stream.truncate(60_000_000)  # 稀疏测试文件，不是真实视频或上传
            fake = Mock()
            fake.extract_info.return_value = {"id": "fake-video"}
            fake.process_ie_result.return_value = {"id": "fake-video"}
            with patch.object(app, "YoutubeDL") as factory, patch.object(app.shutil, "disk_usage") as usage:
                usage.return_value.free = 20_000_000_000
                factory.return_value.__enter__.return_value = fake
                self.assertEqual(app.download("fake-url", folder, max_video=app.USER_MAX_VIDEO), [path])
                options = factory.call_args.args[0]
                self.assertEqual(options["max_filesize"], app.USER_MAX_VIDEO)
                options["progress_hooks"][0]({"downloaded_bytes": 60_000_000})
            client = Mock()
            client.send_file.return_value = SimpleNamespace(id=101)
            account = app.TelegramUser(client, 1, (-2, "fake-input-peer"))
            self.assertEqual(account.upload(-2, path, "https://x.com/i/status/123"), 101)
            self.assertEqual(client.send_file.call_args.args[0], "fake-input-peer")
            self.assertEqual(client.send_file.call_args.kwargs["caption"], "https://x.com/i/status/123")
        finally:
            app.clean_job(folder)

    def test_personal_upload_rejects_wrong_destination(self):
        client = Mock()
        account = app.TelegramUser(client, 1, (-2, "fake-input-peer"))
        with self.assertRaises(app.SafeError):
            account.upload(-3, Mock(), "caption")
        client.send_file.assert_not_called()

    def test_personal_network_failure_is_uncertain(self):
        client = Mock()
        client.send_file.side_effect = OSError("DO_NOT_LOG_PRIVATE_DATA")
        path = Mock()
        path.stat.return_value.st_size = 60_000_000
        path.suffix = ".mp4"
        account = app.TelegramUser(client, 1, (-2, "peer"))
        with self.assertRaises(app.Uncertain) as caught:
            account.upload(-2, path, "caption")
        self.assertNotIn("DO_NOT_LOG", str(caught.exception))

    def test_personal_download_limit_reaches_processing(self):
        account = Mock()
        account.upload.return_value = 10
        app.enqueue(self.db, "123", "url")
        with patch.object(app, "download", side_effect=self.fake_download) as download:
            app.process_one(self.db, account, self.settings, max_video=app.USER_MAX_VIDEO)
        self.assertEqual(download.call_args.kwargs["max_video"], app.USER_MAX_VIDEO)
        self.assertFalse(self.folder.exists())

    def test_saved_messages_only_accepts_owner_links(self):
        for out, sender, text in ((False, 1, "https://x.com/u/status/123"),
                                  (True, 2, "https://x.com/u/status/123"),
                                  (True, 1, "上传完成，已附原帖链接。"),
                                  (True, 1, "已加入下载队列。"),
                                  (True, 1, "已加入下载队列。\nhttps://x.com/i/status/123")):
            message = SimpleNamespace(out=out, sender_id=sender, raw_text=text)
            self.assertEqual(app.accept_saved_message(self.db, message, 1), [])
        message = SimpleNamespace(out=True, sender_id=1, raw_text="https://x.com/u/status/123")
        self.assertEqual(len(app.accept_saved_message(self.db, message, 1)), 1)
        self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "queued")

    def test_saved_messages_does_not_auto_retry_uncertain(self):
        app.enqueue(self.db, "123", "url")
        self.db.execute("UPDATE jobs SET status='uncertain'")
        message = SimpleNamespace(out=True, sender_id=1, raw_text="https://x.com/u/status/123")
        self.assertIn("/retry", app.accept_saved_message(self.db, message, 1)[0])
        message.raw_text = "/retry https://x.com/u/status/123"
        self.assertIn("已加入", app.accept_saved_message(self.db, message, 1)[0])

    def test_personal_notifications_reply_to_source_without_changing_unthreaded_notices(self):
        client = Mock()
        account = app.TelegramUser(client, 1, (-2, "peer"))
        account.say(1, "已加入下载队列。", reply_to=101)
        account.say(1, "上传完成，已附原帖链接。")
        self.assertEqual(client.send_message.call_args_list, [
            call("me", "已加入下载队列。", parse_mode=None, link_preview=False, reply_to=101),
            call("me", "上传完成，已附原帖链接。", parse_mode=None, link_preview=False),
        ])

    def test_personal_account_must_match_paired_owner(self):
        client = Mock()
        client.is_user_authorized.return_value = True
        client.get_me.return_value = SimpleNamespace(id=999, bot=False)
        with patch.object(app, "account_credentials", return_value=(12345, "a" * 32, Mock())):
            with patch("telethon.sync.TelegramClient", return_value=client):
                with self.assertRaises(app.SafeError) as caught:
                    app.open_user_account(self.settings)
        self.assertIn("不一致", str(caught.exception))
        client.disconnect.assert_called_once()
        client.send_file.assert_not_called()

    def test_personal_http_proxy_reaches_client(self):
        for value, host in (("http://proxy.example.test:7890", "proxy.example.test"),
                            ("http://[::1]:7890", "::1")):
            client = Mock()
            client.is_user_authorized.return_value = True
            client.get_me.return_value = SimpleNamespace(id=1, bot=False)
            config = Mock()
            config.exists.return_value = True
            with self.subTest(proxy=value), patch.dict(app.os.environ, {"ARCHIVER_PROXY_URL": value}):
                with patch.object(app, "import_module") as dependency:
                    with patch.object(app, "account_credentials", return_value=(12345, "a" * 32, config)):
                        with patch.object(app, "resolve_user_target", return_value=(-2, "peer")):
                            with patch("telethon.sync.TelegramClient", return_value=client) as factory:
                                account, loop = app.open_user_account(self.settings)
                                try:
                                    self.assertIs(account.client, client)
                                    dependency.assert_called_once_with("python_socks.async_.asyncio")
                                    self.assertEqual(factory.call_args.kwargs["proxy"],
                                                     {"proxy_type": "http", "addr": host,
                                                      "port": 7890, "rdns": True})
                                    client.connect.assert_called_once()
                                finally:
                                    client.disconnect()
                                    loop.close()

    def test_personal_proxy_invalid_or_https_stops_before_credentials_or_connection(self):
        for value in ("https://proxy.example.test:8443",
                      "http://user:FAKE_TEST_CREDENTIAL@proxy.example.test:7890"):
            with self.subTest(proxy=value), patch.dict(app.os.environ, {"ARCHIVER_PROXY_URL": value}):
                with patch.object(app, "account_credentials") as credentials:
                    with patch("telethon.sync.TelegramClient") as factory:
                        with self.assertRaises(app.SafeError) as caught:
                            app.open_user_account(self.settings, interactive=True)
                        credentials.assert_not_called()
                        factory.assert_not_called()
                        self.assertNotIn("FAKE_TEST_CREDENTIAL", str(caught.exception))
                        self.assertNotIn("proxy.example.test", str(caught.exception))

    def test_personal_missing_proxy_dependency_stops_without_direct_fallback(self):
        with patch.dict(app.os.environ, {"ARCHIVER_PROXY_URL": "http://proxy.example.test:7890"}):
            with patch.object(app, "import_module", side_effect=ImportError("FAKE_TEST_CREDENTIAL")):
                with patch.object(app, "account_credentials") as credentials:
                    with patch("telethon.sync.TelegramClient") as factory:
                        with self.assertRaises(app.SafeError) as caught:
                            app.open_user_account(self.settings)
                        self.assertIn("python-socks", str(caught.exception))
                        self.assertNotIn("FAKE_TEST_CREDENTIAL", str(caught.exception))
                        credentials.assert_not_called()
                        factory.assert_not_called()

    def test_personal_proxy_connect_failure_is_hidden_without_direct_fallback(self):
        client = Mock()
        client.connect.side_effect = OSError("FAKE_TEST_CREDENTIAL proxy.example.test")
        client.disconnect.side_effect = OSError("FAKE_TEST_CREDENTIAL cleanup")
        with patch.dict(app.os.environ, {"ARCHIVER_PROXY_URL": "http://proxy.example.test:7890"}):
            with patch.object(app, "import_module"):
                with patch.object(app, "account_credentials", return_value=(12345, "a" * 32, Mock())):
                    with patch("telethon.sync.TelegramClient", return_value=client) as factory:
                        with self.assertRaises(app.SafeError) as caught:
                            app.open_user_account(self.settings)
                        self.assertNotIn("FAKE_TEST_CREDENTIAL", str(caught.exception))
                        self.assertNotIn("proxy.example.test", str(caught.exception))
                        factory.assert_called_once()
                        self.assertIsNotNone(factory.call_args.kwargs["proxy"])
                        client.connect.assert_called_once()
                        client.disconnect.assert_called_once()

    def test_personal_client_construction_failure_is_hidden(self):
        with patch.dict(app.os.environ, {"ARCHIVER_PROXY_URL": ""}):
            with patch.object(app, "account_credentials", return_value=(12345, "a" * 32, Mock())):
                with patch("telethon.sync.TelegramClient", side_effect=RuntimeError("FAKE_TEST_CREDENTIAL")):
                    with self.assertRaises(app.SafeError) as caught:
                        app.open_user_account(self.settings)
                    self.assertNotIn("FAKE_TEST_CREDENTIAL", str(caught.exception))

    def test_hidden_input_refuses_echo_fallback(self):
        def unavailable_getpass(prompt):
            app.warnings.warn("FAKE_TEST_CREDENTIAL", app.getpass.GetPassWarning)
            self.fail("getpass must stop before its echoing fallback")

        with patch.object(app.sys.stdin, "isatty", return_value=True):
            with patch.object(app.getpass, "getpass", side_effect=unavailable_getpass):
                with self.assertRaises(app.SafeError) as caught:
                    app.hidden_input("fake-prompt")
                self.assertNotIn("FAKE_TEST_CREDENTIAL", str(caught.exception))
        with patch.object(app.sys.stdin, "isatty", return_value=False):
            with patch.object(app.getpass, "getpass") as prompt:
                with self.assertRaises(app.SafeError):
                    app.hidden_input("fake-prompt")
                prompt.assert_not_called()

    def test_headless_personal_mode_never_starts_login(self):
        client = Mock()
        client.is_user_authorized.return_value = False
        with patch.object(app, "account_credentials", return_value=(12345, "a" * 32, Mock())):
            with patch("telethon.sync.TelegramClient", return_value=client):
                with self.assertRaises(app.SafeError) as caught:
                    app.open_user_account(self.settings)
        self.assertIn("login-user", str(caught.exception))
        client.send_code_request.assert_not_called()
        client.sign_in.assert_not_called()

    def test_personal_target_requires_channel_post_permission(self):
        client = Mock()
        dialog = SimpleNamespace(id=-2, entity=SimpleNamespace(broadcast=True, creator=False, admin_rights=None), input_entity="peer")
        client.iter_dialogs.return_value = [dialog]
        with self.assertRaises(app.SafeError):
            app.resolve_user_target(client, -2)

    def test_personal_mode_initial_baseline_starts_async_tasks_and_closes_database(self):
        account = Mock()
        account.client.get_messages.return_value = [SimpleNamespace(id=100)]
        loop = app.asyncio.new_event_loop()
        self.addCleanup(loop.close)
        account.client.loop = loop
        proxy = Mock(wraps=self.db)
        proxy.close = Mock()  # 保留内存数据库，以核对退出时已落盘的游标和队列
        with patch.object(app, "database", return_value=proxy):
            with patch.object(app, "run_user_tasks", new_callable=AsyncMock,
                              side_effect=app.SafeError("test stop")) as tasks:
                with self.assertRaises(app.SafeError):
                    app.run_user(account, self.settings)
        self.assertEqual(self.db.execute("SELECT value FROM state WHERE key='user_offset'").fetchone()[0], 100)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        tasks.assert_awaited_once_with(proxy, account, self.settings)
        account.client.get_messages.assert_called_once_with("me", limit=1)
        proxy.close.assert_called_once()

    def test_personal_queue_replies_keep_each_source_and_multiple_post_links(self):
        account = Mock()
        account.client.get_messages = AsyncMock(side_effect=[
            [SimpleNamespace(id=101, out=True, sender_id=1,
                             raw_text="https://twitter.com/a/status/123?s=20 https://x.com/b/status/456"),
             SimpleNamespace(id=102, out=True, sender_id=1,
                             raw_text="https://x.com/c/status/789")], RuntimeError("test stop"),
        ])
        account.say_async = AsyncMock()
        account.create_status_async = AsyncMock(side_effect=[501, 502, 503])
        account.edit_status_async = AsyncMock()
        self.db.execute("INSERT INTO state VALUES ('user_offset',100)")
        with patch.object(app.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaises(app.SafeError):
                app.asyncio.run(app.receive_saved_messages(self.db, account, self.settings, app.asyncio.Event()))
        created = account.create_status_async.await_args_list
        self.assertEqual([item.kwargs["reply_to"] for item in created], [101, 101, 102])
        self.assertTrue(all(item.args[1].startswith("已加入下载队列。") for item in created))
        self.assertIn("https://x.com/i/status/123", created[0].args[1])
        self.assertIn("https://x.com/i/status/456", created[1].args[1])
        account.say_async.assert_not_awaited()
        self.assertEqual(self.db.execute("SELECT message_id FROM job_status_messages ORDER BY post_id").fetchall(),
                         [(501,), (502,), (503,)])
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT value FROM state WHERE key='user_offset'").fetchone()[0], 102)

    def test_personal_service_interrupt_drains_download_before_closing_database(self):
        loop = app.asyncio.new_event_loop()
        self.addCleanup(loop.close)
        client = SimpleNamespace(loop=loop, get_messages=AsyncMock(return_value=[]),
                                 send_message=AsyncMock(), send_file=AsyncMock())
        account = app.TelegramUser(client, 1, (-2, "fake-peer"))
        self.db.execute("INSERT INTO state VALUES ('user_offset',100)")
        app.enqueue(self.db, "123", "url", source_message_id=101)
        started, stopped = app.threading.Event(), app.threading.Event()

        def download(url, folder, *, cancel_event, **kwargs):
            started.set()
            if not cancel_event.wait(3):
                raise AssertionError("下载线程未停止")
            (folder / "last.part").write_bytes(b"fake")
            stopped.set()
            raise app.SafeError("下载已停止。")

        deadline = loop.time() + 2

        def interrupt_when_started():
            if not started.is_set() and loop.time() < deadline:
                loop.call_later(0.01, interrupt_when_started)
            else:
                app.stop_requested(15, None)

        proxy = Mock(wraps=self.db)
        proxy.close = Mock()
        loop.call_soon(interrupt_when_started)
        with patch.object(app, "database", return_value=proxy), patch.object(app, "download", side_effect=download):
            with self.assertRaises(KeyboardInterrupt):
                app.run_user(account, self.settings)
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "uncertain")
        self.assertEqual(list((app.DATA / "jobs").iterdir()), [])
        self.assertFalse(app.asyncio.all_tasks(loop))
        proxy.close.assert_called_once()


class LocalProxyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_telethon_opens_http_connect_through_real_proxy_backend(self):
        """真实本地 CONNECT 握手；不连接 Telegram，也不创建登录会话。"""
        from telethon.network.connection.tcpfull import ConnectionTcpFull

        captured = []
        handler_done = app.asyncio.Event()

        async def proxy_handler(reader, writer):
            try:
                header = await app.asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
                captured.append(header)
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                handler_done.set()

        try:
            server = await app.asyncio.start_server(proxy_handler, "127.0.0.1", 0)
        except PermissionError:
            self.skipTest("当前沙箱不允许本地环回 socket；需在可用终端执行本项集成测试。")
        connection = None
        try:
            port = server.sockets[0].getsockname()[1]
            with patch.dict(app.os.environ, {"ARCHIVER_PROXY_URL": f"http://127.0.0.1:{port}"}):
                connection = ConnectionTcpFull("203.0.113.77", 443, 1,
                                               loggers=defaultdict(lambda: app.logging.getLogger(__name__)),
                                               proxy=app.telegram_user_proxy())
            original_open = app.asyncio.open_connection

            async def tunneled_socket_only(*args, **kwargs):
                # 连接目标为文档保留地址，并在任何直连尝试前失败。
                self.assertFalse(args)
                self.assertIsNotNone(kwargs.get("sock"), "禁止直连：必须使用已完成代理握手的 socket")
                return await original_open(*args, **kwargs)

            with patch.object(app.asyncio, "open_connection", side_effect=tunneled_socket_only):
                await connection._connect(timeout=3)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].split(b"\r\n", 1)[0], b"CONNECT 203.0.113.77:443 HTTP/1.1")
            self.assertNotIn(b"proxy-authorization", captured[0].lower())
        finally:
            if connection is not None and connection._writer is not None:
                connection._writer.close()
                await connection._writer.wait_closed()
            server.close()
            await server.wait_closed()
            if captured:
                await app.asyncio.wait_for(handler_done.wait(), 3)


if __name__ == "__main__":
    unittest.main()
