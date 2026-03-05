"""Tests for CloudflareTunnel subprocess manager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.tunnel import CloudflareTunnel, _TUNNEL_URL_RE


# --- Regex tests ---


class TestTunnelUrlRegex:
    def test_matches_standard_url(self):
        line = (
            "2024-01-01T00:00:00Z INF |  "
            "https://some-random-words.trycloudflare.com  |"
        )
        match = _TUNNEL_URL_RE.search(line)
        assert match is not None
        assert match.group(1) == "https://some-random-words.trycloudflare.com"

    def test_matches_bare_url(self):
        line = "https://abc-def-ghi.trycloudflare.com"
        match = _TUNNEL_URL_RE.search(line)
        assert match is not None
        assert match.group(1) == "https://abc-def-ghi.trycloudflare.com"

    def test_no_match_on_unrelated_line(self):
        line = "2024-01-01T00:00:00Z INF Starting tunnel"
        assert _TUNNEL_URL_RE.search(line) is None

    def test_no_match_on_different_domain(self):
        line = "https://example.com/some-path"
        assert _TUNNEL_URL_RE.search(line) is None

    def test_matches_with_numbers_in_subdomain(self):
        line = "https://abc-123-def-456.trycloudflare.com"
        match = _TUNNEL_URL_RE.search(line)
        assert match is not None
        assert match.group(1) == "https://abc-123-def-456.trycloudflare.com"


# --- Not-installed / OS error tests ---


class TestCloudflaredNotInstalled:
    @pytest.mark.asyncio
    async def test_returns_none_when_not_installed(self):
        tunnel = CloudflareTunnel(port=8000)
        with patch("server.tunnel.shutil.which", return_value=None):
            result = await tunnel.start()
        assert result is None
        assert tunnel.url is None

    @pytest.mark.asyncio
    async def test_returns_none_on_os_error(self):
        tunnel = CloudflareTunnel(port=8000)
        with patch("server.tunnel.shutil.which", return_value="/usr/bin/cloudflared"):
            with patch(
                "server.tunnel.asyncio.create_subprocess_exec",
                side_effect=OSError("exec failed"),
            ):
                result = await tunnel.start()
        assert result is None


# --- Successful start tests ---


def _mock_process_with_lines(lines: list[bytes]):
    """Create a mock process whose stderr yields the given lines."""
    mock = MagicMock()
    mock.returncode = None
    mock.stderr = MagicMock()
    line_iter = iter(lines)

    async def readline():
        try:
            return next(line_iter)
        except StopIteration:
            return b""

    mock.stderr.readline = readline
    mock.terminate = MagicMock()
    mock.kill = MagicMock()

    async def wait():
        mock.returncode = 0

    mock.wait = wait
    return mock


class TestCloudflaredStartSuccess:
    @pytest.mark.asyncio
    async def test_parses_url_from_stderr(self):
        tunnel = CloudflareTunnel(port=8000)
        mock_process = _mock_process_with_lines([
            b"2024-01-01T00:00:00Z INF Starting tunnel\n",
            b"2024-01-01T00:00:00Z INF +-------------------------------------------+\n",
            b"2024-01-01T00:00:00Z INF |  https://test-tunnel-abc.trycloudflare.com  |\n",
            b"2024-01-01T00:00:00Z INF +-------------------------------------------+\n",
        ])

        with patch("server.tunnel.shutil.which", return_value="/usr/bin/cloudflared"):
            with patch(
                "server.tunnel.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ):
                result = await tunnel.start(timeout=5.0)

        assert result == "https://test-tunnel-abc.trycloudflare.com"
        assert tunnel.url == "https://test-tunnel-abc.trycloudflare.com"

    @pytest.mark.asyncio
    async def test_timeout_when_no_url_found(self):
        tunnel = CloudflareTunnel(port=8000)

        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.stderr = MagicMock()
        mock_process.terminate = MagicMock()
        mock_process.kill = MagicMock()

        async def wait():
            mock_process.returncode = 0

        mock_process.wait = wait

        # Simulate stderr that blocks forever (no URL)
        async def readline():
            await asyncio.sleep(100)
            return b""

        mock_process.stderr.readline = readline

        with patch("server.tunnel.shutil.which", return_value="/usr/bin/cloudflared"):
            with patch(
                "server.tunnel.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ):
                result = await tunnel.start(timeout=0.2)

        assert result is None
        assert tunnel.url is None


# --- Stop tests ---


class TestCloudflaredStop:
    @pytest.mark.asyncio
    async def test_terminates_process(self):
        tunnel = CloudflareTunnel(port=8000)

        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.terminate = MagicMock()
        mock_process.kill = MagicMock()

        async def wait():
            mock_process.returncode = 0

        mock_process.wait = wait
        tunnel._process = mock_process

        await tunnel.stop()

        mock_process.terminate.assert_called_once()
        assert tunnel._process is None
        assert tunnel.url is None

    @pytest.mark.asyncio
    async def test_kills_if_terminate_hangs(self):
        tunnel = CloudflareTunnel(port=8000)

        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.terminate = MagicMock()
        mock_process.kill = MagicMock()

        call_count = 0

        async def wait():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.sleep(100)  # Hang on first call (terminate)
            else:
                mock_process.returncode = -9

        mock_process.wait = wait
        tunnel._process = mock_process

        await tunnel.stop()

        mock_process.terminate.assert_called_once()
        mock_process.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_noop_when_not_started(self):
        tunnel = CloudflareTunnel(port=8000)
        await tunnel.stop()  # Should not raise


# --- CLI / Config tests ---


class TestCliTunnelFlag:
    def test_serve_with_tunnel_flag(self):
        from server.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["serve", "--tunnel"])
        assert args.tunnel is True

    def test_serve_without_tunnel_flag(self):
        from server.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["serve"])
        assert args.tunnel is None


class TestConfigEnableTunnel:
    def test_default_is_false(self):
        from server.config import Settings

        s = Settings(auth_token="test")
        assert s.enable_tunnel is False
