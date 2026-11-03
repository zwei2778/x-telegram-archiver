# 第三方软件

本项目的个人非商业许可仅覆盖本仓库原创代码与文档，不替代、不限制或重新许可下面的软件。以下是所声明直接依赖版本的上游许可；传递依赖保留其原有许可及声明。

| 直接依赖 | 上游许可 | 官方发布页 |
|---|---|---|
| yt-dlp 2026.10.26 | Unlicense | https://pypi.org/project/yt-dlp/2026.10.26/ |
| requests 2.34.2 | Apache-2.0 | https://pypi.org/project/requests/2.34.2/ |
| imageio-ffmpeg 0.7.0（Python封装） | BSD-2-Clause | https://pypi.org/project/imageio-ffmpeg/0.7.0/ |
| Telethon 1.45.0 | MIT | https://pypi.org/project/Telethon/1.45.0/ |
| python-socks 2.8.2 | Apache-2.0 | https://pypi.org/project/python-socks/2.8.2/ |

`imageio-ffmpeg` 的平台 wheel 可能包含 FFmpeg 可执行文件。**FFmpeg 二进制不因 Python 封装使用 BSD 许可而变为 BSD。** 实际许可与源码义务取决于该二进制的构建选项及上游分发文件，参见 [FFmpeg 官方说明](https://www.ffmpeg.org/legal.html) 及安装包自带声明。

本仓库与首次源码发布包不内置第三方 wheel、FFmpeg/Mihomo 二进制、浏览器或预构建 Docker 镜像。Dockerfile 在使用者本机安装依赖，基于官方 `python:3.12-slim-bookworm`。基础镜像及系统组件保留各自许可。

后续如发布离线依赖包、安装程序或预构建容器镜像，需要另行核对并履行实际分发的所有第三方组件许可、声明及相应源码义务；本文件不能替代这些材料。
