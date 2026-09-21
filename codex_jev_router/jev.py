"""TypeSafe System One client with bounded retries."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .policy import ASTRA, LUNA, SOL


QUESTIONS = {
    "tier": {
        "type": "choice",
        "instructions": (
            "Which model tier should serve this model call? Route the current user task or newly "
            "spawned subagent task. Use gpt-5.6-luna for mechanical, clearly scoped work; "
            "a coordinator that only needs to delegate subtasks and aggregate their results also "
            "belongs on gpt-5.6-luna even when the delegated work spans multiple modules; "
            "gpt-5.6-sol for standard implementation work; and gpt-6-astra for hard, ambiguous, "
            "architectural, or risky work. Tool continuations are pinned by the caller and are not "
            "sent for a new decision."
        ),
        "criteria": {
            LUNA: "Fast and cheap; mechanical or clearly scoped tasks.",
            SOL: "Workhorse; standard implementation work.",
            ASTRA: "Frontier; hard, ambiguous, or risky problems.",
        },
    },
    "depth": {
        "type": "choice",
        "instructions": (
            "What thinking depth does the task require? low = straightforward; medium = careful "
            "thought; high = substantial reasoning; xhigh = very deep reasoning; max = maximum "
            "depth for the hardest problems."
        ),
        "criteria": {
            "low": "No deep reasoning needed.",
            "medium": "Some careful thought.",
            "high": "Substantial reasoning required.",
            "xhigh": "Very deep reasoning.",
            "max": "Maximum reasoning depth, hardest problems.",
        },
    },
}


def questions_for(*, is_subagent: bool) -> dict[str, Any]:
    questions = deepcopy(QUESTIONS)
    if not is_subagent:
        return questions
    prefix = (
        "This is a delegated subtask. Judge only the delegated subtask. "
        "The parent tier is context, not a default. Do not inherit the parent model or depth. "
    )
    for question in questions.values():
        question["instructions"] = prefix + question["instructions"]
    return questions


def load_key(
    environ: Mapping[str, str] | None = None,
    key_file: str | Path | None = None,
    *,
    default_env_path: str | Path = Path("~/.jev.env").expanduser(),
) -> str:
    environment = os.environ if environ is None else environ
    value = str(environment.get("TYPESAFE_API_KEY", "")).strip()
    if value:
        return value
    paths = []
    if key_file is not None:
        paths.append(Path(key_file).expanduser())
    default_path = Path(default_env_path).expanduser()
    if default_path not in paths:
        paths.append(default_path)
    for path in paths:
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped.startswith("TYPESAFE_API_KEY="):
                        return stripped.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


class JevClient:
    def __init__(
        self,
        *,
        key: str,
        url: str,
        model: str,
        timeout_seconds: float = 4.0,
        retries: int = 2,
        backoff_seconds: float = 0.25,
        retry_after_cap_seconds: float = 4.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.key = key
        self.url = url
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.retry_after_cap_seconds = retry_after_cap_seconds
        self.opener = opener
        self.sleeper = sleeper

    def _retry_delay(self, error: urllib.error.HTTPError, attempt: int) -> float:
        delay = self.backoff_seconds * (2**attempt)
        raw = error.headers.get("Retry-After") if error.headers is not None else None
        if raw:
            try:
                delay = float(raw)
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(raw)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(max(0.0, delay), max(0.0, self.retry_after_cap_seconds))

    def ask(
        self,
        state: dict[str, Any],
        *,
        questions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(
            {"model": self.model, "state": state, "questions": questions or QUESTIONS},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Authorization": "Bearer" + " " + self.key, "Content-Type": "application/json"},
            method="POST",
        )
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                with self.opener(request, timeout=self.timeout_seconds) as response:
                    decoded = json.loads(response.read().decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise ValueError("Jev response must be an object")
                return decoded
            except urllib.error.HTTPError as exc:
                last_error = exc
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise
                if attempt >= self.retries:
                    raise
                if exc.code == 429:
                    self.sleeper(self._retry_delay(exc, attempt))
                else:
                    self.sleeper(self.backoff_seconds * (2**attempt))
            except Exception as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise
                self.sleeper(self.backoff_seconds * (2**attempt))
        raise RuntimeError("unreachable") from last_error
