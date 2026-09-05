"""Open a dedicated visible Chromium for manual login and CDP attachment."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .live_config import LiveConfig, load_config


def debugging_ready(endpoint: str) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(endpoint.rstrip('/') + '/json/version', timeout=1) as response:
            body = json.load(response)
        return bool(body.get('webSocketDebuggerUrl'))
    except Exception:
        return False


def resolve_browser_executable(config: LiveConfig) -> Path:
    """Resolve an executable without creating a Playwright driver connection."""
    if config.browser_path:
        executable = config.path(config.browser_path)
        if not executable.is_file():
            raise RuntimeError(f'配置的浏览器文件不存在：{executable}')
        return executable

    roots = [Path(value) for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')
             if (value := os.environ.get(key))]
    # Stock Chrome/Edge have the media codecs needed by the real learning site.
    for suffix in ('Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe'):
        for root in roots:
            executable = root / suffix
            if executable.is_file():
                return executable

    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        cache = Path(local_app_data) / 'ms-playwright'
        candidates = list(cache.glob('chromium-*/chrome-win64/chrome.exe'))
        candidates += list(cache.glob('chromium-*/chrome-win/chrome.exe'))
        candidates = [path for path in candidates if re.search(r'chromium-\d+', str(path)) and path.is_file()]
        if candidates:
            return max(candidates, key=lambda path: int(re.search(r'chromium-(\d+)', str(path)).group(1)))
    raise RuntimeError('找不到 Chrome/Edge/Chromium。请安装浏览器或在 live_settings.json 指定 browser_path')


@dataclass
class BrowserLaunch:
    reused: bool
    executable: Path | None = None
    process: subprocess.Popen | None = None
    log_path: Path | None = None


def launch_browser(config: LiveConfig, *, initial_url: str | None = None) -> BrowserLaunch:
    config.validate()
    if debugging_ready(config.cdp_url):
        return BrowserLaunch(reused=True)
    executable = resolve_browser_executable(config)
    profile = config.path(config.browser_profile).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    log_directory = config.path('browser_startup_logs')
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.log')
    command = [
        str(executable), f'--remote-debugging-port={urlsplit(config.cdp_url).port}',
        '--remote-debugging-address=127.0.0.1', f'--user-data-dir={profile}',
        '--no-first-run', '--no-default-browser-check', '--enable-logging=stderr',
        '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
        '--disable-background-media-suspend',
        '--mute-audio',
        '--disable-backgrounding-occluded-windows', initial_url or config.site_url,
    ]
    with log_path.open('wb') as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if debugging_ready(config.cdp_url):
            return BrowserLaunch(False, executable, process, log_path)
        code = process.poll()
        if code is not None and code != 0:
            raise RuntimeError(f'浏览器提前退出，退出码：{code}；浏览器：{executable}；启动日志：{log_path}')
        # Exit 0 may mean the request was forwarded to an existing profile;
        # allow its listener time to become available before reporting failure.
        time.sleep(0.2)
    raise RuntimeError(
        f'20 秒内未发现调试端口（进程 {process.pid}，退出码 {process.poll()}）。'
        f'若专用配置窗口已打开，请手动关闭该窗口再启动。启动日志：{log_path}'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='打开可连接的 Chrome/Chromium，供用户手动登录')
    parser.add_argument('--config', type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        launched = launch_browser(config)
        if launched.reused:
            print(f'调试浏览器已在运行：{config.cdp_url}。请在该浏览器中完成登录，可停留在网站首页或我的课程。')
            return 0
        print(f'浏览器已打开：{launched.executable}')
        print(f'调试地址已就绪：{config.cdp_url}')
        print('请自行登录，可停留在网站首页或我的课程，无需手动打开视频。若另有视频正在播放，请先暂停。')
        print('然后运行 02_正式网站兼容性检查.bat；登录窗口请保持打开。')
        return 0
    except Exception as exc:
        print(f'打开失败：{type(exc).__name__}: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
