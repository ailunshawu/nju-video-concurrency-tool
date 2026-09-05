"""Map captured answer text or the existing JSON bank to visible options."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .models import Quiz


def normalized(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", str(text)))
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def option_text(text: str) -> str:
    return normalized(re.sub(r"^\s*[A-Fa-f]\s*[:：.．、)）]\s*", "", text))


@dataclass
class AnswerBank:
    entries: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None) -> "AnswerBank":
        if path is None:
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("题库必须是 {题干: [答案文本]} 格式")
        entries = {}
        for stem, answers in raw.items():
            if isinstance(answers, str):
                answers = [answers]
            if isinstance(answers, list) and all(isinstance(item, str) for item in answers):
                entries[normalized(stem)] = tuple(normalized(item) for item in answers)
        return cls(entries)


@dataclass(frozen=True)
class AnswerChoice:
    letters: tuple[str, ...]
    source: str


def _map_texts(texts: tuple[str, ...], displayed: dict[str, str]) -> tuple[str, ...]:
    selected = []
    for text in texts:
        matches = [letter for letter, shown in displayed.items() if option_text(shown) == normalized(text)]
        if len(matches) != 1:
            return ()
        selected.append(matches[0])
    return tuple(sorted(set(selected)))


def choose_answer(dialog_text: str, displayed: dict[str, str], quizzes: list[Quiz], bank: AnswerBank) -> AnswerChoice:
    content = normalized(dialog_text)
    matching = [quiz for quiz in quizzes if normalized(quiz.stem) and normalized(quiz.stem) in content]
    if len(matching) == 1:
        quiz = matching[0]
        texts = tuple(quiz.options.get(letter, "") for letter in quiz.correct_letters)
        if texts and all(texts):
            letters = _map_texts(texts, displayed)
            if letters:
                return AnswerChoice(letters, "page_answer")
        bank_texts = bank.entries.get(normalized(quiz.stem), ())
    else:
        bank_matches = [texts for stem, texts in bank.entries.items() if stem and stem in content]
        bank_texts = bank_matches[0] if len(bank_matches) == 1 else ()
    letters = _map_texts(bank_texts, displayed) if bank_texts else ()
    return AnswerChoice(letters, "question_bank") if letters else AnswerChoice(("A",), "fallback_a")
