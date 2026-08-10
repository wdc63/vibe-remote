from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.handlers.command_handlers import CommandHandlers
from modules.im import MessageContext


class _Formatter:
    @staticmethod
    def format_code_inline(value: str) -> str:
        return value


class _IMClient:
    def __init__(self) -> None:
        self.formatter = _Formatter()
        self.messages: list[tuple[MessageContext, str]] = []

    async def send_message(self, context: MessageContext, text: str, parse_mode=None) -> str:
        self.messages.append((context, text))
        return "message-1"


class _SettingsManager:
    def __init__(self, custom_cwd: str) -> None:
        self.custom_cwd: str | None = custom_cwd
        self.set_calls: list[tuple[str, str | None]] = []

    def set_custom_cwd(self, settings_key: str, cwd: str | None) -> None:
        self.set_calls.append((settings_key, cwd))
        self.custom_cwd = cwd


class _ClaudeClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _SessionHandler:
    def __init__(self, old_cwd: str, client: _ClaudeClient) -> None:
        self.claude_sessions = {f"telegram_chat-1:{old_cwd}": client}
        self.receiver_tasks = {}
        self.cleared: list[str] = []

    @staticmethod
    def get_base_session_id(context: MessageContext) -> str:
        return "telegram_chat-1"

    def clear_session_tracking(self, composite_key: str) -> None:
        self.cleared.append(composite_key)


class _Controller:
    def __init__(self, settings: _SettingsManager, im_client: _IMClient, session_handler: _SessionHandler) -> None:
        self.config = SimpleNamespace(platform="telegram", language="en")
        self.settings_manager = settings
        self.sessions = settings
        self.im_client = im_client
        self.session_manager = object()
        self.session_handler = session_handler
        self.codex = SimpleNamespace(clear_sessions=AsyncMock())
        self.opencode = SimpleNamespace(clear_sessions=AsyncMock())
        self.agent_service = SimpleNamespace(agents={"codex": self.codex, "opencode": self.opencode})

    @staticmethod
    def _get_settings_key(context: MessageContext) -> str:
        return "telegram::42"

    @staticmethod
    def _get_session_key(context: MessageContext) -> str:
        return "telegram::42"

    def get_cwd(self, context: MessageContext) -> str:
        return self.settings_manager.custom_cwd or "C:\\Users\\21480\\Desktop"


class CwdResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_cwd_clears_override_and_discards_old_directory_sessions(self) -> None:
        old_cwd = "D:\\StrideAvalonia"
        settings = _SettingsManager(old_cwd)
        im_client = _IMClient()
        claude_client = _ClaudeClient()
        session_handler = _SessionHandler(old_cwd, claude_client)
        controller = _Controller(settings, im_client, session_handler)
        handler = CommandHandlers(controller)
        context = MessageContext(user_id="42", channel_id="42", platform="telegram", platform_specific={"is_dm": True})

        await handler.handle_reset_cwd(context)

        assert settings.set_calls == [("telegram::42", None)]
        assert settings.custom_cwd is None
        assert claude_client.closed is True
        assert session_handler.cleared == [f"telegram_chat-1:{old_cwd}"]
        controller.codex.clear_sessions.assert_awaited_once_with("telegram::42")
        controller.opencode.clear_sessions.assert_awaited_once_with("telegram::42")
        assert "C:\\Users\\21480\\Desktop" in im_client.messages[0][1]
