"""TypeSafe System One client with bounded retries."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from .policy import ASTRA, LUNA, SOL


QUESTIONS = {
    "tier": {
        "type": "choice",
        "instructions": (
            "Which model tier should serve this model call? Route the current user task or newly "
            "spawned subagent task. Use gpt-5.6-luna for mechanical, clearly scoped work; "
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


def load_key(
    environ: Mapping[str, str] | None = None,
    env_path: str | Path = Path("~/.jev.env").expanduser(),
) -> str:
    environment = os.environ if environ is None else environ
    value = str(environment.get("TYPESAFE_API_KEY", "")).strip()
    if value:
        return value
    try:
        with open(env_path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("TYPESAFE_API_KEY="):
                    return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
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
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.key = key
        self.url = url
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.opener = opener
        self.sleeper = sleeper

    def ask(self, state: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(
            {"model": self.model, "state": state, "questions": QUESTIONS},
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
            except Exception as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise
                self.sleeper(self.backoff_seconds * (2**attempt))
        raise RuntimeError("unreachable") from last_error
