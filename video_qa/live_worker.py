"""Watch real media and verify the server's completion state before closing."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from .live_adapter import LiveSiteAdapter
from .models import CourseRef
from .reporting import EventReporter


async def run_live_course(context: Any, adapter: LiveSiteAdapter, course: CourseRef, reporter: EventReporter,
                          *, replay: bool = False, play_all: bool = False,
                          owned_pages: set | None = None) -> dict[str, Any]:
    page = None
    observer_tasks: set[asyncio.Task] = set()
    metrics = {"heartbeat_ok": 0, "heartbeat_failed": 0, "finish_ok": 0, "finish_failed": 0}
    answered = 0
    resume_attempts = 0
    refresh_attempts = 0
    started = time.monotonic()
    document_generation = 0
    confirmed_generation: int | None = None
    finish_requests: dict[Any, int] = {}

    def observe_navigation(frame: Any) -> None:
        nonlocal document_generation, confirmed_generation
        if frame == page.main_frame:
            document_generation += 1
            confirmed_generation = None

    def observe_request(request: Any) -> None:
        if (request.method == "POST" and request.url.startswith(adapter.base_url + "/")
                and urlsplit(request.url).path == adapter.api_prefix + "finish"):
            # Bind at request start, not when its response/body finally arrives.
            # A reload can finish before the previous document's response does.
            finish_requests[request] = document_generation

    def discard_request(request: Any) -> None:
        finish_requests.pop(request, None)

    async def record_response(response: Any) -> None:
        nonlocal confirmed_generation
        path = urlsplit(response.url).path
        endpoint = path.removeprefix(adapter.api_prefix)
        if not response.url.startswith(adapter.base_url + "/") or endpoint not in {"finishRate", "finish"} or response.request.method != "POST":
            return
        request_generation = finish_requests.pop(response.request, None)
        try:
            # CDP may never provide a body for a failed response. Its HTTP status
            # is sufficient, and diagnostics must never stall course completion.
            body = await asyncio.wait_for(response.json(), timeout=5) if response.status == 200 else None
            ok = response.status == 200 and isinstance(body, dict) and body.get("success") is True and body.get("code") == 200
        except Exception:
            ok = False
        label = "heartbeat" if endpoint == "finishRate" else "finish"
        metrics[label + ("_ok" if ok else "_failed")] += 1
        if label == "finish" and ok and request_generation == document_generation:
            confirmed_generation = request_generation
        reporter.log("site_response", course_id=course.course_id, endpoint=endpoint, success=ok, status=response.status)

    def observe(response: Any) -> None:
        task = asyncio.create_task(record_response(response))
        observer_tasks.add(task)
        task.add_done_callback(observer_tasks.discard)

    async def play_attempt() -> None:
        nonlocal answered, resume_attempts
        if refresh_attempts:
            reporter.log("course_refresh_started", course_id=course.course_id, attempt=refresh_attempts)
        quizzes = await adapter.open_course(page, course, refresh=refresh_attempts > 0,
                                            already_opening=refresh_attempts == 0)
        attempt_generation = document_generation
        if await adapter.visible_dialog(page) is None:
            await asyncio.wait_for(adapter.play(page), timeout=45)
        last_position = -1.0
        last_progress = time.monotonic()
        last_report = 0.0
        last_resume_attempt = float('-inf')
        while True:
            if await adapter.visible_dialog(page) is not None:
                answer = await adapter.answer_visible_dialog(page, quizzes)
                answered += 1
                reporter.log("quiz_answered", course_id=course.course_id, **answer)
                last_progress = time.monotonic()
                # Some player versions resume themselves; some keep paused.
                if await adapter.visible_dialog(page) is None:
                    state = await adapter.video_state(page)
                    if state["paused"] and not state["ended"]:
                        await asyncio.wait_for(adapter.play(page), timeout=45)
                continue
            state = await adapter.video_state(page)
            if state["error"]:
                raise RuntimeError(f"媒体播放失败，浏览器错误码 {state['error']['code']}")
            now = time.monotonic()
            if state["currentTime"] > last_position:
                last_position = state["currentTime"]
                last_progress = now
            if now - last_report >= 10:
                reporter.log("video_progress", course_id=course.course_id,
                             current_seconds=round(state["currentTime"], 1),
                             duration_seconds=round(state["duration"], 1))
                last_report = now
            if state["ended"]:
                reporter.log("video_ended", course_id=course.course_id)
                break
            if now - last_progress >= adapter.config.stall_timeout:
                raise TimeoutError("视频长时间没有进度，可能缓冲失败或页面未恢复播放")
            if state['paused'] and now - last_resume_attempt >= 2:
                # A browser-side pause can occur after play() already resolved.
                # Never resume across an unanswered dialog, and do not count a
                # successful play() call as actual progress for the stall timer.
                if await adapter.visible_dialog(page) is None:
                    last_resume_attempt = now
                    resume_attempts += 1
                    await asyncio.wait_for(adapter.play(page), timeout=45)
                    reporter.log('playback_resume_requested', course_id=course.course_id,
                                 current_seconds=round(state['currentTime'], 1), attempt=resume_attempts)
            await asyncio.sleep(adapter.config.poll_interval)

        await asyncio.sleep(adapter.config.post_end_wait)
        if not (replay or play_all) and not await adapter.verify_completed(page, course_id=course.course_id, timeout=adapter.config.verify_timeout):
            raise TimeoutError("视频结束后，课程树仍未确认学完；页面不会被计为成功")
        if observer_tasks:
            await asyncio.gather(*list(observer_tasks), return_exceptions=True)
        if replay and confirmed_generation != attempt_generation:
            raise RuntimeError('回放自然结束，但未观察到网站成功接受结束上报')

    try:
        # Acquire one slot/page for the whole course, including refreshes. Page
        # creation and reporting failures are not recoverable by reloading media.
        page = await adapter.new_course_page(context, course, reporter=reporter)
        if owned_pages is not None:
            owned_pages.add(page)
        page.on("framenavigated", observe_navigation)
        page.on("request", observe_request)
        page.on("requestfailed", discard_request)
        page.on("response", observe)
        reporter.log("course_started", course_id=course.course_id, name=course.name)
        while True:
            try:
                await play_attempt()
                break
            except (RuntimeError, TimeoutError, PlaywrightError) as exc:
                if page.is_closed():
                    raise
                # Settle callbacks from the old document before retrying; keep
                # cumulative diagnostics, but never reuse an earlier finish to
                # pass a later replay attempt. Cancellation is not caught here.
                if observer_tasks:
                    await asyncio.gather(*list(observer_tasks), return_exceptions=True)
                refresh_attempts += 1
                delay = min(5 * refresh_attempts, 30)
                reporter.log("course_refresh_scheduled", course_id=course.course_id,
                             attempt=refresh_attempts, delay_seconds=delay,
                             error=f"{type(exc).__name__}: {exc}")
                # Release the UI lock while waiting so other tabs keep starting,
                # answering questions and finishing. No maximum retry count.
                await asyncio.sleep(delay)
        elapsed = round(time.monotonic() - started, 3)
        result = {"answered": answered, "elapsed_seconds": elapsed,
                  "resume_attempts": resume_attempts, "refresh_attempts": refresh_attempts, **metrics}
        if replay or play_all:
            result.update(baseline_completed=course.completed, newly_completed=False, completion_transition_checked=False)
        reporter.log("course_played" if play_all else "course_replayed" if replay else "course_completed", course_id=course.course_id, **result)
        return result
    except Exception as exc:
        # Store the phase/status rather than headers, cookies or whole page dumps.
        reporter.log("course_failed", course_id=course.course_id, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if page is not None:
            page.remove_listener("framenavigated", observe_navigation)
            page.remove_listener("request", observe_request)
            page.remove_listener("requestfailed", discard_request)
            page.remove_listener("response", observe)
        finish_requests.clear()
        for task in list(observer_tasks):
            task.cancel()
        if observer_tasks:
            await asyncio.gather(*list(observer_tasks), return_exceptions=True)
        if page is not None and not page.is_closed():
            await asyncio.shield(page.close())
        if owned_pages is not None:
            owned_pages.discard(page)
        if page is not None:
            reporter.log("page_closed", course_id=course.course_id)
