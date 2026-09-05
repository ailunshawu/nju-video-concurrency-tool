"""Bounded replay of completed courses; never asserts a new completion."""
from __future__ import annotations

import asyncio
import math
from typing import Any

from .live_worker import run_live_course
from .scheduler import run_bounded


async def run_replay(context: Any, adapter: Any, courses: list, reporter: Any, *, all_courses: bool = False) -> dict:
    # Select short completed courses without opening players during selection.
    finished = list(courses) if all_courses else [course for course in courses if course.completed]
    limit = adapter.config.course_limit or (len(finished) if all_courses else adapter.config.concurrency)
    gate = asyncio.Semaphore(3)

    async def duration(course: Any) -> tuple[float, Any]:
        async with gate:
            data = await adapter._fetch_json(adapter.seed_page, 'getById', course_id=course.course_id)
        try:
            seconds = float(data.get('duration'))
        except (ValueError, TypeError, AttributeError):
            seconds = math.inf
        return (seconds if math.isfinite(seconds) and seconds > 0 else math.inf), course

    ranked = ([(math.inf, course) for course in finished] if all_courses else
              sorted(await asyncio.gather(*(duration(course) for course in finished)), key=lambda item: item[0]))
    selected = [course for _, course in ranked[:limit]]
    reporter.log('replay_selected', courses=[{'course_id':c.course_id, 'name':c.name,
                                             'duration_seconds':seconds if math.isfinite(seconds) else None}
                                            for seconds, c in ranked[:limit]],
                 concurrency=adapter.config.concurrency, all_courses=all_courses)
    owned_pages: set = set()
    peak_playing = 0
    peak_pages = 0
    monitor_errors = 0
    previous: dict = {}

    async def monitor() -> None:
        nonlocal peak_playing, peak_pages, monitor_errors
        while True:
            current = list(owned_pages)
            peak_pages = max(peak_pages, len(current))
            states = await asyncio.gather(*(adapter.video_state(page) for page in current), return_exceptions=True)
            playing = 0
            next_previous = {}
            for page, state in zip(current, states):
                if isinstance(state, BaseException):
                    # A new page may not contain media yet, or be closing.
                    monitor_errors += 1
                    continue
                position = state['currentTime']
                if (page in previous and position > previous[page]
                        and not state['paused'] and not state['ended']):
                    playing += 1
                next_previous[page] = position
            previous.clear()
            previous.update(next_previous)
            peak_playing = max(peak_playing, playing)
            reporter.log('concurrency_sample', open_course_pages=len(current), playing=playing,
                         peak_playing=peak_playing)
            await asyncio.sleep(max(0.25, min(1, adapter.config.poll_interval)))

    async def handler(course: Any) -> dict:
        return await run_live_course(context, adapter, course, reporter, replay=not all_courses,
                                     play_all=all_courses, owned_pages=owned_pages)

    original_pages = list(context.pages)
    sampler = asyncio.create_task(monitor())
    try:
        result = await run_bounded(selected, concurrency=adapter.config.concurrency, handler=handler)
    finally:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
    return {'status':('playback_completed' if all_courses else 'replay_completed') if result.failed_count == 0 else 'partial_failure',
            'mode':'live_all_once' if all_courses else 'live_replay', 'total':len(result.items),
            ('played' if all_courses else 'replayed'):result.completed_count,
            'newly_completed':0, 'completion_transition_checked':False,
            'failed':result.failed_count, 'peak_active':result.peak_active,
            'peak_playing':peak_playing, 'peak_course_pages':peak_pages,
            'sample_unavailable_count':monitor_errors,
            'original_pages_preserved':all(not page.is_closed() for page in original_pages),
            'owned_pages_remaining':len(owned_pages),
            'failures':[{'course_id':item.item_id, 'error':item.error} for item in result.items if not item.success],
            'course_results':[{'course_id':item.item_id, 'success':item.success, 'value':item.value} for item in result.items]}
