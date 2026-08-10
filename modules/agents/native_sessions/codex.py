from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from .base import NativeSessionProvider, build_tail_preview, dt_from_ts, read_json_lines
from .types import NativeResumeSession

logger = logging.getLogger(__name__)


def _cwd_variants(working_path: str) -> tuple[str, ...]:
    """Return equivalent normal and Windows extended-length path forms."""
    raw_path = str(working_path)
    variants = [raw_path]

    if raw_path.startswith("\\\\?\\UNC\\"):
        variants.append("\\\\" + raw_path[8:])
    elif raw_path.startswith("\\\\?\\"):
        variants.append(raw_path[4:])
    elif raw_path.startswith("\\\\"):
        variants.append("\\\\?\\UNC\\" + raw_path[2:])
    elif len(raw_path) >= 3 and raw_path[1] == ":" and raw_path[2] in ("\\", "/"):
        variants.append("\\\\?\\" + raw_path)

    return tuple(dict.fromkeys(variants))


class CodexNativeSessionProvider(NativeSessionProvider):
    agent_name = "codex"

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or Path.home() / ".codex" / "state_5.sqlite")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def list_metadata(self, working_path: str) -> list[NativeResumeSession]:
        if not self.db_path.exists():
            return []
        items: list[NativeResumeSession] = []
        cwd_variants = _cwd_variants(working_path)
        placeholders = ", ".join("?" for _ in cwd_variants)
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    f"""
                    SELECT id, created_at, updated_at, title, first_user_message, rollout_path
                    FROM threads
                    WHERE cwd IN ({placeholders}) AND archived = 0
                    ORDER BY updated_at DESC, id DESC
                    """,
                    cwd_variants,
                )
                for session_id, created_ts, updated_ts, title, first_user_message, rollout_path in cursor.fetchall():
                    tokens_used = (
                        self._get_tokens_used(session_id)
                        if self._has_vibe_bootstrap_marker(title, first_user_message)
                        else None
                    )
                    if self._is_empty_vibe_bootstrap(title, first_user_message, rollout_path, tokens_used):
                        continue
                    created_at = dt_from_ts(created_ts)
                    updated_at = dt_from_ts(updated_ts)
                    items.append(
                        NativeResumeSession(
                            agent="codex",
                            agent_prefix="cx",
                            native_session_id=session_id,
                            working_path=working_path,
                            created_at=created_at,
                            updated_at=updated_at,
                            sort_ts=(updated_at or created_at).timestamp() if (updated_at or created_at) else 0.0,
                            locator={
                                "title": title or "",
                                "first_user_message": first_user_message or "",
                                "rollout_path": rollout_path or "",
                            },
                        )
                    )
        except Exception as exc:
            logger.warning("Failed to list Codex sessions for %s: %s", working_path, exc)
        return items

    @staticmethod
    def _has_vibe_bootstrap_marker(title: str | None, first_user_message: str | None) -> bool:
        value = str(first_user_message or "").strip()
        return bool(
            value
            and value == str(title or "").strip()
            and value.startswith("If you generate an image with Codex, include it in the final reply")
        )

    @staticmethod
    def _rollout_has_assistant_output(rollout_path: str | None) -> bool | None:
        """Return None when the rollout cannot be inspected safely."""
        value = str(rollout_path or "").strip()
        if not value:
            return None
        path = Path(value)
        if not path.is_file():
            return None

        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict) or row.get("type") != "response_item":
                        continue
                    payload = row.get("payload") or {}
                    if payload.get("type") != "message" or payload.get("role") != "assistant":
                        continue
                    parts = payload.get("content") or []
                    if not isinstance(parts, list):
                        continue
                    for part in parts:
                        if not isinstance(part, dict) or part.get("type") != "output_text":
                            continue
                        if str(part.get("text") or "").strip():
                            return True
        except Exception as exc:
            logger.warning("Failed to inspect Codex rollout %s: %s", path, exc)
            return None
        return False

    def _is_empty_vibe_bootstrap(
        self,
        title: str | None,
        first_user_message: str | None,
        rollout_path: str | None,
        tokens_used: int | None = None,
    ) -> bool:
        """Identify a Vibe-created thread that never produced an assistant reply."""
        if not self._has_vibe_bootstrap_marker(title, first_user_message):
            return False
        has_assistant_output = self._rollout_has_assistant_output(rollout_path)
        if has_assistant_output is not None:
            return not has_assistant_output

        value = str(rollout_path or "").strip()
        if not value or tokens_used != 0:
            return False
        try:
            return not Path(value).exists()
        except OSError:
            return False

    def _get_tokens_used(self, native_session_id: str) -> int | None:
        """Read token usage when supported by the current Codex state schema."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT tokens_used FROM threads WHERE id = ? LIMIT 1",
                    (native_session_id,),
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        except Exception as exc:
            logger.warning("Failed to inspect Codex token usage for %s: %s", native_session_id, exc)
            return None
        if not row:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def is_empty_bootstrap_session(self, native_session_id: str) -> bool:
        """Check one persisted Codex thread without hiding uncertain results."""
        if not self.db_path.exists() or not native_session_id:
            return False
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT title, first_user_message, rollout_path
                    FROM threads
                    WHERE id = ?
                    LIMIT 1
                    """,
                    (native_session_id,),
                ).fetchone()
        except Exception as exc:
            logger.warning("Failed to inspect Codex session %s: %s", native_session_id, exc)
            return False
        if not row:
            return False
        return self._is_empty_vibe_bootstrap(*row, self._get_tokens_used(native_session_id))

    def hydrate_preview(self, item: NativeResumeSession) -> NativeResumeSession:
        preview = ""
        rollout_path_raw = str(item.locator.get("rollout_path") or "").strip()
        rollout_path = Path(rollout_path_raw) if rollout_path_raw else None
        if rollout_path and rollout_path.is_file():
            rows = read_json_lines(rollout_path)
            for row in reversed(rows):
                if row.get("type") != "response_item":
                    continue
                payload = row.get("payload") or {}
                if payload.get("type") != "message" or payload.get("role") != "assistant":
                    continue
                parts = payload.get("content") or []
                texts: list[str] = []
                if isinstance(parts, list):
                    for part in parts:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            text = str(part.get("text") or "").strip()
                            if text:
                                texts.append(text)
                if texts:
                    preview = "\n".join(texts)
                    break
        if not preview:
            preview = str(item.locator.get("title") or item.locator.get("first_user_message") or "")
        item.last_agent_message = preview
        item.last_agent_tail = build_tail_preview(preview or item.native_session_id)
        return item
