"""Discover the current plan via the site's normal course-overview requests."""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .live_config import LiveConfig, origin


async def discover_graph_from_overview(context: Any, config: LiveConfig) -> str:
    page = await context.new_page()
    found: set[str] = set()
    ready = asyncio.Event()
    expected_path = '/api/jeecg-boot/jcedutec/knowledgeGraph/queryCourseTypeById'

    def observe(request: Any) -> None:
        parsed = urlsplit(request.url)
        if (request.method == 'GET' and parsed.path == expected_path
                and origin(request.url) == origin(config.site_url)):
            found.update(value for value in parse_qs(parsed.query).get('id', []) if value)
            if found:
                ready.set()

    page.on('request', observe)
    try:
        await page.goto(config.site_url.rstrip('/') + '/students/myCourse',
                        wait_until='domcontentloaded', timeout=30_000)
        if origin(page.url) != origin(config.site_url):
            raise RuntimeError('课程总览跳转到登录站点，请先完成登录')
        await page.locator('.custom-tree > li[role="treeitem"]').first.wait_for(timeout=20_000)
        plans = page.locator('.custom-tree > li[role="treeitem"]')
        if await plans.count() != 1:
            raise RuntimeError('存在多个课程计划：请打开目标计划的一门课程，或配置 graph_id 后重试')
        await asyncio.wait_for(ready.wait(), timeout=20)
        if len(found) != 1:
            raise RuntimeError('课程总览返回多个课程计划编号，请配置 graph_id 后重试')
        return found.pop()
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise RuntimeError('未能从课程总览识别计划，请确认已登录且“我的课程”能够正常显示') from exc
    finally:
        page.remove_listener('request', observe)
        await page.close()
