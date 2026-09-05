"""One entry: clear this project's closed profile, manual login, detected playback."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from .browser_launcher import debugging_ready, launch_browser, resolve_browser_executable
from .live_cli import run_attached
from .live_config import LiveConfig, load_config


def profile_in_use(profile: Path, browser_name: str = '') -> bool:
    """Read browser command lines without logging them or closing any process."""
    if os.name != 'nt':
        raise RuntimeError('自动清理登录资料仅支持 Windows；无法确认占用状态，未清理')
    script = r"""$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$taskNames=@('chrome.exe','msedge.exe','chromium.exe','chrome-headless-shell.exe')
if($env:VIDEO_QA_BROWSER_NAME){$taskNames += $env:VIDEO_QA_BROWSER_NAME}
$taskProcesses=@(Get-CimInstance Win32_Process | Where-Object {$_.Name -in $taskNames} |
  Select-Object -ExpandProperty CommandLine)
ConvertTo-Json -InputObject $taskProcesses -Compress
"""
    env = dict(os.environ, VIDEO_QA_BROWSER_NAME=browser_name)
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            env=env, capture_output=True, text=True, encoding='utf-8', timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('process query failed')
        commands = json.loads(result.stdout)
        if not isinstance(commands, list) or any(not isinstance(command, str) or not command for command in commands):
            raise RuntimeError('process command lines unavailable')
    except Exception as exc:
        raise RuntimeError('无法确认浏览器是否占用专用资料，未清理；请关闭专用浏览器后重试') from exc
    option = re.compile(r'(?:^|\s)(?:"--user-data-dir=([^"]+)"|--user-data-dir="([^"]+)"|'
                        r'--user-data-dir=([^\s"]+)|--user-data-dir\s+"([^"]+)"|--user-data-dir\s+([^\s"]+))', re.I)
    for command in commands:
        for match in option.finditer(command):
            value = next(value for value in match.groups() if value is not None)
            if os.path.normcase(str(Path(value).resolve())) == os.path.normcase(str(profile)):
                return True
    return False


def _reject_link(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f'专用资料包含链接或重解析点，未清理：{path.name}')


def clear_login_profile(config: LiveConfig) -> bool:
    config.validate()
    project = config.base_directory.resolve()
    profile = config.path(config.browser_profile)
    expected = project / 'chromium-live-profile'
    _reject_link(profile)
    if profile.resolve() != expected or profile.resolve().parent != project:
        raise ValueError('自动清理只允许本项目直属的 chromium-live-profile，不会删除其他路径')
    if (project / 'live_run.lock').exists():
        raise RuntimeError('还有播放任务或运行锁；请先正常停止任务，未清理登录资料')
    if debugging_ready(config.cdp_url) or profile_in_use(expected, Path(config.browser_path).name):
        raise RuntimeError('专用浏览器仍在运行，请先手动关闭它再启动；未清理登录资料，不会关闭其他浏览器')
    if not profile.exists():
        return False
    if not profile.is_dir():
        raise ValueError('专用资料路径不是文件夹，未清理')
    # Validate the entire tree before removing anything. Do not follow a
    # junction/symlink into unrelated user data, even if it is inside the profile.
    for directory, folders, files in os.walk(profile, followlinks=False):
        for name in folders + files:
            _reject_link(Path(directory) / name)
    shutil.rmtree(profile)
    return True


def start_session(config: LiveConfig) -> dict:
    config.validate()
    lock = config.path('startup.lock')
    try:
        handle = lock.open('x', encoding='ascii')
    except FileExistsError as exc:
        raise RuntimeError('已有启动/播放流程或 startup.lock，请先停止原脚本，不要重复双击') from exc
    try:
        with handle:
            handle.write(str(os.getpid()))
        # Missing executable must not cost the user their previous session.
        resolve_browser_executable(config)
        removed = clear_login_profile(config)
        print('已清理本项目专用浏览器的旧登录资料（未备份，需重新登录）。' if removed else '没有旧登录资料，将使用全新会话。', flush=True)
        launched = launch_browser(config)
        if getattr(launched, 'reused', False):
            raise RuntimeError('启动期间出现已有调试浏览器，未开始播放；请关闭专用窗口后重试')
        input('请在刚打开的浏览器中登录；登录完成后回到这里按回车，开始检测并播放。Ctrl+C 取消：')
        return asyncio.run(run_attached(config, action='run'))
    finally:
        lock.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='清理本项目旧登录资料、打开浏览器、登录后检测并发播放')
    parser.add_argument('--config', type=Path)
    args = parser.parse_args(argv)
    try:
        result = start_session(load_config(args.config))
        print(f"本次完成 {result['completed']}/{result['total']}，剩余 {result['remaining']}；报告：{result.get('run_directory', '')}")
        return 0 if result.get('failed', 0) == 0 else 1
    except (KeyboardInterrupt, EOFError):
        print('已停止；本次任务页面会清理，浏览器仍保留。下次启动前请关闭专用浏览器。')
        return 130
    except Exception as exc:
        print(f'启动/运行失败：{type(exc).__name__}: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
