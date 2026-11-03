"""首次设置的离线测试；仅使用假账号和独占临时目录，不联网。"""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import archiver as app


def dialog(identifier=-100, name="Fake private channel", *, creator=True,
           broadcast=True, rights=None, group=False, deactivated=False):
    return SimpleNamespace(id=identifier, name=name, is_channel=not group, is_group=group,
                           input_entity=object(), entity=SimpleNamespace(
                               creator=creator, broadcast=broadcast, admin_rights=rights,
                               deactivated=deactivated, migrated_to=None))


class UserSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="public-user-setup-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        change = patch.object(app, "DATA", self.root)
        change.start()
        self.addCleanup(change.stop)
        tty = patch.object(app.sys, "stdin", Mock(isatty=Mock(return_value=True)))
        tty.start()
        self.addCleanup(tty.stop)
        self.me = SimpleNamespace(id=123, bot=False, username="fake_owner")

    def choose(self, dialogs, answers=("1", "BIND")):
        client = Mock()
        client.iter_dialogs.return_value = dialogs
        output = io.StringIO()
        with patch("builtins.input", side_effect=answers), redirect_stdout(output):
            target = app.initial_user_target(client, self.me)
        return target, output.getvalue()

    def test_choose_created_channel_explicitly(self):
        channel = dialog()
        target, output = self.choose([channel])
        self.assertEqual(target, (channel.id, channel.input_entity))
        self.assertIn("@fake_owner", output)
        self.assertIn("Fake private channel", output)

    def test_only_owned_or_manageable_targets_are_listed(self):
        read_only = dialog(-1, "Read-only", creator=False)
        no_post = dialog(-2, "No post rights", creator=False,
                         rights=SimpleNamespace(post_messages=False))
        managed = dialog(-3, "Managed", creator=False,
                         rights=SimpleNamespace(post_messages=True))
        deleted = dialog(-4, "Deleted", deactivated=True)
        target, output = self.choose([read_only, no_post, managed, deleted])
        self.assertEqual(target[0], -3)
        self.assertNotIn("Read-only", output)
        self.assertNotIn("No post rights", output)
        self.assertNotIn("Deleted", output)

    def test_owned_and_managed_groups_are_supported(self):
        unmanaged = dialog(-1, "Ordinary group", creator=False, broadcast=False, group=True)
        managed = dialog(-2, "Managed group", creator=False, broadcast=False,
                         rights=SimpleNamespace(), group=True)
        target, output = self.choose([unmanaged, managed])
        self.assertEqual(target[0], -2)
        self.assertNotIn("Ordinary group", output)

    def test_user_dialog_and_positive_target_are_rejected(self):
        user = dialog(-1, "Private user")
        user.is_channel = False
        good = dialog(-3, "Allowed")
        target, output = self.choose([user, dialog(8, "Positive"), good])
        self.assertEqual(target[0], -3)
        self.assertNotIn("Private user", output)
        self.assertNotIn("Positive", output)

    def test_unsafe_terminal_characters_in_target_name_are_removed(self):
        _, output = self.choose([dialog(name="Fake\x1b[31m\nchannel")])
        self.assertNotIn("\x1b", output)
        self.assertIn("Fake [31m channel", output)

    def test_no_eligible_target_stops_before_input(self):
        with patch("builtins.input") as prompt, redirect_stdout(io.StringIO()):
            with self.assertRaises(app.SafeError):
                app.initial_user_target(Mock(iter_dialogs=Mock(return_value=[])), self.me)
        prompt.assert_not_called()

    def test_invalid_selection_or_missing_confirmation_does_not_bind(self):
        for answer in ("0", "2", "name", "１２", "-1"):
            with self.subTest(answer=answer), redirect_stdout(io.StringIO()):
                with self.assertRaises(app.SafeError):
                    self.choose([dialog()], (answer,))
        for confirmation in ("", "yes", "bind", "NO"):
            with self.subTest(confirmation=confirmation), redirect_stdout(io.StringIO()):
                with self.assertRaises(app.SafeError):
                    self.choose([dialog()], ("1", confirmation))
        self.assertFalse((self.root / "settings.json").exists())

    def test_settings_publication_is_atomic_private_and_preserves_siblings(self):
        sibling = self.root / "unrelated.txt"
        sibling.write_text("FAKE_UNRELATED")
        settings = {"owner_id": 123, "chat_id": -100, "mode": "user", "thread_id": None}
        app.write_user_settings(settings)
        target = self.root / "settings.json"
        self.assertEqual(json.loads(target.read_text()), settings)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(sibling.read_text(), "FAKE_UNRELATED")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["settings.json", "unrelated.txt"])

    def test_existing_or_symlink_target_never_overwritten(self):
        target = self.root / "settings.json"
        target.write_text("FAKE_OLD_BINDING")
        with self.assertRaises(app.SafeError):
            app.write_user_settings({"owner_id": 123})
        self.assertEqual(target.read_text(), "FAKE_OLD_BINDING")
        target.unlink()
        target.symlink_to(self.root / "missing")
        with self.assertRaises(app.SafeError):
            app.write_user_settings({"owner_id": 123})
        self.assertTrue(target.is_symlink())

    def test_racing_binding_is_not_overwritten_and_temporary_is_cleaned(self):
        target = self.root / "settings.json"
        real_link = os.link
        def raced(source, destination, **kwargs):
            target.write_text("FAKE_RACING_BINDING")
            return real_link(source, destination, **kwargs)
        with patch.object(app.os, "link", side_effect=raced):
            with self.assertRaises(app.SafeError):
                app.write_user_settings({"owner_id": 123})
        self.assertEqual(target.read_text(), "FAKE_RACING_BINDING")
        self.assertEqual([p.name for p in self.root.iterdir()], ["settings.json"])

    def test_serialization_failure_leaves_no_partial_target(self):
        with self.assertRaises(TypeError):
            app.write_user_settings({"owner_id": object()})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_setup_refuses_noninteractive_or_existing_binding_before_login(self):
        with patch.object(app, "open_user_account") as login:
            with patch.object(app.sys.stdin, "isatty", return_value=False):
                with self.assertRaises(app.SafeError):
                    app.setup_user()
            (self.root / "settings.json").write_text("FAKE_BINDING")
            with self.assertRaises(app.SafeError):
                app.setup_user()
        login.assert_not_called()

    def test_successful_setup_closes_client_and_loop(self):
        client, loop = Mock(), Mock()
        account = app.TelegramUser(client, 123, (-100, object()))
        with patch.object(app, "open_user_account", return_value=(account, loop)) as login:
            with redirect_stdout(io.StringIO()):
                app.setup_user()
        login.assert_called_once_with(None, interactive=True)
        self.assertEqual(json.loads((self.root / "settings.json").read_text())["chat_id"], -100)
        client.disconnect.assert_called_once_with()
        loop.close.assert_called_once_with()

    def test_publication_failure_still_closes_login(self):
        client, loop = Mock(), Mock()
        account = app.TelegramUser(client, 123, (-100, object()))
        with patch.object(app, "open_user_account", return_value=(account, loop)):
            with patch.object(app, "write_user_settings", side_effect=OSError("FAKE_FAILURE")):
                with self.assertRaises(OSError):
                    app.setup_user()
        client.disconnect.assert_called_once_with()
        loop.close.assert_called_once_with()

    def test_unconfigured_noninteractive_login_never_reads_credentials(self):
        with patch.object(app, "account_credentials") as credentials:
            with self.assertRaises(app.SafeError):
                app.open_user_account(None, interactive=False)
        credentials.assert_not_called()

    def test_full_mocked_first_login_checks_owner_and_target_without_bot(self):
        from telethon.sync import TelegramClient
        client = Mock()
        client.session = SimpleNamespace(save_entities=True)
        client.is_user_authorized.return_value = True
        client.get_me.return_value = self.me
        channel = dialog()
        client.iter_dialogs.return_value = [channel]
        private = self.root / "private"
        private.mkdir(mode=0o700)
        config = private / "telegram_api.json"
        with patch("telethon.sync.TelegramClient", return_value=client):
            with patch.object(app, "account_credentials", return_value=(777, "a" * 32, config)):
                with patch.object(app, "telegram_user_proxy", return_value=None):
                    with patch("builtins.input", side_effect=["1", "BIND"]), redirect_stdout(io.StringIO()):
                        app.setup_user()
        self.assertEqual(json.loads((self.root / "settings.json").read_text())["owner_id"], 123)
        self.assertFalse(client.session.save_entities)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        client.send_code_request.assert_not_called()
        client.disconnect.assert_called_once_with()

    def test_existing_bound_account_mismatch_still_fails(self):
        client = Mock()
        client.session = SimpleNamespace(save_entities=True)
        client.is_user_authorized.return_value = True
        client.get_me.return_value = self.me
        private = self.root / "private"
        private.mkdir(mode=0o700)
        with patch("telethon.sync.TelegramClient", return_value=client):
            with patch.object(app, "account_credentials", return_value=(777, "a" * 32, private / "telegram_api.json")):
                with patch.object(app, "telegram_user_proxy", return_value=None):
                    with self.assertRaises(app.SafeError):
                        app.open_user_account({"owner_id": 999, "chat_id": -100})
        client.iter_dialogs.assert_not_called()
        client.disconnect.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
