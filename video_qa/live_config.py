"""Configuration for attached, manually authenticated browser sessions."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("网站地址必须是没有账号密码的 http/https 地址")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def infer_graph_id(urls: list[str]) -> str:
    found = set()
    for url in urls:
        parsed = urlsplit(url)
        for query in (parsed.query, urlsplit(parsed.fragment).query):
            found.update(value for value in parse_qs(query).get("graphId", []) if value and value != "<redacted>")
    if len(found) != 1:
        raise ValueError("无法唯一确定课程计划：请在浏览器打开一门课程，或在 live_settings.json 填写 graph_id")
    return found.pop()


@dataclass(frozen=True)
class LiveConfig:
    site_url: str = "https://aqxx.nju.edu.cn"
    cdp_url: str = "http://127.0.0.1:9222"
    graph_id: str = ""
    concurrency: int = 15
    tab_open_interval: float = 1.0
    separate_course_windows: bool = False
    course_limit: int = 0
    post_end_wait: float = 15
    verify_timeout: float = 60
    stall_timeout: float = 120
    poll_interval: float = 0.5
    bank_path: str = ""
    output_root: str = "live_runs"
    browser_profile: str = "chromium-live-profile"
    browser_path: str = ""
    auth_storage_key: str = ""
    auth_header: str = "X-Access-Token"
    base_directory: Path = PROJECT_ROOT

    def path(self, value: str) -> Path:
        result = Path(value)
        return result if result.is_absolute() else self.base_directory / result

    def validate(self) -> "LiveConfig":
        if origin(self.site_url) != self.site_url.rstrip("/"):
            raise ValueError("site_url 只填写网站根地址，不包含路径或查询参数")
        cdp = urlsplit(self.cdp_url)
        if (cdp.scheme != "http" or cdp.hostname not in {"127.0.0.1", "localhost"}
                or cdp.path not in {"", "/"} or cdp.query or cdp.fragment
                or cdp.username or cdp.password or not cdp.port):
            raise ValueError("cdp_url 必须是带端口的本机 HTTP 调试地址")
        if not isinstance(self.concurrency, int) or not 1 <= self.concurrency <= 15:
            raise ValueError("concurrency 必须是 1 到 15")
        if not isinstance(self.separate_course_windows, bool):
            raise ValueError('separate_course_windows 必须是 true 或 false')
        if not isinstance(self.course_limit, int) or self.course_limit < 0:
            raise ValueError("course_limit 必须是非负整数，0 表示全部")
        for name in ("verify_timeout", "stall_timeout", "poll_interval", "post_end_wait", "tab_open_interval"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (name != "post_end_wait" and value == 0):
                raise ValueError(f"{name} 必须是有效的正数秒数")
        if self.auth_header.lower() not in {"x-access-token", "authorization"}:
            raise ValueError("auth_header 支持 X-Access-Token 或 Authorization")
        return self


def load_config(path: Path | None = None) -> LiveConfig:
    path = path or PROJECT_ROOT / "live_settings.json"
    raw = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件应为 JSON 对象")
    allowed = {item.name for item in fields(LiveConfig)} - {"base_directory"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"未知配置项：{', '.join(sorted(unknown))}")
    return LiveConfig(**raw, base_directory=path.resolve().parent).validate()
