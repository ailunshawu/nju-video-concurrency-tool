"""Typed models for the captured course and quiz contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable


OPTION_KEYS = {
    "A": "optiona",
    "B": "optionb",
    "C": "optionc",
    "D": "optiond",
    "E": "optione",
    "F": "optionf",
}


def _normalize_letters(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        candidates = re.split(r"[,，\s]+", value.strip())
    elif isinstance(value, Iterable):
        candidates = [str(item) for item in value]
    else:
        candidates = [str(value)]
    valid = {candidate.strip().upper() for candidate in candidates if candidate.strip()}
    return tuple(letter for letter in OPTION_KEYS if letter in valid)


@dataclass(frozen=True)
class Quiz:
    relation_id: str
    question_id: str
    course_id: str
    eject_time: float
    stem: str
    kind: str
    correct_letters: tuple[str, ...]
    options: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Quiz":
        options = {
            letter: str(raw.get(key) or "")
            for letter, key in OPTION_KEYS.items()
            if raw.get(key) is not None
        }
        return cls(
            relation_id=str(raw.get("id") or ""),
            question_id=str(raw.get("questionId") or ""),
            course_id=str(raw.get("courseId") or ""),
            eject_time=float(raw.get("ejectTime") or 0),
            stem=str(raw.get("stem") or "").strip(),
            kind=str(raw.get("kind") or ""),
            correct_letters=_normalize_letters(raw.get("correctAnswer")),
            options=options,
        )

    def submission_payload(self, *, graph_id: str) -> dict[str, str]:
        return {
            "questionId": self.relation_id,
            "id": self.course_id,
            "graphId": str(graph_id),
            "option": ",".join(self.correct_letters),
        }

    def to_api(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": self.relation_id,
            "createBy": "本地测试管理员",
            "createTime": "2026-09-04 00:00:00",
            "updateBy": "local",
            "updateTime": "2026-09-04 00:00:00",
            "courseId": self.course_id,
            "ejectTime": str(int(self.eject_time) if self.eject_time.is_integer() else self.eject_time),
            "questionId": self.question_id,
            "stem": self.stem,
            "kind": self.kind,
            "type": "local-fixture",
            "correctAnswer": ",".join(self.correct_letters),
            "analysis": None,
            "isPic": "N",
            "degree": "1",
            "kind_dictText": {"1": "判断题", "2": "单选题", "3": "多选题"}.get(
                self.kind, "未知题型"
            ),
            "degree_dictText": "简单",
        }
        for letter, key in OPTION_KEYS.items():
            record[key] = self.options.get(letter) or None
        return record


@dataclass(frozen=True)
class CourseRef:
    course_id: str
    graph_id: str
    name: str
    completed: bool


@dataclass
class Course:
    course_id: str
    graph_id: str
    name: str
    duration: float
    quizzes: list[Quiz] = field(default_factory=list)
    completed: bool = False
    watch_duration: float = 0.0
    answered: dict[str, str] = field(default_factory=dict)


def flatten_course_tree(
    groups: Iterable[dict[str, Any]], *, graph_id: str
) -> list[CourseRef]:
    refs: list[CourseRef] = []
    for group in groups:
        for child in group.get("children") or []:
            title = str(child.get("title") or "").strip()
            completed = title.startswith("(学完)")
            name = re.sub(r"^\((?:学完|未学)\)\s*", "", title).strip()
            refs.append(
                CourseRef(
                    course_id=str(child.get("key") or ""),
                    graph_id=str(graph_id),
                    name=name,
                    completed=completed,
                )
            )
    return refs
