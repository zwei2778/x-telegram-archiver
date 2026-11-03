"""X 视频归档：支持 Bot 模式，以及从收藏夹收链接的个人 Telegram 账号模式。"""

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import getpass
from importlib import import_module
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import uuid
import warnings
from urllib.parse import urlsplit

import imageio_ffmpeg
import requests
from yt_dlp import YoutubeDL

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ARCHIVER_DATA_DIR", str(ROOT / "data"))).resolve()
MAX_VIDEO = 49_000_000  # 留出 Telegram 50 MB 边界余量
MAX_DISK = 1_000_000_000
USER_MAX_VIDEO = 1_900_000_000  # 个人账号首版留出 2 GB 边界余量
X_COOKIE_MAX_BYTES = 1_000_000
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
POST_PATH = re.compile(r"/(?:[A-Za-z0-9_]{1,30}/status|i/(?:web/)?status)/(\d{1,25})(?:/video/\d+)?/?$")


class SafeError(Exception):
    """只使用代码内的固定消息，不包含第三方原始异常。"""


class Uncertain(SafeError):
    pass


def explicit_proxy():
    """只读取本程序显式配置，不输出地址或接受代理凭据。"""
    value = os.environ.get("ARCHIVER_PROXY_URL")
    if not value:
        return None
    try:
        proxy = urlsplit(value)
        if (any(ord(char) <= 32 or ord(char) == 127 for char in value)
                or proxy.scheme not in ("http", "https")
                or not proxy.hostname
                or not re.fullmatch(r"[A-Za-z0-9._:-]+", proxy.hostname)
                or proxy.username is not None or proxy.password is not None
                or proxy.port is None or not 1 <= proxy.port <= 65535
                or proxy.path or proxy.query or proxy.fragment
                or "?" in value or "#" in value):
            raise ValueError
    except ValueError:
        raise SafeError("代理配置无效：请使用不含用户名、密码、路径或参数的 HTTP(S) 代理地址与端口。") from None
    return value


def telegram_user_proxy():
    """将显式 HTTP 代理交给 MTProto；配置失败时绝不回退直连。"""
    value = explicit_proxy()
    if value is None:
        return None
    proxy = urlsplit(value)
    if proxy.scheme != "http":
        raise SafeError("个人账号模式目前只支持 HTTP CONNECT 代理；不支持 HTTPS 代理，也不会降级连接。")
    try:
        import_module("python_socks.async_.asyncio")
    except ImportError:
        raise SafeError("个人账号代理依赖不可用，请安装项目指定的 python-socks[asyncio] 后再启动。") from None
    return {"proxy_type": "http", "addr": proxy.hostname, "port": proxy.port, "rdns": True}


def hidden_input(prompt):
    """终端无法隐藏输入时停止，避免 getpass 回退为明文回显。"""
    if not sys.stdin.isatty():
        raise SafeError("请在自己的交互式终端输入，程序不会使用可回显的输入方式。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except (getpass.GetPassWarning, EOFError, OSError):
        raise SafeError("当前终端无法安全隐藏输入，已停止；请换用自己的交互式终端。") from None


def links(text):
    found = {}
    for candidate in URL_RE.findall(text):
        try:
            url = urlsplit(candidate.rstrip('.,;!?)，。；！）'))
            match = POST_PATH.fullmatch(url.path)
            if (url.scheme in ("https", "http") and url.hostname in
                    ("x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com")
                    and not url.username and not url.password and url.port in (None, 80, 443) and match):
                post_id = match[1]
                found[post_id] = f"https://x.com/i/status/{post_id}"
        except ValueError:
            continue
    return found


class Telegram:
    def __init__(self, token):
        proxy = explicit_proxy()
        self._base = "https://api.telegram.org/bot" + token + "/"
        self.session = requests.Session()
        # 不自动读取项目外的 ~/.netrc 等认证配置。
        self.session.trust_env = False
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def call(self, method, data=None, files=None):
        try:
            response = self.session.post(self._base + method, data=data or {}, files=files,
                                         timeout=(15, 180 if files else 45), allow_redirects=False)
            payload = response.json()
        except (requests.RequestException, ValueError):
            if files:
                raise Uncertain("上传结果不确定；请先查看群组，再决定是否重试。") from None
            raise SafeError("Telegram 网络请求失败，请检查网络后重试。") from None
        if not payload.get("ok"):
            code = payload.get("error_code", response.status_code)
            if files and (code >= 500 or code == 429):
                raise Uncertain("上传未确认；请检查群组，稍后再手动重试。")
            messages = {401: "Bot 令牌无效。", 403: "Bot 没有访问或发消息权限。",
                        409: "Bot 已有其他轮询程序或 webhook，请勿同时运行。",
                        429: "Telegram 请求过于频繁，请稍后重试。"}
            raise SafeError(messages.get(code, "Telegram 拒绝请求，请检查群组权限或文件格式。"))
        return payload["result"]

    def say(self, chat, text):
        try:
            self.call("sendMessage", {"chat_id": chat, "text": text, "link_preview_options": '{"is_disabled":true}'})
        except SafeError:
            print("状态通知未送达；处理记录仍保存在本机。", flush=True)

    def updates(self, offset, include_channels=False):
        return self.call("getUpdates", {"offset": offset, "timeout": 20,
                                       "allowed_updates": json.dumps(["message", "channel_post"] if include_channels else ["message"])})

    def upload(self, chat, path, caption, thread=None):
        video = path.suffix.lower() == ".mp4"
        field = "video" if video else "document"
        data = {"chat_id": chat, "caption": caption}
        if thread:
            data["message_thread_id"] = thread
        with path.open("rb") as stream:
            result = self.call("sendVideo" if video else "sendDocument", data,
                               {field: (path.name, stream, "application/octet-stream")})
        if not isinstance(result, dict) or not result.get("message_id"):
            raise Uncertain("上传结果不确定，请先检查群组。")
        return result["message_id"]


def binding_message(bot, bot_id, owner, update, code):
    """频道发帖可能没有真实 from；独立核验已配对用户的管理员身份。"""
    message = update.get("message") or update.get("channel_post") or {}
    command = message.get("text", "").split(maxsplit=1)
    if not owner or len(command) != 2 or command[0].split("@")[0] != "/bind" or command[1] != code:
        return None
    chat = message.get("chat", {})
    kind = chat.get("type")
    if kind in ("group", "supergroup"):
        return message if message.get("from", {}).get("id") == owner else None
    if kind != "channel" or "channel_post" not in update:
        return None
    member = bot.call("getChatMember", {"chat_id": chat["id"], "user_id": bot_id})
    if member.get("status") != "administrator" or not member.get("can_post_messages"):
        raise SafeError("频道绑定失败：请把 Bot 加为频道管理员，并允许发布消息，再发一次 /bind 指令。")
    user = bot.call("getChatMember", {"chat_id": chat["id"], "user_id": owner})
    if user.get("status") not in ("creator", "administrator"):
        raise SafeError("频道绑定失败：已配对的 Telegram 账号必须是该频道的所有者或管理员。")
    return message


def setup(bot, bot_id):
    config_path = DATA / "settings.json"
    if config_path.exists():
        raise SafeError("已有绑定配置。请使用 run；如需重新绑定，请先联系维护者。")
    code = secrets.token_hex(5)
    print(f"1. 私聊你自己的 Bot，发送：/pair {code}", flush=True)
    offset, owner = 0, None
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        for update in bot.updates(offset, include_channels=True):
            offset = update["update_id"] + 1
            message = update.get("message", {})
            sender = message.get("from", {}).get("id")
            chat = message.get("chat", {})
            text = message.get("text", "")
            if not owner and chat.get("type") == "private" and text == f"/pair {code}" and sender:
                owner = sender
                bot.say(owner, "本人绑定成功。将 Bot 加入私人群组，或加为私人频道管理员（允许发布消息），再到目标群组/频道发送终端中的 /bind 指令。")
                print(f"2. 群组：加入 Bot，允许发送消息和视频。频道：添加 Bot 为管理员，允许发布消息。\n在目标群组或频道发送：/bind {code}", flush=True)
            try:
                target = binding_message(bot, bot_id, owner, update, code)
            except SafeError as error:
                bot.say(owner, str(error))
                continue
            if target:
                chat = target["chat"]
                settings = {"owner_id": owner, "chat_id": chat["id"], "bot_id": bot_id,
                            "thread_id": target.get("message_thread_id"), "initial_offset": offset,
                            "chat_type": chat["type"]}
                with config_path.open("x", encoding="utf-8") as stream:
                    json.dump(settings, stream, indent=2)
                config_path.chmod(0o600)
                bot.say(owner, "配置完成。回到 Mac 终端运行 run 后，即可私聊发送 X 帖子链接。")
                print("绑定完成。配置中只有 ID，没有 Bot 令牌。", flush=True)
                return
    raise SafeError("十分钟内未完成绑定。请重新运行 setup 获取新的配对码。")


def database(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS jobs (post_id TEXT PRIMARY KEY, url TEXT, status TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS sent (post_id TEXT, media TEXT, message_id INTEGER, PRIMARY KEY(post_id,media))")
    db.execute("CREATE TABLE IF NOT EXISTS job_sources (post_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS job_status_messages (post_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL)")
    # 上次进程退出时可能已发送但未落盘，停止自动重试。
    db.execute("UPDATE jobs SET status='uncertain' WHERE status='processing'")
    db.commit()
    return db


def enqueue(db, post_id, url, retry=False, *, source_message_id=None):
    old = db.execute("SELECT status FROM jobs WHERE post_id=?", (post_id,)).fetchone()
    if old and old[0] in ("success", "queued", "processing"):
        return "该帖子已上传或已在队列中。"
    if old and old[0] == "uncertain" and not retry:
        return "上次上传结果不确定。请先查看群组，确认需要重发后发送 /retry 加帖子链接（可能重复）。"
    db.execute("INSERT INTO jobs VALUES (?,?,'queued') ON CONFLICT(post_id) DO UPDATE SET status='queued'", (post_id, url))
    # 重试使用新的状态回复；重复请求在上方返回，不能改掉正在使用的回复。
    db.execute("DELETE FROM job_status_messages WHERE post_id=?", (post_id,))
    # 仅保存原消息编号，随任务落盘；重复提示不能覆盖正在处理任务的来源。
    if type(source_message_id) is int and source_message_id > 0:
        db.execute("INSERT INTO job_sources VALUES (?,?) ON CONFLICT(post_id) DO UPDATE SET message_id=excluded.message_id",
                   (post_id, source_message_id))
    else:
        db.execute("DELETE FROM job_sources WHERE post_id=?", (post_id,))
    db.commit()
    return "已加入下载队列。"


class QuietLogger:
    def debug(self, *_):
        pass

    info = warning = error = debug


def load_x_session(ydl):
    """只为本次提取器载入用户指定的 X 会话，不读取浏览器或写回文件。"""
    configured = os.environ.get("ARCHIVER_X_COOKIE_FILE")
    if not configured:
        return False
    expected = DATA / "private" / "x_cookies.txt"
    try:
        # CookieJar 的解析警告可能包含整行 Cookie；只在本进程内静默处理。
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if configured != str(expected) or ydl.params.get("cookiefile") is not None:
                raise ValueError
            directory = os.open(expected.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                before = os.stat(expected.name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError
                descriptor = os.open(expected.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if ((info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                            or not stat.S_ISREG(info.st_mode)
                            or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                            or not 0 < info.st_size <= X_COOKIE_MAX_BYTES):
                        raise ValueError
                    content = stream.read(X_COOKIE_MAX_BYTES + 1)
            finally:
                os.close(directory)
            if len(content) > X_COOKIE_MAX_BYTES:
                raise ValueError
            text = content.decode("utf-8")
            for line in text.splitlines():
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                elif line.startswith("#") or not line.strip():
                    continue
                fields = line.split("\t")
                domain = fields[0].removeprefix(".").lower()
                if (len(fields) != 7 or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain)
                        or not (domain in ("x.com", "twitter.com")
                                or domain.endswith((".x.com", ".twitter.com")))
                        or fields[4] and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", fields[4])):
                    raise ValueError
            jar = ydl.cookiejar
            # 先保留 0 到期字段，让库将其规范为 session cookie，再逐项核对真实到期时间。
            jar.load(filename=io.StringIO(text), ignore_discard=True, ignore_expires=True)
            jar.clear_expired_cookies()
            cookies = list(jar)
            present = {cookie.name for cookie in cookies if cookie.value and cookie.value.strip()}
            if not {"auth_token", "ct0"}.issubset(present):
                raise ValueError
        return True
    except Exception:
        raise SafeError("X 会话文件无效、过期或权限不安全；请由本人更新指定的私密会话文件后重试。") from None


def disk_used(root):
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def transfer_progress_text(current, total=None, *, estimated=False):
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    if not number(current):
        return "正在传输…"
    if number(total) and total > 0:
        percent = min(100, max(0, int(current / total * 100)))
        return ("约 " if estimated else "") + f"{percent}%"
    return f"已传输 {current / 1_000_000:.1f} MB"


def download_progress_text(event):
    info = event.get("info_dict") or {}
    if not isinstance(info, dict):
        info = {}
    label = "媒体文件"
    if info.get("vcodec") == "none":
        label = "音轨"
    elif info.get("acodec") == "none":
        label = "视频流"
    index = info.get("playlist_autonumber") or info.get("playlist_index")
    total = info.get("n_entries") or info.get("playlist_count")
    if type(index) is int and type(total) is int and 1 <= index <= total <= 10:
        label = f"视频 {index}/{total} · " + label
    exact = event.get("total_bytes")
    size = exact or event.get("total_bytes_estimate")
    value = transfer_progress_text(event.get("downloaded_bytes"), size, estimated=not bool(exact))
    return f"⬇️ 正在下载（{label}）：{value}"


def download(url, folder, max_video=MAX_VIDEO, *, cancel_event=None, progress_callback=None):
    def check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise SafeError("下载已停止。")

    check_cancelled()
    task_budget = max(MAX_DISK, max_video * 3)
    reserve = max(2_000_000_000, max_video * 3 + 1_000_000_000)
    if disk_used(DATA / "jobs") > task_budget or shutil.disk_usage(DATA).free < reserve:
        raise SafeError(f"临时目录已达限额，或磁盘可用空间不足 {reserve / 1e9:.1f} GB，已暂停下载。")

    def progress(event):
        check_cancelled()
        if event.get("downloaded_bytes", 0) > max_video:
            raise SafeError(f"最高画质文件超过当前 {max_video / 1e6:.0f} MB 上限，未降低画质。")
        if disk_used(DATA / "jobs") > task_budget or shutil.disk_usage(DATA).free < 1_000_000_000:
            raise SafeError("临时文件或磁盘空间已达限制。")
        if progress_callback is not None:
            if event.get("status") == "downloading":
                progress_callback("download", download_progress_text(event))
            elif event.get("status") == "finished":
                progress_callback("processing", "🔧 当前媒体下载完成，正在整理视频…")

    def postprocess(event):
        check_cancelled()
        if progress_callback is not None:
            progress_callback("processing", "🔧 正在合并或整理视频…")

    options = {"format": "bv*+ba/b/bv*", "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
               "outtmpl": str(folder / "video-%(autonumber)03d.%(ext)s"),
               "quiet": True, "no_warnings": True, "logger": QuietLogger(),
               "progress_hooks": [progress], "postprocessor_hooks": [postprocess], "max_filesize": max_video,
               "socket_timeout": 25, "retries": 2, "fragment_retries": 2,
               "ignoreerrors": False, "restrictfilenames": True,
               "overwrites": False, "cachedir": False, "cookiefile": None,
               "concurrent_fragment_downloads": 1}
    proxy = explicit_proxy()
    if proxy:
        options["proxy"] = proxy
    try:
        with YoutubeDL(options) as ydl:
            load_x_session(ydl)
            info = ydl.extract_info(url, download=False)
            check_cancelled()
            if info and info.get("_type") == "playlist":
                entries = list(info.get("entries") or [])
                if len(entries) > 10:
                    raise SafeError("单帖视频数量超过首版限制。")
                info["entries"] = entries
            if info:
                info = ydl.process_ie_result(info, download=True)
            check_cancelled()
    except SafeError:
        raise
    except Exception:
        raise SafeError("下载失败：帖子可能无法访问、没有视频、超过大小限制，或网络/yt-dlp 需要检查；如已配置 X 会话，请由本人确认登录仍有效。") from None
    entries = (info.get("entries") or []) if info and info.get("_type") == "playlist" else [info]
    if not entries or any(not entry for entry in entries):
        raise SafeError("未获取到完整视频列表。")
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov"))
    if len(files) != len(entries) or any(p.stat().st_size == 0 or p.stat().st_size > max_video for p in files):
        raise SafeError(f"未得到完整、可上传的视频；最高画质可能超过当前 {max_video / 1e6:.0f} MB 上限，未自动降低画质。")
    return files


def clean_job(folder):
    # 仅接受本次进程创建的专用 UUID 目录；不扫描或清理旧目录。
    if (folder.parent == DATA / "jobs" and re.fullmatch(r"[0-9a-f]{32}", folder.name)
            and not folder.is_symlink()):
        shutil.rmtree(folder)


def process_one(db, bot, settings, max_video=MAX_VIDEO, *, reply_to_source=False):
    row = db.execute("SELECT post_id,url FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
    if not row:
        return
    post_id, url = row
    db.execute("UPDATE jobs SET status='processing' WHERE post_id=?", (post_id,))
    db.commit()
    folder = DATA / "jobs" / uuid.uuid4().hex
    folder.mkdir(mode=0o700)
    status = "failed"
    try:
        for path in download(url, folder, max_video=max_video):
            if db.execute("SELECT 1 FROM sent WHERE post_id=? AND media=?", (post_id, path.stem)).fetchone():
                continue
            # 若上传后、写入记录前退出，保留 uncertain 语义。
            message_id = bot.upload(settings["chat_id"], path, url, settings.get("thread_id"))
            try:
                db.execute("INSERT INTO sent VALUES (?,?,?)", (post_id, path.stem, message_id))
                db.commit()
            except Exception:
                raise Uncertain("视频已发送但本地记录失败，请检查群组后再决定是否重试。") from None
        status = "success"
        source = (db.execute("SELECT message_id FROM job_sources WHERE post_id=?", (post_id,)).fetchone()
                  if reply_to_source else None)
        if source:
            bot.say(settings["owner_id"], "上传完成，已附原帖链接。", reply_to=source[0])
        else:
            bot.say(settings["owner_id"], "上传完成，已附原帖链接。")
    except SafeError as error:
        status = "uncertain" if isinstance(error, Uncertain) else "failed"
        bot.say(settings["owner_id"], str(error))
    except BaseException:
        status = "uncertain"
        raise
    finally:
        try:
            db.execute("UPDATE jobs SET status=? WHERE post_id=?", (status, post_id))
            db.commit()
        finally:
            clean_job(folder)


def run(bot, settings):
    (DATA / "jobs").mkdir(mode=0o700, exist_ok=True)
    db = database(DATA / "queue.sqlite3")
    print("运行中。请私聊 Bot 发送 X 帖子链接；Ctrl+C 停止。", flush=True)
    while True:
        try:
            row = db.execute("SELECT value FROM state WHERE key='offset'").fetchone()
            offset = row[0] if row else settings["initial_offset"]
            # 有待办时不长轮询，先完成队列。
            if db.execute("SELECT 1 FROM jobs WHERE status='queued'").fetchone():
                process_one(db, bot, settings)
                continue
            for update in bot.updates(offset):
                message = update.get("message", {})
                if (message.get("chat", {}).get("type") == "private"
                        and message.get("from", {}).get("id") == settings["owner_id"]):
                    text = message.get("text", "")
                    items = links(text)
                    if items:
                        for post_id, url in list(items.items())[:5]:
                            bot.say(settings["owner_id"], enqueue(db, post_id, url, text.startswith("/retry ")))
                    elif text.startswith(("/start", "/help")):
                        bot.say(settings["owner_id"], "直接发送 x.com 或 twitter.com 的帖子链接，每条消息最多 5 条。重复成功帖子会跳过。")
                    else:
                        bot.say(settings["owner_id"], "请发送 X 帖子的完整链接，暂不支持短链接和需要登录的帖子。")
                db.execute("INSERT INTO state VALUES ('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (update["update_id"] + 1,))
                db.commit()
        except SafeError as error:
            print(str(error), flush=True)
            time.sleep(5)


class TelegramUser:
    """只向已绑定的目标上传；通知发到本人的收藏夹。"""
    def __init__(self, client, owner, target):
        self.client, self.owner, self.target = client, owner, target

    def notification_options(self, chat, reply_to):
        if chat != self.owner:
            raise SafeError("个人账号通知目标与本人不一致。")
        options = {"parse_mode": None, "link_preview": False}
        if reply_to is not None:
            options["reply_to"] = reply_to
        return options

    def say(self, chat, text, *, reply_to=None):
        options = self.notification_options(chat, reply_to)
        try:
            self.client.send_message("me", text, **options)
        except Exception:
            print("状态通知未送达，请查看任务记录。", flush=True)

    async def say_async(self, chat, text, *, reply_to=None):
        options = self.notification_options(chat, reply_to)
        try:
            await self.client.send_message("me", text, **options)
        except Exception:
            print("状态通知未送达，请查看任务记录。", flush=True)

    async def create_status_async(self, chat, text, *, reply_to=None):
        options = self.notification_options(chat, reply_to)
        result = await self.client.send_message("me", text, **options)
        message_id = getattr(result, "id", None)
        return message_id if type(message_id) is int and message_id > 0 else None

    async def edit_status_async(self, message_id, text):
        if type(message_id) is not int or message_id <= 0:
            return
        await self.client.edit_message("me", message_id, text, parse_mode=None, link_preview=False)

    def upload_arguments(self, chat, path, caption, thread):
        if chat != self.target[0]:
            raise SafeError("上传目标与已绑定频道不一致，已停止。")
        if path.stat().st_size > USER_MAX_VIDEO:
            raise SafeError("文件超过个人账号模式当前 1.9 GB 上限。")
        return {"caption": caption, "parse_mode": None,
                "force_document": path.suffix.lower() != ".mp4",
                "supports_streaming": path.suffix.lower() == ".mp4", "reply_to": thread}

    @staticmethod
    def upload_failed(error):
        from telethon import errors
        if isinstance(error, errors.FloodWaitError):
            raise SafeError(f"Telegram 要求等待 {int(error.seconds)} 秒。请稍后重试，不会绕过限流。") from None
        if isinstance(error, errors.RPCError):
            if getattr(error, "code", 0) >= 500:
                raise Uncertain("个人账号上传结果不确定，请先检查频道再决定是否重试。") from None
            raise SafeError("Telegram 拒绝个人账号上传，请检查发帖权限、账号状态或文件限制。") from None
        raise Uncertain("个人账号上传结果不确定，请先检查频道再决定是否重试。") from None

    @staticmethod
    def confirmed_upload(result):
        if not getattr(result, "id", None):
            raise Uncertain("个人账号上传未得到确认，请先检查频道。")
        return result.id

    def upload(self, chat, path, caption, thread=None):
        options = self.upload_arguments(chat, path, caption, thread)
        try:
            result = self.client.send_file(self.target[1], str(path), **options)
        except Exception as error:
            self.upload_failed(error)
        return self.confirmed_upload(result)

    async def upload_async(self, chat, path, caption, thread=None, *, progress_callback=None):
        options = self.upload_arguments(chat, path, caption, thread)
        if progress_callback is not None:
            options["progress_callback"] = progress_callback
        try:
            result = await self.client.send_file(self.target[1], str(path), **options)
        except Exception as error:
            self.upload_failed(error)
        return self.confirmed_upload(result)


class UserProgressManager:
    """同一事件循环内管理状态回复；只保存程序消息编号，不保存进度事件内容。"""
    def __init__(self, db, account, owner, *, clock=time.monotonic):
        self.db, self.account, self.owner, self.clock = db, account, owner, clock
        self.states = {}
        self.retry_after = float("-inf")

    def get(self, post_id):
        source = self.db.execute("SELECT message_id FROM job_sources WHERE post_id=?", (post_id,)).fetchone()
        source_id = source[0] if source else None
        key = (post_id, source_id)
        for old_key, old in list(self.states.items()):
            if old_key[0] == post_id and old_key != key:
                old.disabled, old.dirty = True, False
                self.states.pop(old_key)
        if key not in self.states:
            self.states[key] = UserJobProgress(self, post_id, source_id)
        return self.states[key]

    async def flush_all(self):
        for key, state in list(self.states.items()):
            await state.flush()
            if state.terminal and not state.dirty:
                if self.states.get(key) is state:
                    self.states.pop(key)

    async def run(self):
        while True:
            await asyncio.sleep(1)
            await self.flush_all()


class UserJobProgress:
    INTERVAL = 10

    def __init__(self, manager, post_id, source_id):
        self.manager, self.post_id, self.source_id = manager, post_id, source_id
        row = manager.db.execute("SELECT message_id FROM job_status_messages WHERE post_id=?", (post_id,)).fetchone()
        self.message_id = row[0] if row else None
        self.lock = asyncio.Lock()
        self.stage, self.text = "queued", "已加入下载队列。"
        self.terminal, self.dirty = False, True
        self.last_text, self.last_stage = None, None
        self.last_attempt, self.retry_after = float("-inf"), float("-inf")
        self.disabled = False

    def report(self, stage, text, terminal=False):
        if self.terminal:
            return
        self.stage, self.text, self.terminal = stage, text, terminal
        self.dirty = True

    def render(self):
        db = self.manager.db
        text = self.text
        if not self.terminal:
            if self.stage == "queued":
                row = db.execute("SELECT rowid FROM jobs WHERE post_id=?", (self.post_id,)).fetchone()
                ahead = db.execute("SELECT count(*) FROM jobs WHERE post_id!=? AND (status='processing' OR (status='queued' AND rowid<?))",
                                   (self.post_id, row[0] if row else 0)).fetchone()[0]
                text += f"\n入队时前面有 {ahead} 条任务。"
            else:
                waiting = db.execute("SELECT count(*) FROM jobs WHERE status='queued' AND post_id!=?", (self.post_id,)).fetchone()[0]
                text += f"\n等待处理：{waiting} 条"
        if self.source_id is not None:
            count = db.execute("SELECT count(*) FROM job_sources WHERE message_id=?", (self.source_id,)).fetchone()[0]
            if count > 1:
                row = db.execute("SELECT url FROM jobs WHERE post_id=?", (self.post_id,)).fetchone()
                if row:
                    text += "\n" + row[0]
        return text

    def is_current(self):
        source = self.manager.db.execute("SELECT message_id FROM job_sources WHERE post_id=?", (self.post_id,)).fetchone()
        return (source[0] if source else None) == self.source_id

    async def flush(self, force=False):
        # 进度显示的网络/数据库错误不影响媒体上传及sent确认。
        from telethon import errors
        async with self.lock:
            now = self.manager.clock()
            if self.disabled:
                self.dirty = False
                return
            if not self.dirty or now < max(self.retry_after, self.manager.retry_after):
                return
            if self.stage == self.last_stage and now - self.last_attempt < self.INTERVAL:
                return
            self.last_attempt = now
            try:
                if not self.is_current():
                    self.disabled, self.dirty = True, False
                    return
                text = self.render()
                stage, terminal = self.stage, self.terminal
                if text == self.last_text:
                    self.dirty = False
                    return
                if self.message_id is None:
                    message_id = await self.manager.account.create_status_async(
                        self.manager.owner, text, reply_to=self.source_id)
                    if type(message_id) is not int or message_id <= 0:
                        # 可能已发送但回执丢失，不能每隔10秒创建另一条回复。
                        self.disabled, self.dirty = True, False
                        print("状态回复未得到确认，已停止自动重发；视频任务继续。", flush=True)
                        return
                    self.message_id = message_id
                    # 发送等待期间可能发生显式重试；旧尝试不能覆盖新尝试的回复编号。
                    if self.disabled or not self.is_current():
                        self.disabled, self.dirty = True, False
                        return
                    try:
                        self.manager.db.execute("INSERT INTO job_status_messages VALUES (?,?) ON CONFLICT(post_id) DO UPDATE SET message_id=excluded.message_id",
                                                (self.post_id, message_id))
                        self.manager.db.commit()
                    except Exception:
                        print("状态回复编号未能保存；不影响视频任务。", flush=True)
                else:
                    await self.manager.account.edit_status_async(self.message_id, text)
                self.last_text, self.last_stage = text, stage
                # 网络等待期间可能有新进度，保留下一次刷新。
                self.dirty = (self.stage, self.terminal, self.render()) != (stage, terminal, text)
            except errors.MessageNotModifiedError:
                self.last_text, self.last_stage = text, stage
                try:
                    self.dirty = (self.stage, self.terminal, self.render()) != (stage, terminal, text)
                except Exception:
                    self.dirty = True
                    self.retry_after = self.manager.clock() + self.INTERVAL
            except errors.FloodWaitError as error:
                self.retry_after = self.manager.clock() + max(1, int(error.seconds)) + 1
                self.manager.retry_after = max(self.manager.retry_after, self.retry_after)
            except (errors.MessageIdInvalidError, errors.MessageAuthorRequiredError):
                self.disabled = True
                self.dirty = False
            except Exception:
                if self.message_id is None:
                    self.disabled, self.dirty = True, False
                    print("状态回复未得到确认，已停止自动重发；视频任务继续。", flush=True)
                else:
                    self.retry_after = self.manager.clock() + self.INTERVAL

    async def finish(self, text):
        self.report("done", text, terminal=True)
        await self.flush(force=True)


class DownloadProgressBridge:
    """下载线程只投递最新的安全文字，至多保留一个待执行回调。"""
    def __init__(self, loop, state):
        self.loop, self.state = loop, state
        self.lock, self.latest = threading.Lock(), None
        self.pending, self.closed = False, False

    def __call__(self, stage, text):
        with self.lock:
            if self.closed:
                return
            self.latest = (stage, text)
            if self.pending:
                return
            self.pending = True
        try:
            self.loop.call_soon_threadsafe(self.deliver)
        except RuntimeError:
            pass

    def deliver(self):
        with self.lock:
            latest = self.latest if not self.closed else None
            self.pending = False
        if latest is not None:
            self.state.report(*latest)

    def close(self):
        with self.lock:
            self.closed = True
            self.latest = None


def account_credentials(interactive):
    private = DATA / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    config = private / "telegram_api.json"
    if config.exists():
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
            api_id, api_hash = int(data["api_id"]), data["api_hash"]
        except (OSError, ValueError, KeyError, TypeError):
            raise SafeError("无法读取本地 Telegram API 配置；不要把配置内容发到聊天中。") from None
    else:
        if not interactive or not sys.stdin.isatty():
            raise SafeError("请先在自己的终端运行 login-user 完成个人账号登录。")
        print("请先在 my.telegram.org 的 API development tools 创建自己的免费应用。", flush=True)
        raw_id = hidden_input("本机输入 Telegram api_id（不显示）：").strip()
        api_hash = hidden_input("本机输入 Telegram api_hash（不显示）：").strip()
        if not raw_id.isdigit():
            raise SafeError("api_id 应为数字。")
        api_id = int(raw_id)
    if api_id <= 0 or not isinstance(api_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise SafeError("Telegram API 应用信息格式不正确。")
    return api_id, api_hash, config


def resolve_user_target(client, chat_id):
    # 只遍历会话列表定位已绑定目标，不下载其他聊天的历史或媒体。
    for dialog in client.iter_dialogs():
        if dialog.id == chat_id:
            entity = dialog.entity
            if getattr(entity, "broadcast", False):
                rights = getattr(entity, "admin_rights", None)
                if not getattr(entity, "creator", False) and not getattr(rights, "post_messages", False):
                    raise SafeError("当前个人账号没有在目标频道发帖的权限。")
            return (chat_id, dialog.input_entity)
    raise SafeError("个人账号的会话列表中未找到已绑定频道，请先用该账号打开并加入目标频道。")


def initial_user_target(client, me):
    """本人只从自己拥有或管理的频道/群组中明确选择首次上传目标。"""
    if not sys.stdin.isatty():
        raise SafeError("首次设置只允许本人在交互终端完成。")
    username = getattr(me, "username", None)
    identity = "@" + username if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,32}", username) else "本人账号"
    print(f"已登录 Telegram：{identity}（账号 ID：{me.id}）。请核对是否为你预期的账号。", flush=True)
    candidates = []
    for dialog in client.iter_dialogs():
        entity = dialog.entity
        if (not (getattr(dialog, "is_channel", False) or getattr(dialog, "is_group", False))
                or type(dialog.id) is not int or dialog.id >= 0
                or getattr(entity, "deactivated", False) or getattr(entity, "migrated_to", None)):
            continue
        creator = bool(getattr(entity, "creator", False))
        rights = getattr(entity, "admin_rights", None)
        if getattr(entity, "broadcast", False):
            allowed = creator or bool(rights and getattr(rights, "post_messages", False))
        else:
            allowed = creator or bool(rights)
        if allowed:
            candidates.append(dialog)
    if not candidates:
        raise SafeError("没有找到你拥有或管理且可发帖的频道/群组。请先在 Telegram 创建私人频道，再重新运行 setup-user。")
    print("请选择视频上传目标（仅列出你拥有或管理的频道/群组，不读取聊天历史）：", flush=True)
    labels = []
    for number, dialog in enumerate(candidates, 1):
        raw = getattr(dialog, "name", "")
        label = "".join(character if character.isprintable() else " " for character in str(raw))[:80].strip() or "未命名目标"
        labels.append(label)
        print(f"{number}. {label}（目标 ID：{dialog.id}）", flush=True)
    selection = input("输入目标编号：").strip()
    if not selection.isascii() or not selection.isdecimal() or not 1 <= int(selection) <= len(candidates):
        raise SafeError("目标编号无效，未保存绑定。")
    index = int(selection) - 1
    chosen = candidates[index]
    print(f"将用上述 Telegram 账号上传到：{labels[index]}（目标 ID：{chosen.id}）。", flush=True)
    if input("核对账号和目标后，输入 BIND 保存；其他输入取消：").strip() != "BIND":
        raise SafeError("已取消，未保存绑定。")
    return chosen.id, chosen.input_entity


def write_user_settings(settings):
    """排他、原子发布首次绑定；不覆盖既有配置或清理其他文件。"""
    target = DATA / "settings.json"
    if os.path.lexists(target):
        raise SafeError("已有绑定配置，未覆盖。请使用 login-user 检查登录，或使用 run-user 启动。")
    descriptor, name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=DATA)
    temporary = Path(name)
    identity = os.fstat(descriptor)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(settings, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        current = temporary.lstat()
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise SafeError("临时配置发生变化，未保存绑定。")
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            raise SafeError("已有绑定配置，未覆盖。请使用 login-user 检查登录，或使用 run-user 启动。") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            current = temporary.lstat()
        except FileNotFoundError:
            current = None
        if (current is not None and stat.S_ISREG(current.st_mode)
                and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)):
            temporary.unlink()


def setup_user():
    if not sys.stdin.isatty():
        raise SafeError("首次设置只允许本人在交互终端运行 setup-user。")
    if os.path.lexists(DATA / "settings.json"):
        raise SafeError("已有绑定配置，未覆盖。请使用 login-user 检查登录，或使用 run-user 启动。")
    account, loop = open_user_account(None, interactive=True)
    try:
        write_user_settings({"owner_id": account.owner, "chat_id": account.target[0], "thread_id": None,
                             "mode": "user"})
        print("个人账号首次设置完成。现在运行 run-user，或用 Docker Compose 启动服务。", flush=True)
        print("请在服务启动后，向本人的 Telegram 收藏夹发送一条新 X 链接测试。", flush=True)
    finally:
        try:
            account.client.disconnect()
        finally:
            loop.close()


def open_user_account(settings, interactive=False):
    if settings is None and (not interactive or not sys.stdin.isatty()):
        raise SafeError("首次设置需要本人在交互终端运行 setup-user。")
    from telethon.sync import TelegramClient
    from telethon import errors

    proxy = telegram_user_proxy()
    api_id, api_hash, config = account_credentials(interactive)
    # sync 客户端仍需要自己的 asyncio 事件循环。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = None
    try:
        client = TelegramClient(str(DATA / "private" / "telegram"), api_id, api_hash,
                                proxy=proxy, receive_updates=False, request_retries=2,
                                connection_retries=2, flood_sleep_threshold=0,
                                device_model="Personal Video Archiver")
        client.session.save_entities = False
        client.connect()
        if not client.is_user_authorized():
            if not interactive or not sys.stdin.isatty():
                raise SafeError("个人账号会话未登录或已失效，请在本机运行 login-user。")
            phone = hidden_input("手机号，含国家区号（不显示）：").strip()
            client.send_code_request(phone)
            code = hidden_input("Telegram 登录验证码（不显示）：").strip()
            try:
                client.sign_in(phone=phone, code=code)
            except errors.SessionPasswordNeededError:
                password = hidden_input("Telegram 两步验证密码（不显示）：")
                client.sign_in(password=password)
        me = client.get_me()
        if not me or me.bot or type(me.id) is not int or me.id <= 0:
            raise SafeError("未能确认本人的 Telegram 账号，已停止。")
        if settings is None:
            if not interactive:
                raise SafeError("首次设置需要本人运行 setup-user。")
            target = initial_user_target(client, me)
        elif me.id != settings["owner_id"]:
            raise SafeError("登录账号与原来配对的本人账号不一致，已停止。请不要使用其他账号。")
        else:
            target = resolve_user_target(client, settings["chat_id"])
        if not config.exists():
            with config.open("x", encoding="utf-8") as stream:
                json.dump({"api_id": api_id, "api_hash": api_hash}, stream)
            config.chmod(0o600)
        session = DATA / "private" / "telegram.session"
        if session.exists():
            session.chmod(0o600)
        return TelegramUser(client, me.id, target), loop
    except BaseException as error:
        try:
            if client is not None:
                client.disconnect()
        except Exception:
            pass
        finally:
            loop.close()
        if isinstance(error, (SafeError, KeyboardInterrupt)):
            raise
        if isinstance(error, errors.FloodWaitError):
            raise SafeError(f"Telegram 要求等待 {int(error.seconds)} 秒后再登录，请不要连续尝试。") from None
        if isinstance(error, (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError)):
            raise SafeError("登录验证码不正确或已过期，请重新运行 login-user，并输入新收到的验证码。") from None
        if isinstance(error, errors.PasswordHashInvalidError):
            raise SafeError("Telegram 两步验证密码不正确。") from None
        if isinstance(error, errors.ApiIdInvalidError):
            raise SafeError("Telegram api_id / api_hash 不匹配，请核对自己创建的应用信息。") from None
        raise SafeError("个人账号登录或连接未完成，请检查网络与账号状态；未输出原始异常或凭据。") from None


def accept_saved_message(db, message, owner):
    # 不响应旧消息、他人账号消息或程序自己的状态通知。
    if not getattr(message, "out", False) or getattr(message, "sender_id", None) != owner:
        return []
    text = (getattr(message, "raw_text", None) or "").strip()
    if not (text.startswith(("https://", "http://", "/archive ", "/retry "))):
        return []
    items = list(links(text).items())
    # 原生回复定位来源消息；同条消息含多个帖子时再标明各自的链接。
    return [enqueue(db, post_id, url, text.startswith("/retry "),
                    source_message_id=getattr(message, "id", None))
            + ("\n" + url if len(items) > 1 else "")
            for post_id, url in items]


async def download_user_job(url, folder, *, progress_callback=None):
    # yt-dlp是阻塞操作；只有下载放在线程中，Telegram连接及SQLite仍留在原事件循环。
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(
        download, url, folder, max_video=USER_MAX_VIDEO, cancel_event=cancelled,
        progress_callback=progress_callback))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        # 等下载线程退出后才允许清理，避免删完目录后线程继续写文件。
        try:
            await task
        except Exception:
            pass
        raise


async def process_one_user(db, account, settings, *, progress_manager=None):
    row = db.execute("SELECT post_id,url FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
    if not row:
        return
    post_id, url = row
    db.execute("UPDATE jobs SET status='processing' WHERE post_id=?", (post_id,))
    db.commit()
    folder = DATA / "jobs" / uuid.uuid4().hex
    folder.mkdir(mode=0o700)
    manager = progress_manager or UserProgressManager(db, account, settings["owner_id"])
    progress = manager.get(post_id)
    bridge = DownloadProgressBridge(asyncio.get_running_loop(), progress)
    status = "uncertain"
    try:
        progress.report("parsing", "🔎 正在解析视频…")
        await progress.flush(force=True)
        paths = await download_user_job(url, folder, progress_callback=bridge)
        bridge.close()
        for index, path in enumerate(paths, 1):
            if db.execute("SELECT 1 FROM sent WHERE post_id=? AND media=?", (post_id, path.stem)).fetchone():
                continue
            label = f"视频 {index}/{len(paths)} · " if len(paths) > 1 else ""

            def upload_progress(current, total, label=label, index=index):
                value = transfer_progress_text(current, total)
                suffix = "（等待 Telegram 确认）" if current == total and total else ""
                progress.report(f"upload:{index}", f"⬆️ 正在上传（{label}文件）：{value}{suffix}")

            upload_progress(0, path.stat().st_size)
            await progress.flush(force=True)
            message_id = await account.upload_async(settings["chat_id"], path, url,
                                                    settings.get("thread_id"), progress_callback=upload_progress)
            try:
                db.execute("INSERT INTO sent VALUES (?,?,?)", (post_id, path.stem, message_id))
                db.commit()
            except Exception:
                raise Uncertain("视频已发送但本地记录失败，请检查群组后再决定是否重试。") from None
        status = "success"
        await progress.finish("✅ 上传完成，已附原帖链接。")
    except SafeError as error:
        status = "uncertain" if isinstance(error, Uncertain) else "failed"
        await progress.finish(("⚠️ " if status == "uncertain" else "❌ ") + str(error))
    finally:
        bridge.close()
        try:
            db.execute("UPDATE jobs SET status=? WHERE post_id=?", (status, post_id))
            db.commit()
        finally:
            clean_job(folder)
        if not progress.terminal:
            await progress.finish("⚠️ 任务已停止，结果尚未确认。请检查频道后再决定是否重试。")


async def receive_saved_messages(db, account, settings, wake, *, progress_manager=None):
    manager = progress_manager or UserProgressManager(db, account, settings["owner_id"])
    while True:
        row = db.execute("SELECT value FROM state WHERE key='user_offset'").fetchone()
        try:
            messages = await account.client.get_messages("me", min_id=row[0], reverse=True, limit=20)
            for message in messages:
                notices = accept_saved_message(db, message, settings["owner_id"])
                items = list(links((getattr(message, "raw_text", None) or "").strip()).items())
                for (post_id, _), notice in zip(items, notices):
                    if notice.startswith("已加入下载队列。"):
                        await manager.get(post_id).flush(force=True)
                    else:
                        await account.say_async(settings["owner_id"], notice, reply_to=message.id)
                db.execute("UPDATE state SET value=? WHERE key='user_offset'", (message.id,))
                db.commit()
                if notices:
                    wake.set()
        except Exception:
            raise SafeError("读取收藏夹失败，已停止；请检查网络、登录状态或账号限流后重新启动。") from None
        # 一批超过20条时接着读取下一页，不等队列处理完，也不丢弃分页后的消息。
        if len(messages) < 20:
            await asyncio.sleep(3)


async def consume_user_jobs(db, account, settings, wake, *, progress_manager=None):
    manager = progress_manager or UserProgressManager(db, account, settings["owner_id"])
    while True:
        wake.clear()
        if db.execute("SELECT 1 FROM jobs WHERE status='queued'").fetchone():
            await process_one_user(db, account, settings, progress_manager=manager)
        else:
            await wake.wait()


async def run_user_tasks(db, account, settings):
    wake = asyncio.Event()
    manager = UserProgressManager(db, account, settings["owner_id"])
    tasks = [asyncio.create_task(receive_saved_messages(db, account, settings, wake, progress_manager=manager)),
             asyncio.create_task(consume_user_jobs(db, account, settings, wake, progress_manager=manager)),
             asyncio.create_task(manager.run())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def run_user(account, settings):
    (DATA / "jobs").mkdir(mode=0o700, exist_ok=True)
    db = database(DATA / "queue.sqlite3")
    try:
        row = db.execute("SELECT value FROM state WHERE key='user_offset'").fetchone()
        if not row:
            # 首次启用只处理启动后新发的链接，不翻历史收藏夹。
            recent = account.client.get_messages("me", limit=1)
            offset = recent[0].id if recent else 0
            db.execute("INSERT INTO state VALUES ('user_offset',?)", (offset,))
            db.commit()
        print("个人账号模式运行中。把 X 帖子链接发送到 Telegram 收藏夹（Saved Messages）；Ctrl+C 停止。", flush=True)
        task = account.client.loop.create_task(run_user_tasks(db, account, settings))
        try:
            account.client.loop.run_until_complete(task)
        finally:
            if not task.done():
                task.cancel()
                account.client.loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
    finally:
        db.close()


def load_token(token_file=None):
    if token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise SafeError("无法读取指定的 Bot secret 文件，请检查挂载与权限。") from None
    else:
        if not sys.stdin.isatty():
            raise SafeError("交互式启动请在自己的终端输入令牌；容器启动请配置 BOT_TOKEN_FILE。")
        token = getpass.getpass("在本机粘贴 BotFather 令牌（不显示、不保存）：").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise SafeError("令牌格式不正确。")
    return token


def stop_requested(signum, frame):
    # Docker stop 的 SIGTERM 也走任务清理和 uncertain 记录流程。
    raise KeyboardInterrupt


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "run", "check", "setup-user", "login-user", "run-user"))
    parser.add_argument("--token-file", default=os.environ.get("BOT_TOKEN_FILE"),
                        help="用户提供的令牌文件路径；不把令牌本身放进参数")
    args = parser.parse_args()
    if args.command == "check":
        print("依赖可用；ffmpeg：" + ("可用" if Path(imageio_ffmpeg.get_ffmpeg_exe()).is_file() else "不可用"))
        return
    DATA.mkdir(mode=0o700, exist_ok=True)
    # 一个 Bot 只运行一个本地消费者。
    with (DATA / "process.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafeError("项目已有一个进程在运行，请先停止它。") from None
        if args.command == "setup-user":
            logging.disable(logging.CRITICAL)
            setup_user()
            return
        if args.command in ("login-user", "run-user"):
            logging.disable(logging.CRITICAL)
            settings_path = DATA / "settings.json"
            if not settings_path.exists():
                raise SafeError("尚无目标绑定，请先在自己的交互终端运行 setup-user。")
            settings = json.loads(settings_path.read_text())
            account, loop = open_user_account(settings, interactive=args.command == "login-user")
            try:
                if args.command == "login-user":
                    print("个人账号登录及目标权限检查完成；凭据和会话仅保存在 data/private/。", flush=True)
                    print("下一步运行：.venv/bin/python archiver.py run-user", flush=True)
                else:
                    run_user(account, settings)
            finally:
                try:
                    account.client.disconnect()
                finally:
                    loop.close()
            return
        token = load_token(args.token_file)
        # 防止第三方库 debug 日志打印包含令牌的 Telegram URL。
        logging.disable(logging.CRITICAL)
        bot = Telegram(token)
        bot_id = bot.call("getMe")["id"]
        if args.command == "setup":
            setup(bot, bot_id)
        else:
            path = DATA / "settings.json"
            if not path.exists():
                raise SafeError("请先运行 setup 完成本人及群组绑定。")
            settings = json.loads(path.read_text())
            if settings["bot_id"] != bot_id:
                raise SafeError("令牌属于另一个 Bot，与已绑定的 Bot 不一致。")
            run(bot, settings)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_requested)
    try:
        main()
    except KeyboardInterrupt:
        print("已停止。")
    except SafeError as error:
        print(str(error))
        sys.exit(1)
    except Exception:
        print("程序遇到错误，已停止。未输出原始异常，以免暴露凭据。")
        sys.exit(1)
