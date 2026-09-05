"""Site contract helpers and the loopback-only Playwright adapter."""

from __future__ import annotations

import re
import time
from typing import Any, Iterable
from urllib.parse import urlencode, urljoin, urlsplit

from .models import CourseRef, Quiz, flatten_course_tree


LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


def is_loopback_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except Exception:
        return False
    return parts.scheme == "http" and (parts.hostname or "").lower() in LOOPBACK_HOSTS


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip()


def match_quiz_from_dialog(dialog_text: str, quizzes: Iterable[Quiz]) -> Quiz | None:
    normalized_dialog = normalize_text(dialog_text)
    matches = [
        quiz
        for quiz in quizzes
        if quiz.stem and normalize_text(quiz.stem) in normalized_dialog
    ]
    return matches[0] if len(matches) == 1 else None


def answer_letters_for_dialog(
    dialog_text: str, quizzes: Iterable[Quiz]
) -> tuple[str, ...]:
    quiz = match_quiz_from_dialog(dialog_text, quizzes)
    if quiz is not None and quiz.correct_letters:
        return quiz.correct_letters
    return ("A",)


class LocalSiteAdapter:
    """Drive the captured page contract, but reject every non-loopback base URL."""

    api_prefix = "/api/jeecg-boot/jcedutec/courseSource/"

    def __init__(self, base_url: str, *, graph_id: str) -> None:
        base_url = base_url.rstrip("/")
        if not is_loopback_url(base_url):
            raise ValueError("only http://127.0.0.1 or http://localhost is allowed")
        self.base_url = base_url
        self.graph_id = str(graph_id)

    def _url(self, path: str, **params: str) -> str:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        return f"{url}?{urlencode(params)}" if params else url

    async def _fetch_json(
        self, page: Any, endpoint: str, *, course_id: str | None = None
    ) -> Any:
        params = {"graphId": self.graph_id}
        if course_id is not None:
            params["id"] = str(course_id)
        url = self._url(self.api_prefix + endpoint, **params)
        result = await page.evaluate(
            """async (url) => {
                const response = await fetch(url, {cache: 'no-store'});
                const contentType = response.headers.get('content-type') || '';
                const body = contentType.includes('application/json')
                    ? await response.json()
                    : {success: false, code: response.status, message: await response.text()};
                return {status: response.status, body};
            }""",
            url,
        )
        body = result.get("body") if isinstance(result, dict) else None
        if (
            not isinstance(body, dict)
            or result.get("status") != 200
            or body.get("success") is not True
            or body.get("code") != 200
        ):
            raise RuntimeError(f"local API {endpoint} failed: {result!r}")
        return body.get("result")

    async def discover_courses(self, context: Any) -> list[CourseRef]:
        page = await context.new_page()
        try:
            await page.goto(
                self._url("/students/myCourse"),
                wait_until="domcontentloaded",
            )
            groups = await self._fetch_json(page, "myCourseTypeTree")
            if not isinstance(groups, list):
                raise RuntimeError("course tree result is not a list")
            return flatten_course_tree(groups, graph_id=self.graph_id)
        finally:
            await page.close()

    async def open_course(self, page: Any, course: CourseRef) -> list[Quiz]:
        await page.goto(
            self._url(
                "/students/courseDetail",
                graphId=course.graph_id,
                id=course.course_id,
            ),
            wait_until="domcontentloaded",
        )
        await page.wait_for_function(
            "window.__localVideoReady === true",
            timeout=10_000,
        )
        raw_questions = await self._fetch_json(
            page,
            "queryCourseQuestionRelaByMainId",
            course_id=course.course_id,
        )
        if not isinstance(raw_questions, list):
            raise RuntimeError("question result is not a list")
        return [Quiz.from_api(item) for item in raw_questions]

    async def play(self, page: Any) -> None:
        await page.locator("video").evaluate("video => video.play()")

    async def video_state(self, page: Any) -> dict[str, Any]:
        return await page.locator("video").evaluate(
            """video => ({
                currentTime: Number(video.currentTime || 0),
                duration: Number(video.duration || 0),
                paused: Boolean(video.paused),
                ended: Boolean(video.ended),
                readyState: Number(video.readyState || 0)
            })"""
        )

    async def visible_dialog(self, page: Any) -> Any | None:
        dialog = page.locator('[role="dialog"]:visible').first
        return dialog if await dialog.count() else None

    async def answer_visible_dialog(
        self, page: Any, quizzes: Iterable[Quiz]
    ) -> dict[str, Any]:
        dialog = await self.visible_dialog(page)
        if dialog is None:
            raise RuntimeError("quiz dialog disappeared before it could be answered")
        dialog_text = await dialog.inner_text()
        matched = match_quiz_from_dialog(dialog_text, quizzes)
        letters = answer_letters_for_dialog(dialog_text, quizzes)

        for letter in letters:
            option = dialog.locator(f'input[value="{letter}"]').first
            if not await option.count():
                raise RuntimeError(f"quiz option {letter} is not present")
            await option.check()

        # A locator resolves again on every use. Keep the submitted DOM node so a
        # following quiz cannot accidentally become the target of the close wait.
        submitted_dialog = await dialog.element_handle()
        if submitted_dialog is None:
            raise RuntimeError("quiz dialog disappeared before submission")
        try:
            async with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/submitAnswer"),
                timeout=5_000,
            ) as response_info:
                await dialog.locator(".ant-btn-primary").click()
            response = await response_info.value
            try:
                payload = await response.json()
            except Exception as exc:
                raise RuntimeError(
                    f"submitAnswer returned non-JSON HTTP {response.status}"
                ) from exc
            if (
                response.status != 200
                or payload.get("success") is not True
                or payload.get("code") != 200
            ):
                raise RuntimeError(f"submitAnswer failed: {payload!r}")
            await page.wait_for_function(
                "node => !node.isConnected || node.getClientRects().length === 0",
                arg=submitted_dialog,
                timeout=5_000,
            )
        finally:
            await submitted_dialog.dispose()
        return {
            "matched": matched is not None,
            "relation_id": matched.relation_id if matched else None,
            "letters": list(letters),
        }

    async def wait_for_finish(self, page: Any, *, timeout: float) -> dict[str, Any]:
        await page.wait_for_function(
            "window.__finishDone === true",
            timeout=max(1, int(timeout * 1000)),
        )
        result = await page.evaluate("window.__finishResponse")
        if (
            not isinstance(result, dict)
            or result.get("success") is not True
            or result.get("code") != 200
        ):
            raise RuntimeError(f"finish failed: {result!r}")
        return result

    async def verify_completed(
        self,
        page: Any,
        *,
        course_id: str,
        timeout: float,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            groups = await self._fetch_json(page, "myCourseTypeTree")
            refs = flatten_course_tree(groups or [], graph_id=self.graph_id)
            target = next((item for item in refs if item.course_id == course_id), None)
            if target is not None and target.completed:
                return True
            if time.monotonic() >= deadline:
                return False
            await page.wait_for_timeout(50)
