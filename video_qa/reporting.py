"""Small append-only run reports for the local browser test harness."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EventReporter:
    def __init__(self, output_root: Path, *, live_output: bool = False) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.run_directory = Path(output_root) / stamp
        self.run_directory.mkdir(parents=True, exist_ok=False)
        self.events_path = self.run_directory / "events.jsonl"
        self._lock = threading.Lock()
        self.live_output = live_output
        self._completed = 0

    def log(self, event: str, **fields: Any) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        if self.live_output:
            message = ""
            if event == "run_started":
                run_label = "网站课程任务" if fields.get("mode") == "live" else "本地测试"
                message = (
                    f"{run_label}已启动：{fields['base_url']}\n"
                    f"课程 {fields['course_count']} 门，并发上限 {fields['concurrency']}。"
                )
            elif event == "course_completed":
                self._completed += 1
                message = f"已完成 {self._completed} 门：{fields['course_id']}"
            elif event == 'course_replayed':
                self._completed += 1
                message = f"回放通过 {self._completed} 门：{fields['course_id']}"
            elif event == 'course_played':
                self._completed += 1
                message = f"本轮播放结束 {self._completed} 门：{fields['course_id']}"
            elif event == 'replay_selected':
                label = '全量课程各播放一次' if fields.get('all_courses') else '已完成课程回放'
                message = f"已选择 {len(fields['courses'])} 门{label}，并发上限 {fields['concurrency']}。"
            elif event == "course_failed":
                message = f"课程失败 {fields['course_id']}：{fields['error']}"
            elif event == "course_refresh_scheduled":
                message = (f"课程 {fields['course_id']} 暂时失败：{fields['error']}\n"
                           f"保留标签页，{fields['delay_seconds']} 秒后刷新重试（第 {fields['attempt']} 次）；Ctrl+C 可停止。")
            elif event == "courses_rescanned":
                message = f"再次检查课程列表：剩余 {fields['remaining']} 门未完成。"
            elif event == "quiz_answered" and "source" in fields:
                source = {"page_answer":"页面答案", "question_bank":"题库", "fallback_a":"默认 A"}.get(fields["source"], fields["source"])
                message = f"答题成功 {fields['course_id']}：{','.join(fields['letters'])}（{source}）"
            if message:
                print(message, flush=True)

    def write_summary(self, summary: dict[str, Any]) -> Path:
        path = self.run_directory / "summary.json"
        path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path
