# 视频并发测试器

面向 NJU 实验室安全育人智慧平台的视频课程工具。手动登录后，识别课程计划、检查未完成视频，并在同一浏览器窗口中并发播放。

本仓库只提供运行所需源码、启动入口、依赖清单和空白配置示例，不附带 Python、Playwright 安装环境或 Chrome / Chromium 浏览器。

## 安装环境

适用 Windows 10 / 11；需要 Python 3.10+ 和 Chrome / Edge。可从 [Python 官网](https://www.python.org/downloads/windows/)安装 Python。

在解压后的项目文件夹打开 PowerShell，运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item -LiteralPath .\live_settings.example.json -Destination .\live_settings.json
```

如果系统提供的是 `py` 命令，第一行可改成 `py -3 -m venv .venv`。配置文件只需首次复制；后续更新不要覆盖自己的 `live_settings.json`。

脚本优先连接本机已安装的 Chrome / Edge，不必再下载整套浏览器。找不到浏览器时，可以在 `live_settings.json` 的 `browser_path` 中填写浏览器可执行文件路径，或者只安装 Chromium：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

浏览器缓存留在本机，不放入仓库。有关依赖安装，参见 [Playwright Python 官方说明](https://playwright.dev/python/docs/library)。

## 启动

1. 关闭本工具上次打开的专用浏览器，确认上次脚本已结束。
2. 双击 **启动检测播放.bat**。
3. 在新浏览器里自行输入账号、密码及验证码，完成登录。
4. 回到脚本窗口按回车，开始检测和播放。

如果账号有多个课程计划，请先在浏览器打开目标计划中的一门课程，再按回车。按 `Ctrl+C` 停止。

启动器优先使用 `VIDEO_QA_PYTHON` 指定的解释器，其次使用项目内 `.venv`，最后尝试系统 `python`。也可以直接运行：

```powershell
.\.venv\Scripts\python.exe -m video_qa.startup
```

## 设置

编辑自己的 `live_settings.json`；示例中的个人字段均为空。

| 字段 | 用途 |
| --- | --- |
| `concurrency` | 同时处理的课程数量，默认 15，允许 1–15 |
| `tab_open_interval` | 相邻打开动作间隔，默认 1 秒 |
| `graph_id` | 留空时从当前页面或课程总览发现课程计划 |
| `post_end_wait` | 视频结束后的缓冲时间，默认 15 秒 |
| `bank_path` | 可选本地题库路径；留空表示不加载外部题库 |
| `browser_path` | 浏览器路径；留空自动查找 |

已完成视频会跳过；失败课程在原标签页刷新重试，等待从 5 秒逐步增加到 30 秒。弹题优先匹配页面提供的答案或本地题库；没有匹配时，当前逻辑会默认选 A，因此不保证答题正确。完成状态以站点实际返回为准。

每次启动会清理**本项目直属的 `chromium-live-profile`**，所以需要重新登录。该目录包含专用浏览器的 Cookie 和缓存；脚本会检查占用及链接情况，不应将其改成个人浏览器资料目录。运行期间不要重复双击启动入口。

## 关联项目与致谢

“实验室安全自动考试”是另一位作者的项目：[@QuTongxi](https://github.com/QuTongxi)，原仓库为 [QuTongxi/NJU_Aqxx_Test_v2](https://github.com/QuTongxi/NJU_Aqxx_Test_v2)。请直接前往原仓库获取代码、题库和使用说明。本仓库不打包该项目及其本地修改、题库或运行环境。

如需给视频弹题使用题库，请自行从合法来源获取兼容的 JSON 文件，并把路径填入 `bank_path`。支持 `{ "题干": ["答案文本"] }` 格式。题库保存在本机，默认忽略上传。

## 文件与隐私

本仓库包含 `video_qa/` 必要运行模块、两个启动批处理、`requirements.txt`、本说明及 `live_settings.example.json`。

以下内容不在发布包中，且已加入 `.gitignore` 防止常规误提交：

- 真实 `live_settings.json`、账号、密码、Cookie、Token、登录状态及个人目录路径。
- `chromium-live-profile/`、浏览器缓存、启动日志和运行锁。
- `live_runs/`、检测结果、截图、录屏、HAR、调试日志及个人学习记录。
- `_归档/`、备份、回归测试、探测脚本、本地模拟站点和测试素材。
- `.venv/`、Python、Playwright、Chrome / Chromium 安装目录及下载包。

日常运行仍会在本机产生报告和浏览器资料，它们不随源码上传。`.gitignore` 采用允许清单；以后新增源码应显式加入允许清单并重新检查内容，不要使用强制添加来提交个人配置或运行目录。

本次发布做了静态语法、模块依赖、配置及压缩包检查；没有为打包操作登录真实学习平台，也没有运行实际课程。
