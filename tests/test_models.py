from server.models import (
    CreateSessionRequest,
    MessageContent,
    MessageRole,
    SessionInfo,
    SessionStatus,
)


def test_create_session_request_defaults():
    req = CreateSessionRequest()
    assert req.name == "New Session"
    assert req.working_dir is None


def test_create_session_request_custom():
    req = CreateSessionRequest(name="My Session", working_dir="/tmp")
    assert req.name == "My Session"
    assert req.working_dir == "/tmp"


def test_session_info():
    info = SessionInfo(
        id="abc123",
        name="Test",
        working_dir="/tmp",
        status=SessionStatus.idle,
        created_at="2026-01-01T00:00:00Z",
        message_count=5,
    )
    assert info.id == "abc123"
    assert info.status == SessionStatus.idle
    assert info.message_count == 5


def test_message_content():
    msg = MessageContent(
        role=MessageRole.assistant,
        type="text",
        content="Hello",
    )
    assert msg.role == MessageRole.assistant
    assert msg.content == "Hello"
    assert msg.tool_name is None


def test_message_content_tool_use():
    msg = MessageContent(
        role=MessageRole.assistant,
        type="tool_use",
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_use_id="tool_123",
    )
    assert msg.tool_name == "Bash"
    assert msg.tool_input == {"command": "ls"}
