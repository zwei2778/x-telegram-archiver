# 可选：使用自己的 X 登录会话

这一步只用于访问需要登录的帖子。它不会自动读取点赞或书签，也不会让 NAS 依赖 Chrome 持续在线。

## 1. 在 Mac 准备独立 Chrome 配置

在 Chrome 的“个人资料”菜单中新建专用配置，**只登录准备用于归档的 X 账号**。X 密码和验证码只在官方 `https://x.com` 页面输入。

在该窗口打开 `chrome://version`，找到“个人资料路径”。最后一段可能是 `Default` 或 `Profile 1`、`Profile 2` 等。它不是你给窗口起的显示名称。记下最后这一段，不要选 Chrome 用户数据总目录。

## 2. 本人在 Mac 交互终端导出

在公开项目目录创建本地 Python 3.12 环境并安装依赖：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-runtime.txt
.venv/bin/python prepare_x_session.py --help
```

下面使用假用户名和示例配置，请替换成自己刚核对的配置及 X 用户名：

```sh
.venv/bin/python prepare_x_session.py --profile "Profile 1" --expected-account example_user
```

已有本项目的会话文件且本人决定替换时，命令末尾加 `--replace`。不要因为报错就反复导出或换配置。

工具要求：

1. 第一次输入 `EXPORT`，才允许读取你指定的 Chrome 配置。
2. 它会显示唯一的账号数字标记和一个 X 官方核对链接。**亲自打开该链接，确认显示的 @用户名就是预期账号。** 这个标记检查不是服务器端登录令牌归属鉴权。
3. 账号确认无误后，按提示精确输入 `SAVE @example_user`（换成自己的预期用户名），才保存新文件。

缺少标记、多个不同账号标记、格式错误或取消确认时均拒绝保存；`--replace` 也不能绕过。若你已从独立可靠来源知道账号数字 ID，可增加 `--expected-account-id 数字ID` 来做额外严格匹配；不要从本次输出复制一个 ID 再声称做了独立验证。

Chrome 提取库可能读取所选配置的全部 Cookie、临时复制 Cookie 数据库，并触发 macOS 钥匙串授权。工具最终只保存 X/Twitter 域的未过期项。**“只保存 X”不等于“只读取 X”。** 它不联网、不自动上传，不输出 Cookie 值。不要把程序输出之外的原始日志、Cookie 文件或钥匙串信息发到聊天。

文件固定保存在本项目 `data/private/x_cookies.txt`，私有目录700、文件600。

## 3. 本人放到运行设备

如果程序运行在另一台 NAS，由你本人使用可信局域网文件传输方式，将这个文件放到对应项目的 `data/private/x_cookies.txt`。不要分享公开链接、发到聊天或上传 GitHub，也不要复制其他浏览器／Telegram 会话。

传输可能改变权限。确认它是普通文件、属主与容器运行用户一致、文件权限600、私有目录700；不要读取或打印内容来检查。

在运行设备的 `.env` 中设置：

```dotenv
ARCHIVER_X_COOKIE_FILE=/data/private/x_cookies.txt
```

等待队列空闲后，正常停止、重新启动容器使配置生效；首次启用后用一条新链接验证。

## 失效与停用

关闭 Chrome 不会自动删除 NAS 上已提供的会话，但 X 会话可能过期、被撤销或需要本人验证。出现失效提示时停止重试，由本人检查官方账号状态。

要停用会话访问，将 `.env` 的 `ARCHIVER_X_COOKIE_FILE` 留空，再在任务空闲时重启。不要自动删除文件。要撤销登录，使用 X 官方会话管理；这可能同时影响原浏览器登录。
