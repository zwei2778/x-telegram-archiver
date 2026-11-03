"""本人在 Mac 交互终端确认后，导出仅含 X 域的会话；不会访问网络。"""

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import copy
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parent
TARGET_NAME = "x_cookies.txt"
WINDOWS_TO_UNIX_SECONDS = 11_644_473_600
MAX_UNIX_EXPIRY = 253_402_300_799  # 9999-12-31T23:59:59Z
MAX_CHROME_EXPIRY = (MAX_UNIX_EXPIRY + WINDOWS_TO_UNIX_SECONDS) * 1_000_000 + 999_999


class ExportError(Exception):
    """只承载固定的安全提示。"""


class QuietLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ExportError("参数无效，请使用 --help 查看用法。")


@contextmanager
def quiet_backend():
    # 不保留浏览器路径、Cookie 值或底层异常的临时日志副本。
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            yield


def extract_browser_cookies(browser, profile):
    # 延迟导入；模块导入、--help、取消和非交互运行均不触碰浏览器。
    from yt_dlp.cookies import extract_cookies_from_browser

    return extract_cookies_from_browser(browser, profile=profile, logger=QuietLogger())


def x_domain(domain):
    if not isinstance(domain, str):
        return False
    host = domain.lower()
    if host.startswith("."):
        host = host[1:]
    return bool(re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
                and any(host == allowed or host.endswith("." + allowed)
                        for allowed in ("x.com", "twitter.com")))


def chrome_cookie_copy(cookie):
    result = copy(cookie)
    expiry = result.expires
    if expiry is None:
        return result
    if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
            or isinstance(expiry, float) and not math.isfinite(expiry)
            or expiry < 0 or expiry > MAX_CHROME_EXPIRY):
        raise ExportError("Cookie 到期时间格式或范围无效，未保存新文件。")
    if expiry > 1_000_000_000_000:
        # 安装版 yt-dlp 的 Chrome 提取器原样返回 expires_utc。
        # Chromium BindTime(ExpiryDate) 使用1601起的微秒；这里只换单位/epoch，
        # 整数除法向下取秒，绝不把实际到期时刻向后延长。
        # https://chromium.googlesource.com/chromium/src/+/refs/heads/main/net/extras/sqlite/sqlite_persistent_cookie_store.cc
        # https://chromium.googlesource.com/chromium/src/+/refs/heads/main/base/time/time.h
        expiry = int((expiry - WINDOWS_TO_UNIX_SECONDS * 1_000_000) // 1_000_000)
    if not 0 <= expiry <= MAX_UNIX_EXPIRY:
        raise ExportError("Cookie 到期时间格式或范围无效，未保存新文件。")
    result.expires = expiry
    return result


def select_cookies(source, now=None, *, from_chrome=False):
    from yt_dlp.cookies import YoutubeDLCookieJar

    moment = time.time() if now is None else now
    selected = YoutubeDLCookieJar()
    marker_ids = set()
    for cookie in source:
        if not x_domain(cookie.domain):
            continue
        candidate = chrome_cookie_copy(cookie) if from_chrome else copy(cookie)
        if candidate.is_expired(moment):
            continue
        # Netscape 一行必须恰好七列，拒绝行/列分隔符，避免制造其他域的记录。
        fields = (candidate.domain, candidate.path, candidate.name, candidate.value)
        if any(isinstance(value, str) and any(c in value for c in "\r\n\t")
               for value in fields):
            raise ExportError("会话格式无法安全导出，未保存新文件。")
        # Check every source marker before CookieJar can overwrite a duplicate key.
        if candidate.name == "twid":
            marker_ids.add(twid_id(candidate.value))
            if len(marker_ids) > 1:
                raise ExportError("存在不同账号标记，未保存新文件；请使用只登录预期账号的独立 Chrome 配置。")
        # yt-dlp 保存 session Cookie 时会将副本的 expires=None 转为合法的 0。
        # 不修改浏览器返回的 Cookie 对象，也不延长真实的到期时间。
        selected.set_cookie(candidate)
    required = {cookie.name for cookie in selected
                if cookie.name in ("auth_token", "ct0")
                and isinstance(cookie.value, str) and cookie.value.strip()}
    if required != {"auth_token", "ct0"}:
        raise ExportError("未找到完整且未过期的 X 会话；请在选定的 Chrome 配置档中确认已登录。")
    return selected


def exact_profile(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:Default|Profile [1-9][0-9]*)", value):
        raise ExportError("必须明确选择 Chrome 配置目录名，例如 Default 或 Profile 1；不接受路径或自动选择。")
    return value


def expected_account(value):
    name = value[1:] if isinstance(value, str) and value.startswith("@") else value
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", name):
        raise ExportError("预期账号必须是 X 用户名，可带 @；不要输入密码、邮箱或会话内容。")
    return name


def expected_account_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ExportError("预期账号 ID 必须是事先独立核对的 1—20 位正整数。")
    return value


def twid_id(value):
    match = re.fullmatch(r"(?:u=|u%3[Dd])([1-9][0-9]{0,19})", value) if isinstance(value, str) else None
    if match is None:
        raise ExportError("账号标记无效，未保存新文件；请在独立 Chrome 配置中核对登录账号。")
    return match.group(1)


def account_marker(jar):
    """Only identify an unambiguous marker; this does not authenticate its token."""
    ids = {twid_id(cookie.value) for cookie in jar
           if cookie.name == "twid" and x_domain(cookie.domain)}
    if len(ids) != 1:
        raise ExportError("缺少唯一账号标记或存在不同账号，未保存新文件；请使用只登录预期账号的独立 Chrome 配置。")
    return next(iter(ids))


def path_info(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def check_target(target, replace):
    info = path_info(target)
    if info is None:
        return
    if not stat.S_ISREG(info.st_mode):
        raise ExportError("目标必须是普通文件，不能是符号链接、目录或特殊文件。")
    if not replace:
        raise ExportError("会话文件已存在；如需仅替换该文件，请使用 --replace 并重新确认。")


def prepare_destination(replace, *, create=True):
    data = ROOT / "data"
    private = data / "private"
    for directory in (data, private):
        info = path_info(directory)
        if info is None:
            if not create:
                continue
            directory.mkdir(mode=0o700)
            info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ExportError("会话目录必须是实际目录，不能是符号链接或特殊路径。")
    if create:
        private.chmod(0o700)
    target = private / TARGET_NAME
    check_target(target, replace)
    return target


def write_atomic(jar, target, replace):
    descriptor, name = tempfile.mkstemp(prefix=".x-cookies-", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    identity = os.fstat(descriptor)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            # 真实过期项已在内存排除；False 会误删 save() 转为 expires=0 的会话项。
            jar.save(stream, ignore_discard=True, ignore_expires=True)
            stream.flush()
            os.fsync(stream.fileno())
        # 再次检查目录/目标；默认独占发布，避免覆盖期间出现的已有文件。
        for directory in (ROOT / "data", target.parent):
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise ExportError("会话目录已发生变化，未发布新文件。")
        check_target(target, replace)
        if replace:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise ExportError("会话文件已存在；如需仅替换该文件，请使用 --replace 并重新确认。") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        # 只清理本次独占创建且仍为同一 inode 的临时文件。
        info = path_info(temporary)
        if (info is not None and stat.S_ISREG(info.st_mode)
                and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)):
            temporary.unlink()


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("--browser", choices=("chrome",), default="chrome", help="仅支持 Chrome")
    parser.add_argument("--profile", required=True, type=exact_profile,
                        help="必须明确指定 Chrome 配置目录名，例如 Default 或 Profile 1；不接受路径")
    parser.add_argument("--expected-account", required=True, type=expected_account,
                        help="本人预期的 X 用户名，例如 example_user；用于人工核对确认")
    parser.add_argument("--expected-account-id", type=expected_account_id,
                        help="可选：事先独立核对的账号数字 ID；不应从本次标记自动反填")
    parser.add_argument("--replace", action="store_true", help="确认后仅替换本项目 data/private/x_cookies.txt")
    try:
        args = parser.parse_args(argv)
        if not sys.stdin.isatty():
            raise ExportError("仅允许本人在交互终端运行；未读取浏览器 Cookie。")
        print("此操作将读取选定 Chrome 配置档的全部 Cookie。")
        print("选定配置目录：" + args.profile + "；预期账号：@" + args.expected_account)
        print("请先在该独立 Chrome 配置中亲自登录并核对账号，避免混用其他账号。")
        print("本工具将只显示账号数字标记和官方公开核对链接，不显示 Cookie 值。")
        print("yt-dlp 可能临时复制 Cookie 数据库，并触发 macOS 钥匙串授权。")
        print("仅保存 x.com、twitter.com 及其子域中未过期的 Cookie；需要有效 auth_token 和 ct0。")
        print("保存位置固定为本项目 data/private/x_cookies.txt，不连接 X/Telegram，也不自动上传。")
        if args.replace:
            print("已选择 --replace：只会原子替换上述会话文件，不删除其他文件。")
        else:
            print("已有会话文件时拒绝覆盖。")
        if input("本人同意后输入 EXPORT：").strip() != "EXPORT":
            print("已取消；未读取浏览器 Cookie。")
            return 1
        # Before browser access, reject unsafe/existing destinations without creating or changing them.
        target = prepare_destination(args.replace, create=False)
        with quiet_backend():
            try:
                source = extract_browser_cookies(args.browser, args.profile)
                if not len(source):
                    # 数据库有记录却全部无法解密时，多为 macOS 钥匙串未授权（yt-dlp 会静默跳过）。
                    raise ExportError("未能解密任何 Chrome Cookie：多半是 macOS 钥匙串未授权读取“Chrome Safe Storage”。"
                                      "请在弹窗中输入 Mac 登录密码并点“允许”后重试；未保存新文件。")
                selected = select_cookies(source, from_chrome=args.browser == "chrome")
                marker = account_marker(selected)
                if args.expected_account_id is not None and marker != args.expected_account_id:
                    raise ExportError("账号标记与事先核对的预期 ID 不一致，未保存新文件。")
            except ExportError:
                raise
            except BaseException:
                # 包括底层 SystemExit；不能让其消息在解释器退出时回显凭据。
                raise ExportError("导出未完成，请在本机检查 Chrome 登录状态、系统授权与目录权限；未输出原始错误。") from None
        print("会话中的账号标记 ID：" + marker)
        print("请自行打开 X 官方公开页面核对用户名：https://x.com/intent/user?user_id=" + marker)
        print("账号标记匹配不等于服务器已验证登录令牌归属；本工具不联网，也不自动打开浏览器。")
        print("只有确认上述 ID 的公开主页属于 @" + args.expected_account + " 后，才继续保存。")
        if input("确认后输入 SAVE @" + args.expected_account + "：") != "SAVE @" + args.expected_account:
            print("已取消保存；原会话文件保持不变。")
            return 1
        # Manual review can take time; never save authentication that expired during it.
        with quiet_backend():
            selected = select_cookies(selected)
            if account_marker(selected) != marker:
                raise ExportError("核对期间账号标记发生变化，未保存新文件。")
        target = prepare_destination(args.replace)
        with quiet_backend():
            try:
                write_atomic(selected, target, args.replace)
            except ExportError:
                raise
            except BaseException:
                raise ExportError("保存未完成，请在本机检查目标文件；未输出原始错误。") from None
        print("X 会话已保存到本项目 data/private/x_cookies.txt（目录 700，文件 600）。")
        print("文件包含登录凭据，请勿发到聊天或截图；未自动上传。")
        return 0
    except ExportError as error:
        print(str(error))
        return 1
    except (EOFError, KeyboardInterrupt):
        print("已中止导出；未输出会话内容。")
        return 1
    except Exception:
        print("导出未完成，请在本机检查 Chrome 登录状态、系统授权与目录权限；未输出原始错误。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
