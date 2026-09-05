"""Attach to Chromium: read-only compatibility check or explicit live run."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .answers import AnswerBank
from .live_adapter import LiveSiteAdapter
from .live_config import LiveConfig, infer_graph_id, load_config, origin
from .live_worker import run_live_course
from .live_navigation import discover_graph_from_overview
from .live_replay import run_replay
from .reporting import EventReporter
from .scheduler import run_bounded


def _matches_site(url: str, config: LiveConfig) -> bool:
    try:
        return origin(url) == origin(config.site_url)
    except ValueError:
        return False


async def _session(browser: Any, config: LiveConfig) -> tuple[Any, Any, str]:
    matches = [(context, [page for page in context.pages if _matches_site(page.url, config)]) for context in browser.contexts]
    matches = [(context, pages) for context, pages in matches if pages]
    if len(matches) != 1:
        raise RuntimeError("请在本次启动的 Chromium 中登录目标网站，并打开课程页面")
    context, pages = matches[0]
    graph_id = config.graph_id
    if not graph_id:
        urls = [page.url for page in pages]
        for page in pages:
            urls.extend(await page.locator('a[href*="graphId="]').evaluate_all("nodes => nodes.map(node => node.href)"))
        has_graph_url = any(parse_qs(urlsplit(url).query).get('graphId')
                            or parse_qs(urlsplit(urlsplit(url).fragment).query).get('graphId') for url in urls)
        graph_id = infer_graph_id(urls) if has_graph_url else await discover_graph_from_overview(context, config)
    matching = [page for page in pages if f"graphId={graph_id}" in page.url]
    seed = matching[0] if matching else pages[0]
    return context, seed, graph_id


async def _check(adapter: LiveSiteAdapter, context: Any) -> dict[str, Any]:
    courses = await adapter.discover_courses()
    pending = [course for course in courses if not course.completed]
    sample = pending[0] if pending else (courses[0] if courses else None)
    questions = await adapter.questions(adapter.seed_page, sample.course_id) if sample else []
    videos = []
    for page in context.pages:
        if _matches_site(page.url, adapter.config) and await adapter.video_locator(page) is not None:
            state = await adapter.video_state(page)
            videos.append({"path": urlsplit(page.url).path, **state})
    return {
        "status": "read_checks_passed",
        "site_origin": adapter.base_url,
        "course_count": len(courses),
        "pending_count": len(pending),
        "sample_question_count": len(questions),
        "sample_answers_available": sum(bool(quiz.correct_letters) for quiz in questions),
        "bank_entries": len(adapter.bank.entries),
        "open_videos": videos,
        "playback_and_submission_verified": False,
        "note": "只读结果仅验证连接、课程和题目读取；真实播放、答题提交和完成上报需单门试跑。",
    }


async def run_attached(config: LiveConfig, *, action: str, live_output: bool = True) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    config.validate()
    if action not in {"check", "run", "replay", "all"}:
        raise ValueError("action must be check, run, replay or all")
    reporter = EventReporter(config.path(config.output_root), live_output=live_output)
    lock_path = config.path("live_run.lock")
    acquired = False
    try:
        if action in {"run", "replay", "all"}:
            try:
                with lock_path.open("x", encoding="ascii") as handle:
                    handle.write(str(os.getpid()))
                acquired = True
            except FileExistsError as exc:
                raise RuntimeError("已有运行任务（live_run.lock）。先结束该任务；若此前异常退出，确认任务已停止后删除这个锁文件") from exc
        bank = AnswerBank.load(config.path(config.bank_path)) if config.bank_path else AnswerBank()
        async with async_playwright() as playwright:
            attach_options = {"timeout": 10_000}
            if "no_defaults" in inspect.signature(playwright.chromium.connect_over_cdp).parameters:
                attach_options["no_defaults"] = True
            try:
                browser = await playwright.chromium.connect_over_cdp(config.cdp_url, **attach_options)
            except Exception as exc:
                raise RuntimeError("无法连接调试浏览器，请先双击 01_打开登录浏览器.bat") from exc
            # Exiting Playwright disconnects this client. Do not close the
            # attached browser or context, which belong to the user.
            context, seed, graph_id = await _session(browser, config)
            adapter = LiveSiteAdapter(config, seed, graph_id=graph_id, bank=bank)
            checks = await _check(adapter, context)
            if action == "check":
                reporter.write_summary(checks)
                reporter.log("compatibility_checked", **checks)
                if live_output:
                    print(f"只读检查通过：课程 {checks['course_count']} 门，未完成 {checks['pending_count']} 门，题库 {checks['bank_entries']} 题。")
                    print(checks["note"])
                return {**checks, "run_directory": str(reporter.run_directory)}
            if any(not video["paused"] and not video["ended"] for video in checks["open_videos"]):
                raise RuntimeError("已有手动打开的视频正在播放，请先暂停它，再开始自动运行")

            courses = await adapter.discover_courses()
            if action in {'replay','all'}:
                summary = await run_replay(context, adapter, courses, reporter, all_courses=action == 'all')
                reporter.write_summary(summary)
                reporter.log('replay_finished', **summary)
                return {**summary, 'run_directory':str(reporter.run_directory)}
            attempted: set[str] = set()
            results = []
            peak = 0
            reporter.log("run_started", mode="live", base_url=adapter.base_url, course_count=len([c for c in courses if not c.completed]), concurrency=config.concurrency)
            while True:
                pending = [course for course in courses if not course.completed and course.course_id not in attempted]
                if config.course_limit:
                    pending = pending[:max(0, config.course_limit - len(attempted))]
                if not pending:
                    break
                attempted.update(course.course_id for course in pending)

                async def handler(course: Any) -> dict[str, Any]:
                    return await run_live_course(context, adapter, course, reporter)

                bounded = await run_bounded(pending, concurrency=config.concurrency, handler=handler)
                results.extend(bounded.items)
                peak = max(peak, bounded.peak_active)
                courses = await adapter.discover_courses()
                reporter.log("courses_rescanned", remaining=sum(not course.completed for course in courses))
            summary = {
                "status": "completed" if all(item.success for item in results) else "partial_failure",
                "mode": "live", "total": len(results),
                "completed": sum(item.success for item in results),
                "failed": sum(not item.success for item in results),
                "peak_active": peak,
                "remaining": sum(not course.completed for course in courses),
                "failures": [{"course_id": item.item_id, "error": item.error} for item in results if not item.success],
                "course_results": [{"course_id": item.item_id, "success": item.success, "value": item.value} for item in results],
            }
            reporter.write_summary(summary)
            reporter.log("run_completed", **summary)
            return {**summary, "run_directory": str(reporter.run_directory)}
    except BaseException as exc:
        summary = {"status": "interrupted" if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) else "error", "error": str(exc)}
        reporter.write_summary(summary)
        reporter.log("run_failed", **summary)
        if live_output:
            print(f"报告：{reporter.run_directory}")
        raise
    finally:
        if acquired:
            lock_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="正式网站课程兼容性检查与视频队列")
    parser.add_argument("action", choices=["check", "run", "replay", "all"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        overrides = {}
        if args.concurrency is not None:
            overrides["concurrency"] = args.concurrency
        if args.limit is not None:
            overrides["course_limit"] = args.limit
        config = replace(config, **overrides).validate()
        result = asyncio.run(run_attached(config, action=args.action))
        if args.action == "run":
            print(f"本次完成 {result['completed']}/{result['total']}，失败 {result['failed']}，峰值并发 {result['peak_active']}，全站剩余 {result['remaining']}。")
        elif args.action == 'replay':
            print(f"回放通过 {result['replayed']}/{result['total']}，失败 {result['failed']}，峰值任务 {result['peak_active']}，实际同时播放峰值 {result['peak_playing']}。不验证或计入新增学习完成。")
        elif args.action == 'all':
            print(f"本轮播放结束 {result['played']}/{result['total']}，失败 {result['failed']}，实际同时播放峰值 {result['peak_playing']}。不筛选、不回查学习完成状态。")
        print(f"报告：{result['run_directory']}")
        return 0 if result.get("failed", 0) == 0 else 1
    except KeyboardInterrupt:
        print("已停止；已尝试清理本次任务窗口，登录浏览器继续保留。如有异常，请检查是否残留任务窗口。")
        return 130
    except Exception as exc:
        print(f"运行失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
