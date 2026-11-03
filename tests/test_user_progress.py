"""可编辑进度消息的离线测试；只使用临时数据库、假消息ID及假视频。"""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors

import archiver as app


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class UserProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="editable-progress-offline-")
        self.addCleanup(temporary.cleanup)
        data_patch = patch.object(app, "DATA", Path(temporary.name))
        data_patch.start()
        self.addCleanup(data_patch.stop)
        (app.DATA / "jobs").mkdir()
        self.db = app.database(app.DATA / "queue.sqlite3")
        self.addCleanup(self.db.close)
        self.clock = FakeClock()
        self.next_message_id = 500

        async def create_status(owner, text, **options):
            # 让同一轮事件循环中的其它创建请求有机会到达，检验创建锁。
            await asyncio.sleep(0)
            self.next_message_id += 1
            return self.next_message_id

        self.account = SimpleNamespace(
            create_status_async=AsyncMock(side_effect=create_status),
            edit_status_async=AsyncMock(),
            say_async=AsyncMock(),
            upload_async=AsyncMock(return_value=10),
        )
        app.enqueue(self.db, "123", "https://x.com/i/status/123", source_message_id=101)
        self.manager = app.UserProgressManager(self.db, self.account, 1, clock=self.clock)

    def status_message_id(self):
        row = self.db.execute("SELECT message_id FROM job_status_messages WHERE post_id='123'").fetchone()
        return row[0] if row else None

    async def create_initial(self):
        state = self.manager.get("123")
        await state.flush(force=True)
        return state

    async def test_one_reply_is_reused_through_all_stages(self):
        state = await self.create_initial()
        self.assertEqual(self.status_message_id(), 501)
        created = self.account.create_status_async.await_args
        self.assertEqual(created.args[0], 1)
        self.assertIn("已加入下载队列", created.args[1])
        self.assertEqual(created.kwargs.get("reply_to"), 101)

        state.report("download", "download 65%")
        await state.flush()
        state.report("upload", "upload 12%")
        await state.flush()
        await state.finish("finished successfully")

        self.account.create_status_async.assert_awaited_once()
        self.assertGreaterEqual(self.account.edit_status_async.await_count, 3)
        for edited in self.account.edit_status_async.await_args_list:
            self.assertEqual(edited.args[0], 501)
            self.assertNotEqual(edited.args[0], 101, "不能把用户原消息当作编辑目标")
        self.assertIn("download 65%", self.account.edit_status_async.await_args_list[0].args[1])
        self.assertIn("upload 12%", self.account.edit_status_async.await_args_list[1].args[1])
        self.assertIn("finished successfully", self.account.edit_status_async.await_args_list[-1].args[1])

    async def test_concurrent_initial_flush_creates_only_one_reply(self):
        state = self.manager.get("123")
        self.assertIs(self.manager.get("123"), state)
        await asyncio.gather(state.flush(force=True), state.flush(force=True),
                             self.manager.get("123").flush(force=True))
        self.account.create_status_async.assert_awaited_once()
        self.assertEqual(self.status_message_id(), 501)

    async def test_same_stage_is_throttled_and_identical_text_is_not_edited(self):
        state = await self.create_initial()
        state.report("download", "download 65%")
        await state.flush()
        self.assertEqual(self.account.edit_status_async.await_count, 1)

        self.clock.advance(1)
        state.report("download", "download 66%")
        await state.flush(force=True)
        self.assertEqual(self.account.edit_status_async.await_count, 1)
        self.clock.advance(8)
        state.report("download", "download 67%")
        await self.manager.flush_all()
        self.assertEqual(self.account.edit_status_async.await_count, 1)

        self.clock.advance(1)
        state.report("download", "download 68%")
        await self.manager.flush_all()
        self.assertEqual(self.account.edit_status_async.await_count, 2)
        self.assertIn("download 68%", self.account.edit_status_async.await_args.args[1])
        self.clock.advance(30)
        state.report("download", "download 68%")
        await state.flush()
        self.assertEqual(self.account.edit_status_async.await_count, 2)

    async def test_phase_and_terminal_update_immediately_and_reject_late_progress(self):
        state = await self.create_initial()
        state.report("download", "download 65%")
        await state.flush()
        self.clock.advance(1)
        state.report("upload", "upload 0%")
        await state.flush()
        self.assertIn("upload 0%", self.account.edit_status_async.await_args.args[1])
        await state.finish("final confirmation")
        count = self.account.edit_status_async.await_count
        self.assertIn("final confirmation", self.account.edit_status_async.await_args.args[1])

        self.clock.advance(100)
        state.report("download", "late download 99%")
        await state.flush(force=True)
        self.assertEqual(self.account.edit_status_async.await_count, count)
        self.assertIn("final confirmation", self.account.edit_status_async.await_args.args[1])

    async def test_restart_restores_reply_id_without_creating_another_message(self):
        await self.create_initial()
        restarted = app.UserProgressManager(self.db, self.account, 1, clock=self.clock)
        state = restarted.get("123")
        state.report("upload", "resumed upload 25%")
        await state.flush(force=True)
        self.account.create_status_async.assert_awaited_once()
        self.assertEqual(self.account.edit_status_async.await_args.args[0], 501)
        self.assertIn("resumed upload 25%", self.account.edit_status_async.await_args.args[1])

    async def test_duplicate_request_keeps_original_reply_and_explicit_retry_gets_new_reply(self):
        original = await self.create_initial()
        for status in ("queued", "processing", "success", "uncertain"):
            with self.subTest(status=status):
                self.db.execute("UPDATE jobs SET status=? WHERE post_id='123'", (status,))
                app.enqueue(self.db, "123", "https://x.com/i/status/123", source_message_id=102)
                self.assertEqual(self.status_message_id(), 501)
                self.assertEqual(self.db.execute("SELECT message_id FROM job_sources WHERE post_id='123'").fetchone()[0], 101)

        app.enqueue(self.db, "123", "https://x.com/i/status/123", retry=True, source_message_id=102)
        self.assertIsNone(self.status_message_id())
        retried = self.manager.get("123")
        self.assertIsNot(retried, original)
        await retried.flush(force=True)
        self.assertEqual(self.account.create_status_async.await_count, 2)
        self.assertEqual(self.account.create_status_async.await_args.kwargs.get("reply_to"), 102)
        self.assertEqual(self.status_message_id(), 502)

    async def test_old_job_without_source_does_not_invent_a_reply_target(self):
        app.enqueue(self.db, "456", "https://x.com/i/status/456")
        state = self.manager.get("456")
        await state.flush(force=True)
        self.assertIsNone(self.account.create_status_async.await_args.kwargs.get("reply_to"))
        self.assertIsNotNone(self.db.execute("SELECT message_id FROM job_status_messages WHERE post_id='456'").fetchone())

    async def test_late_old_attempt_creation_cannot_overwrite_retry_reply_id(self):
        old_started, release_old = asyncio.Event(), asyncio.Event()

        async def create_status(owner, text, **options):
            if options.get("reply_to") == 101:
                old_started.set()
                await release_old.wait()
                return 401
            self.assertEqual(options.get("reply_to"), 102)
            return 402

        self.account.create_status_async.side_effect = create_status
        old = self.manager.get("123")
        old.report("done", "old attempt failed", terminal=True)
        old_creation = asyncio.create_task(old.flush(force=True))
        try:
            await asyncio.wait_for(old_started.wait(), 2)
            self.db.execute("UPDATE jobs SET status='failed' WHERE post_id='123'")
            app.enqueue(self.db, "123", "https://x.com/i/status/123", retry=True, source_message_id=102)
            retried = self.manager.get("123")
            self.assertIsNot(retried, old)
            await retried.flush(force=True)
            self.assertEqual(self.status_message_id(), 402)
            release_old.set()
            await asyncio.wait_for(old_creation, 2)
        finally:
            release_old.set()
            if not old_creation.done():
                old_creation.cancel()
            await asyncio.gather(old_creation, return_exceptions=True)
        self.assertEqual(self.status_message_id(), 402)
        self.clock.advance(30)
        await old.flush(force=True)
        self.assertEqual(self.status_message_id(), 402)
        self.assertEqual(self.account.create_status_async.await_count, 2)
        self.assertNotIn(old, self.manager.states.values())

    async def test_flood_wait_defers_all_edits_including_phase_changes(self):
        state = await self.create_initial()
        self.account.edit_status_async.side_effect = [errors.FloodWaitError(request=None, capture=30), None]
        state.report("download", "download 10%")
        await state.flush()
        self.assertEqual(self.account.edit_status_async.await_count, 1)

        self.clock.advance(10)
        state.report("upload", "upload 20%")
        await state.flush(force=True)
        self.assertEqual(self.account.edit_status_async.await_count, 1)
        self.clock.advance(21)
        await state.flush()
        self.assertEqual(self.account.edit_status_async.await_count, 2)
        self.assertIn("upload 20%", self.account.edit_status_async.await_args.args[1])

    async def test_message_not_modified_is_treated_as_success(self):
        state = await self.create_initial()
        self.account.edit_status_async.side_effect = errors.MessageNotModifiedError(request=None)
        state.report("download", "download 10%")
        await state.flush()
        self.clock.advance(20)
        await state.flush()
        self.assertEqual(self.account.edit_status_async.await_count, 1)

    async def test_flood_wait_on_one_status_also_defers_other_status_messages(self):
        state = await self.create_initial()
        self.account.edit_status_async.side_effect = errors.FloodWaitError(request=None, capture=30)
        state.report("download", "download 10%")
        await state.flush()
        app.enqueue(self.db, "456", "https://x.com/i/status/456", source_message_id=102)
        other = self.manager.get("456")
        other.report("parsing", "another job is parsing")
        self.clock.advance(10)
        await other.flush(force=True)
        self.account.create_status_async.assert_awaited_once()
        self.clock.advance(21)
        await other.flush(force=True)
        self.assertEqual(self.account.create_status_async.await_count, 2)
        self.assertIn("another job is parsing", self.account.create_status_async.await_args.args[1])

    async def test_message_not_modified_does_not_discard_new_progress_received_while_editing(self):
        state = await self.create_initial()
        edit_started, release_edit = asyncio.Event(), asyncio.Event()

        async def edit(message_id, text):
            edit_started.set()
            await release_edit.wait()
            raise errors.MessageNotModifiedError(request=None)

        self.account.edit_status_async.side_effect = edit
        state.report("download", "download 10%")
        flushing = asyncio.create_task(state.flush())
        try:
            await asyncio.wait_for(edit_started.wait(), 2)
            state.report("upload", "newer upload 20%")
            release_edit.set()
            await asyncio.wait_for(flushing, 2)
        finally:
            release_edit.set()
            if not flushing.done():
                flushing.cancel()
            await asyncio.gather(flushing, return_exceptions=True)
        self.account.edit_status_async.side_effect = None
        await self.manager.flush_all()
        self.assertEqual(self.account.edit_status_async.await_count, 2)
        self.assertIn("newer upload 20%", self.account.edit_status_async.await_args.args[1])

    async def test_edit_error_never_changes_job_or_upload_confirmation_records(self):
        state = await self.create_initial()
        self.account.edit_status_async.side_effect = RuntimeError("FAKE_PRIVATE_ERROR")
        state.report("download", "download 10%")
        await state.flush()
        await state.finish("upload result already confirmed")
        self.assertEqual(self.db.execute("SELECT status FROM jobs WHERE post_id='123'").fetchone()[0], "queued")
        self.assertEqual(self.db.execute("SELECT count(*) FROM sent").fetchone()[0], 0)

    async def test_deleted_status_message_is_not_recreated_on_every_update(self):
        state = await self.create_initial()
        self.account.edit_status_async.side_effect = errors.MessageIdInvalidError(request=None)
        state.report("download", "download 10%")
        await state.flush()
        self.clock.advance(30)
        state.report("upload", "upload 20%")
        await state.flush(force=True)
        await state.finish("done")
        self.account.create_status_async.assert_awaited_once()
        self.assertEqual(self.account.edit_status_async.await_count, 1)
        await self.manager.flush_all()
        self.assertNotIn(state, self.manager.states.values())

    @staticmethod
    def fake_download(url, folder, **kwargs):
        path = folder / "video-001.mp4"
        path.write_bytes(b"f" * 100)
        return [path]

    async def test_edit_failure_does_not_turn_confirmed_upload_into_failure(self):
        await self.create_initial()
        self.account.edit_status_async.side_effect = RuntimeError("FAKE_PRIVATE_ERROR")
        with patch.object(app, "download", side_effect=self.fake_download):
            await app.process_one_user(self.db, self.account, {"owner_id": 1, "chat_id": -2},
                                       progress_manager=self.manager)
        self.account.upload_async.assert_awaited_once()
        self.assertEqual(self.db.execute("SELECT status FROM jobs WHERE post_id='123'").fetchone()[0], "success")
        self.assertEqual(self.db.execute("SELECT message_id FROM sent WHERE post_id='123'").fetchall(), [(10,)])
        self.assertEqual(list((app.DATA / "jobs").iterdir()), [])
        self.account.create_status_async.assert_awaited_once()
        self.assertNotIn("FAKE_PRIVATE_ERROR", str(self.account.create_status_async.await_args_list))

    async def test_uncertain_initial_create_does_not_spam_and_media_still_succeeds(self):
        for index, failure in enumerate((RuntimeError("FAKE_STATUS_NETWORK_ERROR"), None)):
            with self.subTest(failure=type(failure).__name__):
                post_id = str(123 + index)
                if index:
                    app.enqueue(self.db, post_id, f"https://x.com/i/status/{post_id}", source_message_id=102)
                self.account.create_status_async.side_effect = failure
                self.account.create_status_async.return_value = None
                previous_creations = self.account.create_status_async.await_count
                state = self.manager.get(post_id)
                await state.flush(force=True)
                self.assertEqual(self.account.create_status_async.await_count, previous_creations + 1)

                self.clock.advance(60)
                state.report("download", "later download 65%")
                await state.flush(force=True)
                with patch.object(app, "download", side_effect=self.fake_download):
                    await app.process_one_user(self.db, self.account, {"owner_id": 1, "chat_id": -2},
                                               progress_manager=self.manager)
                self.clock.advance(60)
                state.report("upload", "late progress 99%")
                await state.flush(force=True)
                await state.finish("late completion")
                await self.manager.flush_all()

                self.assertEqual(self.account.create_status_async.await_count, previous_creations + 1)
                self.assertEqual(self.db.execute("SELECT status FROM jobs WHERE post_id=?", (post_id,)).fetchone()[0], "success")
                self.assertEqual(self.db.execute("SELECT count(*) FROM sent WHERE post_id=?", (post_id,)).fetchone()[0], 1)
                self.assertIsNone(self.db.execute("SELECT message_id FROM job_status_messages WHERE post_id=?", (post_id,)).fetchone())
                self.assertEqual(self.account.upload_async.await_count, index + 1)
                self.assertEqual(list((app.DATA / "jobs").iterdir()), [])

    async def test_upload_full_bytes_waits_for_message_confirmation_before_completion(self):
        await self.create_initial()
        reached_full_bytes, release_confirmation = asyncio.Event(), asyncio.Event()

        async def upload(chat, path, caption, thread=None, *, progress_callback):
            progress_callback(100, 100)
            self.clock.advance(10)
            await self.manager.flush_all()
            reached_full_bytes.set()
            await release_confirmation.wait()
            return 10

        self.account.upload_async.side_effect = upload
        with patch.object(app, "download", side_effect=self.fake_download):
            worker = asyncio.create_task(app.process_one_user(
                self.db, self.account, {"owner_id": 1, "chat_id": -2}, progress_manager=self.manager))
            try:
                await asyncio.wait_for(reached_full_bytes.wait(), 2)
                text = self.account.edit_status_async.await_args.args[1]
                self.assertIn("确认", text)
                self.assertNotIn("上传完成", text)
                self.assertEqual(self.db.execute("SELECT status FROM jobs WHERE post_id='123'").fetchone()[0], "processing")
                self.assertEqual(self.db.execute("SELECT count(*) FROM sent").fetchone()[0], 0)
                release_confirmation.set()
                await asyncio.wait_for(worker, 2)
            finally:
                release_confirmation.set()
                if not worker.done():
                    worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(self.db.execute("SELECT status FROM jobs WHERE post_id='123'").fetchone()[0], "success")
        self.assertEqual(self.db.execute("SELECT count(*) FROM sent").fetchone()[0], 1)
        self.assertIn("上传完成", self.account.edit_status_async.await_args.args[1])
        self.account.create_status_async.assert_awaited_once()

    async def test_download_text_identifies_stream_and_video_without_claiming_global_percentage(self):
        video = app.download_progress_text({
            "downloaded_bytes": 65, "total_bytes": 100,
            "filename": "FAKE_PRIVATE_FILENAME",
            "info_dict": {"playlist_index": 1, "n_entries": 2, "acodec": "none", "url": "FAKE_PRIVATE_URL"},
        })
        audio = app.download_progress_text({
            "downloaded_bytes": 10, "total_bytes": 100,
            "info_dict": {"playlist_index": 1, "n_entries": 2, "vcodec": "none"},
        })
        self.assertIn("1/2", video)
        self.assertIn("视频流", video)
        self.assertIn("65%", video)
        self.assertIn("1/2", audio)
        self.assertIn("音轨", audio)
        self.assertIn("10%", audio)
        self.assertNotIn("FAKE_PRIVATE", video)

    async def test_unknown_and_estimated_download_sizes_are_honest(self):
        unknown = app.download_progress_text({"downloaded_bytes": 2_000_000})
        estimated = app.download_progress_text({"downloaded_bytes": 2_000_000, "total_bytes_estimate": 4_000_000})
        self.assertIn("2.0 MB", unknown)
        self.assertNotIn("%", unknown)
        self.assertIn("50%", estimated)
        self.assertIn("约", estimated)
        for invalid in (None, float("nan"), -1, True, "FAKE_PRIVATE_ERROR"):
            with self.subTest(invalid=invalid):
                self.assertNotIn("FAKE_PRIVATE", app.transfer_progress_text(invalid, 100))

    async def test_download_progress_storm_keeps_only_latest_pending_update(self):
        callbacks = []
        loop = SimpleNamespace(call_soon_threadsafe=lambda callback: callbacks.append(callback))
        state = SimpleNamespace(report=Mock())
        bridge = app.DownloadProgressBridge(loop, state)
        for number in range(10_000):
            bridge("download", f"safe progress {number}")
        self.assertEqual(len(callbacks), 1)
        callbacks.pop()()
        state.report.assert_called_once_with("download", "safe progress 9999")
        bridge("download", "late progress")
        self.assertEqual(len(callbacks), 1)
        bridge.close()
        callbacks.pop()()
        state.report.assert_called_once()


if __name__ == "__main__":
    unittest.main()
