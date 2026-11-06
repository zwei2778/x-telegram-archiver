# X → Telegram 视频归档

把 X / Twitter 帖子链接发送到你自己的 Telegram **Saved Messages（收藏夹）**，程序下载视频，上传到你选择的私人频道或群组，并附上原帖链接。

**MIT License。** 可免费使用、复制、修改、合并、发布、分发、再许可和销售（包括商业用途）；副本须包含版权声明和许可声明。完整条款见 [LICENSE](LICENSE)。

## 用起来是什么样

1. 你向 Telegram 收藏夹发送一条或多条 X 帖子链接。
2. 程序立即接收并保存到队列，不必等待上一条视频上传完。
3. 对应回复持续更新解析、下载和上传进度；同阶段约每 10 秒更新一次。
4. Telegram 确认上传后显示完成，并清理本次任务的临时视频。

视频逐条处理；程序会去重，重启后保留队列。文件超过程序上限时停止，不自动压低画质。

## 先准备这三样

- 一台安装了 **Docker 和 Docker Compose**、能正常访问 Telegram 和 X 的电脑或 NAS。
- 你自己的 Telegram 账号，以及一个你拥有或管理、能够发帖的私人频道或群组。
- 你自己在 [my.telegram.org](https://my.telegram.org) 创建的 Telegram API 应用信息。手机号、验证码、API hash 等只在你本机终端隐藏输入。

这是需要自己部署的命令行工具，暂时没有双击安装的图形应用。X 书签、点赞自动同步和夜间二次备份**尚未提供**。

## 最短安装路线

下面命令在**准备运行程序的那台设备**的终端执行。首次设置需要可交互终端。

### 1. 下载

从 [Releases](https://github.com/zwei2778/x-telegram-archiver/releases) 下载发布包并解压；也可以：

```sh
git clone https://github.com/zwei2778/x-telegram-archiver.git
cd x-telegram-archiver
```

### 2. 准备目录与配置

```sh
mkdir -p data
chmod 700 data
cp .env.example .env
```

打开 `.env`，将 `ARCHIVER_UID`、`ARCHIVER_GID` 设为拥有 `data` 目录的用户与组编号。在 Linux 上可以用 `id -u`、`id -g` 查看；NAS 用户按 [NAS 说明](docs/NAS.md) 核对。不要把目录设成所有人可读写。

网络需要代理时，在 `.env` 设置 `ARCHIVER_PROXY_URL`。它必须是容器能访问的 HTTP CONNECT 代理地址，具体见 [网络说明](docs/NETWORK.md)。直连可用时留空。

### 3. 登录并选择保存位置

```sh
docker compose build
docker compose run --rm archiver setup-user
```

跟随终端提示，亲自输入 Telegram API 应用信息、手机号、验证码及可能的两步验证密码。程序会显示登录账号，列出你拥有或管理的可用频道／群组。选择目标，确认名称正确后输入 `BIND` 完成绑定。**不需要先创建 Bot。**

已有绑定时会拒绝覆盖。中途取消目标选择时，不会保存绑定；你刚建立的 Telegram 登录会话可能已保存在本机，重新运行 `setup-user` 可以继续设置。

### 4. 启动并发一条链接

```sh
docker compose up -d
docker compose logs --tail 20 archiver
```

看到个人账号模式已运行后，在 Telegram 收藏夹**新发**一条你有权归档的 X 帖子链接。先验证一个短视频，确认目标频道收到视频、进度回复更新。首次启动不会回扫全部收藏夹历史。

日常只需要继续发链接；Mac/NAS 哪台运行 Docker，就由哪台保持开机联网。X 下载会话有效时，**不需要 Chrome 一直开着**。

## 需要登录才能访问的 X 帖子

默认匿名访问。部分帖子需要你自己的 X 登录状态，按 [X 会话教程](docs/X_SESSION.md) 单独启用。

请使用**专用浏览器配置和预期账号核对**。可选导出工具要求两次本人确认，不接受在脚本或聊天里输入 X 密码；密码只在 X 官方网站输入。会话凭据仅在你自己的设备与 NAS 间由你管理。

## 日常命令

```sh
docker compose logs --tail 20 archiver  # 查看固定提示，不公开日志里的私人链接
docker compose stop                   # 正常停止；不要强制杀死正在上传的任务
docker compose up -d                  # 再次启动
```

在收藏夹发送 `/archive 帖子链接` 与直接发链接效果相同。已成功的帖子会跳过。失败任务可以重发；结果不确定时，先检查目标频道，再决定是否发送 `/retry 帖子链接`，以免重复。

## 已验证范围与限制

- 手动链接路径的基础版本已在真实 NAS 上完成约 151 MB 视频上传及临时清理验证。
- 当前程序单文件上限为 **1,900,000,000 字节**；尚未实测该上限边界。公开版新增的首次设置与导出防错流程使用假数据测试，未代表已完成所有设备的真实安装验证。
- 下载前要求约 6.7 GB 可用空间，为音视频分离下载、合并预留余量。
- 不保证找回已删除、无权限访问或被平台限制的帖子；X 页面或解析规则变化后可能需要更新依赖。
- 网络中断后不能保证绝对不重复；只有 Telegram 返回有效上传确认才记为成功。
- 正常成功或失败后清理本次任务；断电可能留有旧临时目录，程序不会擅自扫描删除其他文件。
- 不读取 X 点赞／书签列表，不要求购买 X 开发者 API。现有 Bot 模式是兼容功能，不是此教程的推荐入口。

## 反馈与开发

问题请发 [Issues](https://github.com/zwei2778/x-telegram-archiver/issues)。请只提供脱敏错误现象，不上传 Cookie、登录会话、API hash、手机号、验证码、私人视频或整个 `data` 目录。

离线测试与贡献边界见 [开发说明](docs/DEVELOPMENT.md)。依赖及二进制边界见 [THIRD_PARTY.md](THIRD_PARTY.md)。

## 许可

本项目采用 [MIT License](LICENSE)（Copyright (c) 2026 zwei2778）。

在副本或实质性部分中包含版权声明和许可声明的前提下，可以免费使用、复制、修改、合并、发布、分发、再许可和销售本软件，包括商业用途，并可以发布修改版本。第三方依赖保留各自许可证，见 [THIRD_PARTY.md](THIRD_PARTY.md)。

请只归档你有权保存的内容，并遵守所使用服务的规则。本仓库不包含任何作者账号的登录凭据或私人数据。
