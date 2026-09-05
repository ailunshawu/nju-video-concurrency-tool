"""Real-media site adapter; all writes are made by the site's own UI."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit
from playwright.async_api import Error as PlaywrightError

from .answers import AnswerBank, choose_answer
from .live_config import LiveConfig, origin
from .models import CourseRef, Quiz, flatten_course_tree
from .site_adapter import LocalSiteAdapter


PAGE_ACQUISITION_TIMEOUT = 45


# Credentials stay inside the browser; only the requested API result is returned.
AUTHENTICATED_GET = r"""async ({url, storageKey, header, timeoutMs}) => {
    if (new URL(url).origin !== location.origin) throw new Error('API origin mismatch');
    const unwrap = (raw) => {
        let value = raw;
        for (let i = 0; i < 4; i++) {
            if (typeof value === 'string') {
                try { const next = JSON.parse(value); if (next === value) break; value = next; }
                catch (_) { return value; }
            } else if (value && typeof value === 'object') {
                value = value.value ?? value.token ?? value.access_token ?? null;
            } else return null;
        }
        return typeof value === 'string' ? value : null;
    };
    let token = null;
    for (const storage of [localStorage, sessionStorage]) {
        const keys = storageKey ? [storageKey] : Object.keys(storage).filter(
            key => /(?:access.?token|^token$)/i.test(key) && !/refresh/i.test(key)
        );
        for (const key of keys) {
            token = unwrap(storage.getItem(key));
            if (token) break;
        }
        if (token) break;
    }
    const headers = {'Accept': 'application/json'};
    if (token) headers[header] = header.toLowerCase() === 'authorization'
        && !/^Bearer /i.test(token) ? 'Bearer ' + token : token;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(url, {
            method:'GET', headers, credentials:'same-origin', cache:'no-store',
            signal:controller.signal, redirect:'error'
        });
        const isJson = (response.headers.get('content-type') || '').includes('json');
        return {status:response.status, body:isJson ? await response.json() : null};
    } finally { clearTimeout(timer); }
}"""


class LiveSiteAdapter(LocalSiteAdapter):
    def __init__(self, config: LiveConfig, seed_page: Any, *, graph_id: str, bank: AnswerBank):
        config.validate()
        self.config = config
        self.base_url = origin(config.site_url)
        self.graph_id = graph_id
        self.seed_page = seed_page
        self.bank = bank
        self._page_creation_lock = asyncio.Lock()
        self._startup_lock = asyncio.Lock()
        self._last_course_open = float('-inf')

    @asynccontextmanager
    async def startup_guard(self):
        # Creation/play and quiz clicks share focus. Only these UI actions are
        # serialized; native video playback remains concurrent in all windows.
        async with self._startup_lock:
            yield

    async def new_course_page(self, context: Any, course: CourseRef, *, reporter: Any = None) -> Any:
        session = await context.new_cdp_session(self.seed_page)
        candidates: asyncio.Queue = asyncio.Queue()
        context.on('page', candidates.put_nowait)
        created = None
        opened_page = None
        matching = None
        navigation_session = None
        try:
            # Rate-limit only target creation, never document/media loading.
            async with self._page_creation_lock:
                delay = self.config.tab_open_interval - (time.monotonic() - self._last_course_open)
                if delay > 0:
                    await asyncio.sleep(delay)
                async with self.startup_guard():
                    if not self.config.separate_course_windows:
                        await self.seed_page.bring_to_front()
                    target = await session.send('Target.getTargetInfo')
                    if not self.config.separate_course_windows:
                        window = await session.send('Browser.getWindowForTarget',
                                                    {'targetId':target['targetInfo']['targetId']})
                        if window['bounds'].get('windowState') == 'minimized':
                            await session.send('Browser.setWindowBounds',
                                               {'windowId':window['windowId'], 'bounds':{'windowState':'normal'}})
                    options = {'url':self._url('/students/courseDetail', graphId=course.graph_id, id=course.course_id),
                               'newWindow':self.config.separate_course_windows}
                    if self.config.separate_course_windows:
                        options.update(width=900, height=650)
                    context_id = target['targetInfo'].get('browserContextId')
                    if context_id:
                        options['browserContextId'] = context_id
                    created = await session.send('Target.createTarget', options)
                    self._last_course_open = time.monotonic()

            async def match_target():
                while True:
                    candidate = await candidates.get()
                    if candidate.is_closed():
                        continue
                    try:
                        probe = await context.new_cdp_session(candidate)
                        try:
                            info = await probe.send('Target.getTargetInfo')
                        finally:
                            await probe.detach()
                    except PlaywrightError:
                        # Another course may finish between is_closed() and
                        # probing its target. Verify ours still exists, then
                        # ignore the disappearing candidate, not our own tab.
                        if not candidate.is_closed():
                            raise
                        await session.send('Target.getTargetInfo', {'targetId': created['targetId']})
                        continue
                    if info['targetInfo']['targetId'] == created['targetId']:
                        return candidate

            # Match exact target identity, not a URL that a user's tab could
            # share. Chrome may briefly report its internal initial document.
            matching = asyncio.create_task(match_target())
            retries = 0
            while True:
                try:
                    opened_page = await asyncio.wait_for(asyncio.shield(matching), timeout=PAGE_ACQUISITION_TIMEOUT)
                    return opened_page
                except asyncio.TimeoutError:
                    retries += 1
                    delay = min(30, retries * 5)
                    if reporter is not None:
                        reporter.log('course_open_retry_scheduled', course_id=course.course_id,
                                     attempt=retries, delay=delay,
                                     message='首次页面加载超时，将在原标签页重试')
                    # A late response during backoff is usable; do not reload
                    # it unnecessarily or block creation of other courses.
                    try:
                        opened_page = await asyncio.wait_for(asyncio.shield(matching), timeout=delay)
                        return opened_page
                    except asyncio.TimeoutError:
                        await session.send('Target.getTargetInfo', {'targetId': created['targetId']})
                        if navigation_session is None:
                            navigation_session = (await session.send('Target.attachToTarget',
                                {'targetId': created['targetId'], 'flatten': False}))['sessionId']
                        # Playwright doesn't expose the Page until the first
                        # document commits. Address this exact target through
                        # a temporary nested CDP session while it is pending.
                        # Before a commit, navigate retries the requested URL;
                        # reloading the initial document could open about:blank.
                        await session.send('Target.sendMessageToTarget', {
                            'sessionId': navigation_session,
                            'message': json.dumps({'id': retries, 'method': 'Page.navigate',
                                                   'params': {'url': options['url']}})})
        except BaseException:
            if created:
                await asyncio.shield(session.send('Target.closeTarget', {'targetId':created['targetId']}))
            raise
        finally:
            context.remove_listener('page', candidates.put_nowait)
            try:
                try:
                    if matching is not None:
                        matching.cancel()
                        await asyncio.gather(matching, return_exceptions=True)
                finally:
                    try:
                        if navigation_session is not None:
                            try:
                                await session.send('Target.detachFromTarget', {'sessionId': navigation_session})
                            except PlaywrightError:
                                # Closing the target already detaches children.
                                # Do not mask cancellation with that error.
                                pass
                    finally:
                        await session.detach()
            except BaseException:
                if opened_page is not None and not opened_page.is_closed():
                    await asyncio.shield(opened_page.close())
                raise

    async def _fetch_json(self, page: Any, endpoint: str, *, course_id: str | None = None) -> Any:
        if endpoint not in {"myCourseTypeTree", "getById", "queryCourseQuestionRelaByMainId"}:
            raise ValueError("此方法仅支持课程读取接口")
        params = {"graphId": self.graph_id}
        if course_id is not None:
            params["id"] = course_id
        result = await page.evaluate(AUTHENTICATED_GET, {
            "url": self._url(self.api_prefix + endpoint, **params),
            "storageKey": self.config.auth_storage_key,
            "header": self.config.auth_header,
            "timeoutMs": 15_000,
        })
        body = result.get("body")
        if result.get("status") in {401, 403} or (isinstance(body, dict) and body.get("code") in {401, 403}):
            raise RuntimeError("登录已失效或没有课程权限，请在 Chromium 中重新登录")
        if not isinstance(body, dict) or result.get("status") != 200 or body.get("success") is not True or body.get("code") != 200:
            code = body.get("code") if isinstance(body, dict) else "non-json"
            raise RuntimeError(f"课程读取接口 {endpoint} 返回异常：HTTP {result.get('status')} / code {code}")
        return body.get("result")

    async def discover_courses(self, context: Any = None) -> list[CourseRef]:
        groups = await self._fetch_json(self.seed_page, "myCourseTypeTree")
        if not isinstance(groups, list):
            raise RuntimeError("课程树结构发生变化：result 不是数组")
        return flatten_course_tree(groups, graph_id=self.graph_id)

    async def questions(self, page: Any, course_id: str) -> list[Quiz]:
        raw = await self._fetch_json(page, "queryCourseQuestionRelaByMainId", course_id=course_id)
        if not isinstance(raw, list):
            raise RuntimeError("弹题数据结构发生变化：result 不是数组")
        return [Quiz.from_api(item) for item in raw]

    async def video_locator(self, page: Any) -> Any | None:
        for frame in page.frames:
            videos = frame.locator("video")
            if await videos.count():
                return videos.first
        return None

    async def open_course(self, page: Any, course: CourseRef, *, refresh: bool = False,
                          already_opening: bool = False) -> list[Quiz]:
        target_url = self._url("/students/courseDetail", graphId=course.graph_id, id=course.course_id)
        if already_opening:
            await page.wait_for_url(target_url, wait_until="domcontentloaded", timeout=45_000)
        elif refresh and page.url == target_url:
            await page.reload(wait_until="domcontentloaded", timeout=45_000)
        else:
            # A failed first navigation may leave about:blank or an error page;
            # restore the intended course in this same owned tab in that case.
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
        if origin(page.url) != self.base_url:
            raise RuntimeError("课程页面跳转到其他站点，可能需要重新登录")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if await self.video_locator(page) is not None:
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("课程页面没有 video 播放器，请运行兼容性检查")
        return await self.questions(page, course.course_id)

    async def play(self, page: Any) -> None:
        video = await self.video_locator(page)
        if video is None:
            raise RuntimeError("播放器不存在")
        # The browser's media clock and natural ended event remain untouched.
        await video.evaluate("""async v => {
            // Chrome pauses hidden per-element-muted media even when background
            // suspension is disabled. The dedicated browser uses --mute-audio.
            v.muted = false;
            if (v.volume === 0) v.volume = 1;
            v.preload = 'auto';
            // The element can exist before an asynchronous media source is
            // assigned. play() in that gap may reject with NotSupportedError.
            if (v.readyState === 0 && !v.error) {
                await new Promise((resolve, reject) => {
                    const cleanup = () => {
                        clearTimeout(timer);
                        v.removeEventListener('loadedmetadata', loaded);
                        v.removeEventListener('error', failed);
                    };
                    const loaded = () => { cleanup(); resolve(); };
                    const failed = () => { cleanup(); reject(new Error('Media source failed to load')); };
                    const timer = setTimeout(() => { cleanup(); reject(new Error('Media source not ready after 30 seconds')); }, 30000);
                    v.addEventListener('loadedmetadata', loaded, {once:true});
                    v.addEventListener('error', failed, {once:true});
                    if (v.readyState > 0) loaded();
                    else if (v.error) failed();
                });
            }
            if (v.error) throw new Error('Media error code ' + v.error.code);
            if (!v.ended) await v.play();
        }""")

    async def video_state(self, page: Any) -> dict[str, Any]:
        video = await self.video_locator(page)
        if video is None:
            raise RuntimeError("播放器已从页面移除")
        return await video.evaluate("""v => ({currentTime:v.currentTime, duration:Number.isFinite(v.duration)?v.duration:0,
            paused:v.paused, ended:v.ended, readyState:v.readyState,
            error:v.error ? {code:v.error.code, message:v.error.message} : null})""")

    async def visible_dialog(self, page: Any) -> Any | None:
        for frame in page.frames:
            dialog = frame.locator('[role="dialog"]:visible').first
            if await dialog.count():
                return dialog
        return None

    async def answer_visible_dialog(self, page: Any, quizzes: list[Quiz]) -> dict[str, Any]:
        # Hidden tabs may not produce the animation frames needed by Playwright
        # actionability checks. Keep this tab foregrounded through submission.
        async with self._startup_lock:
            await page.bring_to_front()
            return await self._answer_visible_dialog_foreground(page, quizzes)

    async def _answer_visible_dialog_foreground(self, page: Any, quizzes: list[Quiz]) -> dict[str, Any]:
        dialog = await self.visible_dialog(page)
        if dialog is None:
            raise RuntimeError("弹题窗口已消失")
        content = await dialog.inner_text()
        inputs = dialog.locator('input[type="radio"], input[type="checkbox"]')
        options = await inputs.evaluate_all(r"""nodes => nodes.map((node, index) => {
            const label = node.closest('label, .ant-radio-wrapper, .ant-checkbox-wrapper') || node.parentElement;
            const text = label.innerText || label.textContent || '';
            const prefix = text.match(/^\s*([A-F])\s*[:：.．、]/i);
            const letter = prefix ? prefix[1].toUpperCase() : /^[A-F]$/i.test(node.value)
                ? node.value.toUpperCase() : String.fromCharCode(65 + index);
            return {letter, text, checked:node.checked, type:node.type};
        })""")
        displayed = {option["letter"]: option["text"] for option in options}
        if not options or len(displayed) != len(options):
            raise RuntimeError("未找到可唯一对应 A/B/C 选项的答题控件")
        choice = choose_answer(content, displayed, quizzes, self.bank)
        for index, option in enumerate(options):
            control = inputs.nth(index)
            selected = option["letter"] in choice.letters
            if selected:
                await control.check(timeout=5_000)
            elif option["type"] == "checkbox" and option["checked"]:
                await control.uncheck(timeout=5_000)
        if not set(choice.letters).issubset(displayed):
            raise RuntimeError("需要选择的答案选项不在当前弹窗内")
        buttons = dialog.locator("button").filter(has_text=re.compile(r"提\s*交"))
        button = buttons.first if await buttons.count() else dialog.locator(".ant-btn-primary").first
        node = await dialog.element_handle()
        if node is None:
            raise RuntimeError("弹窗在提交前消失")
        try:
            async with page.expect_response(
                lambda response: response.request.method == "POST"
                    and urlsplit(response.url).path == self.api_prefix + "submitAnswer"
                    and origin(response.url) == self.base_url,
                timeout=15_000,
            ) as response_info:
                await button.click(timeout=5_000)
            response = await response_info.value
            try:
                body = await asyncio.wait_for(response.json(), timeout=15)
            except Exception as exc:
                raise RuntimeError(f"答题接口返回非 JSON：HTTP {response.status}") from exc
            if response.status != 200 or not isinstance(body, dict) or body.get("success") is not True or body.get("code") != 200:
                raise RuntimeError("本次答案未被网站接受，详情请查看页面")
            frame = await node.owner_frame()
            if frame is not None:
                await frame.wait_for_function("node => !node.isConnected || node.getClientRects().length === 0", arg=node, timeout=10_000)
        finally:
            if node is not None:
                await node.dispose()
        return {"letters": list(choice.letters), "source": choice.source}

    async def verify_completed(self, page: Any, *, course_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            courses = await self.discover_courses()
            target = next((course for course in courses if course.course_id == course_id), None)
            if target is not None and target.completed:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(2, max(0.01, deadline - time.monotonic())))
