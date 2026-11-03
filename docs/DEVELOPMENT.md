# 开发与验证

公开版以已实测的手动链接归档逻辑为基础，新增从零 `setup-user` 引导和更严格的可选 X 会话导出。它不是私人 NAS 工作目录的完整副本。

浏览器自动采集、HTTP内部入队、夜间二次备份均不在本次发布中；没有启用这些功能的生产参数或网页入口。不要把待研究功能当作已实现特性。

## 运行假数据测试

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-runtime.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

测试使用假账号、假视频、临时目录及临时数据库，不应访问真实浏览器、Telegram、X、NAS或你的 `data`。本仓库不包含会打开真实浏览器的历史测试脚本。

可用 `python archiver.py check` 核对依赖及FFmpeg；不要为测试而启动第二个真实消费者。数据库状态诊断使用只读连接，不调用会重置处理中任务的 `database()` 初始化函数。

## 发布边界

只打包审核后的公开文件。不要把 `.env`、`data`、`secrets`、`proxy-state`、浏览器配置、会话、日志、历史运维文档或本地虚拟环境加入版本控制。`.gitignore` 和 `.dockerignore` 是第二层防护，不能代替发布前逐项核对。

源码中的安全边界包括：仅本人收藏夹输入、目标频道权限检查、唯一消费者、持久化去重、确认上传后落盘、临时文件限额、凭据错误脱敏。改动时保留相关假数据验证。

本项目使用个人非商业源码许可。可以私下修改自用；如希望发布修改版、用于组织或商用，请先通过 Issues 与作者协商书面许可。问题反馈与教程纠错欢迎通过 Issues 提交，请勿附带任何私人数据。
