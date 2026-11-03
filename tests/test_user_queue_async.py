"""并发收链接/串行上传的离线测试；只使用假消息、假视频和临时数据库。"""

import asyncio
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

import archiver as app


class UserQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="user-queue-offline-")
        self.addCleanup(temporary.cleanup)
        data_patch = patch.object(app, "DATA", Path(temporary.name))
        data_patch.start()
        self.addCleanup(data_patch.stop)
        (app.DATA / "jobs").mkdir()
        self.db = app.database(app.DATA / "queue.sqlite3")
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO state VALUES ('user_offset',100)")
        self.db.commit()
        self.settings = {"owner_id": 1, "chat_id": -2}
        self.client = SimpleNamespace(get_messages=AsyncMock(return_value=[]),
                                      send_message=AsyncMock(),
                                      edit_message=AsyncMock(),
                                      send_file=AsyncMock(return_value=SimpleNamespace(id=10)))
        self.next_message_id = 500
        self.client.send_message.side_effect = self.sent_status
        self.account = app.TelegramUser(self.client, 1, (-2, "fake-target"))

    async def sent_status(self, *args, **kwargs):
        self.next_message_id += 1
        return SimpleNamespace(id=self.next_message_id)

    @staticmethod
    def message(number, post_id=None):
        return SimpleNamespace(id=number, out=True, sender_id=1,
                               raw_text=f"https://x.com/i/status/{post_id or number}")

    def enqueue(self, post_id="123", source=101):
        app.accept_saved_message(self.db, self.message(source, post_id), 1)

    @staticmethod
    def fake_download(url, folder, **kwargs):
        path = folder / "video-001.mp4"
        path.write_bytes(b"fake-video")
        return [path]

    async def cancel(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def assert_queued_while_busy(self):
        # 26条消息跨越轮询分页；前一个任务仍未完成就必须全部持久化并回复。
        messages = [self.message(i, 2000 + i) for i in range(102, 128)]
        self.client.get_messages.side_effect = [messages[:20], messages[20:], []]
        acknowledged = asyncio.Event()
        replies = []

        async def notify(chat, text, **options):
            if text.startswith("已加入下载队列。"):
                replies.append(options.get("reply_to"))
                if len(replies) == len(messages):
                    acknowledged.set()
            return await self.sent_status(chat, text, **options)

        self.client.send_message.side_effect = notify
        receiver = asyncio.create_task(app.receive_saved_messages(
            self.db, self.account, self.settings, asyncio.Event()))
        try:
            await asyncio.wait_for(acknowledged.wait(), 2)
            self.assertEqual(replies, list(range(102, 128)))
            self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0], 26)
            self.assertEqual(self.db.execute("SELECT count(*) FROM jobs WHERE status='processing'").fetchone()[0], 1)
            self.assertEqual(self.db.execute("SELECT count(*) FROM job_sources").fetchone()[0], 27)
            self.assertEqual(self.db.execute("SELECT value FROM state WHERE key='user_offset'").fetchone()[0], 127)
            self.client.get_messages.assert_any_await("me", min_id=121, reverse=True, limit=20)
            # 另开只读连接，证明回复前入队记录已经提交，不只是内存中可见。
            check = app.sqlite3.connect((app.DATA / "queue.sqlite3").as_uri() + "?mode=ro", uri=True)
            try:
                self.assertEqual(check.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0], 26)
            finally:
                check.close()
        finally:
            await self.cancel(receiver)

    async def test_links_are_acknowledged_and_persisted_during_blocking_download(self):
        self.enqueue()
        started, release = threading.Event(), threading.Event()

        def blocked_download(url, folder, *, cancel_event, **kwargs):
            started.set()
            while not release.wait(0.01):
                if cancel_event.is_set():
                    raise app.SafeError("下载已停止。")
            return self.fake_download(url, folder)

        with patch.object(app, "download", side_effect=blocked_download) as download:
            worker = asyncio.create_task(app.process_one_user(self.db, self.account, self.settings))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                await self.assert_queued_while_busy()
                download.assert_called_once()
                self.client.send_file.assert_not_awaited()
                release.set()
                await asyncio.wait_for(worker, 2)
            finally:
                release.set()
                await self.cancel(worker)
        self.assertEqual(list((app.DATA / "jobs").iterdir()), [])
        status_id = self.db.execute("SELECT message_id FROM job_status_messages WHERE post_id='123'").fetchone()[0]
        self.assertTrue(any(item.args[1] == status_id and "上传完成，已附原帖链接。" in item.args[2]
                            for item in self.client.edit_message.await_args_list))

    async def test_links_are_acknowledged_and_persisted_during_upload(self):
        self.enqueue()
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked_upload(*args, **kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(id=10)

        self.client.send_file.side_effect = blocked_upload
        with patch.object(app, "download", side_effect=self.fake_download):
            worker = asyncio.create_task(app.process_one_user(self.db, self.account, self.settings))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await self.assert_queued_while_busy()
                self.client.send_file.assert_awaited_once()
                release.set()
                await asyncio.wait_for(worker, 2)
            finally:
                release.set()
                await self.cancel(worker)

    async def test_one_message_accepts_all_links_and_ignores_status_notifications(self):
        message = self.message(101)
        message.raw_text = "\n".join(f"https://x.com/i/status/{i}" for i in range(1000, 1025))
        notices = app.accept_saved_message(self.db, message, 1)
        self.assertEqual(len(notices), 25)
        self.assertTrue(all(notice.startswith("已加入下载队列。\n") for notice in notices))
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 25)
        message.raw_text = notices[0]
        self.assertEqual(app.accept_saved_message(self.db, message, 1), [])

    async def test_consumer_serializes_uploads_and_continues_after_failure(self):
        for i in range(3):
            self.enqueue(str(1000 + i), 101 + i)
        completed = asyncio.Event()
        active = 0
        max_active = 0
        uploaded = []

        def download(url, folder, **kwargs):
            if url.endswith("1000"):
                raise app.SafeError("假下载失败")
            return self.fake_download(url, folder)

        async def upload(peer, path, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            uploaded.append(kwargs["caption"])
            active -= 1
            return SimpleNamespace(id=len(uploaded))

        async def edited(chat, message_id, text, **options):
            last_id = self.db.execute("SELECT message_id FROM job_status_messages WHERE post_id='1002'").fetchone()
            if "上传完成，已附原帖链接。" in text and last_id and message_id == last_id[0]:
                completed.set()

        self.client.send_file.side_effect = upload
        self.client.edit_message.side_effect = edited
        with patch.object(app, "download", side_effect=download):
            worker = asyncio.create_task(app.consume_user_jobs(
                self.db, self.account, self.settings, asyncio.Event()))
            try:
                await asyncio.wait_for(completed.wait(), 2)
            finally:
                await self.cancel(worker)
        self.assertEqual(max_active, 1)
        self.assertEqual(uploaded, ["https://x.com/i/status/1001", "https://x.com/i/status/1002"])
        self.assertEqual(self.db.execute("SELECT status FROM jobs ORDER BY rowid").fetchall(),
                         [("failed",), ("success",), ("success",)])
        self.assertEqual(self.db.execute("SELECT count(*) FROM sent").fetchone()[0], 2)
        self.assertEqual(list((app.DATA / "jobs").iterdir()), [])

    async def test_shutdown_waits_for_download_thread_before_cleanup(self):
        self.enqueue()
        started, stopped = threading.Event(), threading.Event()

        def download(url, folder, *, cancel_event, **kwargs):
            started.set()
            if not cancel_event.wait(3):
                raise AssertionError("下载线程未收到停止信号")
            (folder / "last-fragment.part").write_bytes(b"fake")
            stopped.set()
            raise app.SafeError("下载已停止。")

        with patch.object(app, "download", side_effect=download):
            runner = asyncio.create_task(app.run_user_tasks(self.db, self.account, self.settings))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
            finally:
                await asyncio.wait_for(self.cancel(runner), 2)
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "uncertain")
        self.client.send_file.assert_not_awaited()
        self.assertEqual(list((app.DATA / "jobs").iterdir()), [])

    async def test_cancelled_upload_remains_uncertain_and_cleans(self):
        self.enqueue()
        started = asyncio.Event()

        async def upload(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.client.send_file.side_effect = upload
        with patch.object(app, "download", side_effect=self.fake_download):
            worker = asyncio.create_task(app.process_one_user(self.db, self.account, self.settings))
            try:
                await asyncio.wait_for(started.wait(), 2)
            finally:
                await self.cancel(worker)
        self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "uncertain")
        self.assertEqual(self.db.execute("SELECT count(*) FROM sent").fetchone()[0], 0)
        self.assertEqual(list((app.DATA / "jobs").iterdir()), [])

    async def test_partial_upload_retry_skips_confirmed_and_uses_retry_source(self):
        self.enqueue()

        def download(url, folder, **kwargs):
            paths = [folder / "video-001.mp4", folder / "video-002.mp4"]
            for path in paths:
                path.write_bytes(b"fake")
            return paths

        self.client.send_file.side_effect = [SimpleNamespace(id=10), OSError("FAKE_PRIVATE_ERROR"), SimpleNamespace(id=11)]
        with patch.object(app, "download", side_effect=download):
            await app.process_one_user(self.db, self.account, self.settings)
            self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "uncertain")
            self.assertEqual(self.db.execute("SELECT count(*) FROM sent").fetchone()[0], 1)
            app.enqueue(self.db, "123", "https://x.com/i/status/123", retry=True, source_message_id=102)
            await app.process_one_user(self.db, self.account, self.settings)
        self.assertEqual(self.client.send_file.await_count, 3)
        self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "success")
        self.assertEqual(self.db.execute("SELECT count(*) FROM sent").fetchone()[0], 2)
        self.assertNotIn("FAKE_PRIVATE_ERROR", str(self.client.send_message.await_args_list))
        status_id = self.db.execute("SELECT message_id FROM job_status_messages WHERE post_id='123'").fetchone()[0]
        self.assertTrue(any(item.args[1] == status_id and "上传完成，已附原帖链接。" in item.args[2]
                            for item in self.client.edit_message.await_args_list))
        self.assertTrue(any(item.kwargs.get("reply_to") == 102 for item in self.client.send_message.await_args_list))

    async def test_receiver_failure_stops_consumer_without_advancing_cursor(self):
        self.client.get_messages.side_effect = OSError("FAKE_PRIVATE_ERROR")
        with self.assertRaises(app.SafeError) as caught:
            await asyncio.wait_for(app.run_user_tasks(self.db, self.account, self.settings), 2)
        self.assertNotIn("FAKE_PRIVATE_ERROR", str(caught.exception))
        self.assertEqual(self.db.execute("SELECT value FROM state WHERE key='user_offset'").fetchone()[0], 100)
        self.client.send_file.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
