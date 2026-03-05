"""Cloudflare Tunnel subprocess manager for Octopus."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil

logger = logging.getLogger(__name__)

# Regex to capture the trycloudflare.com URL from cloudflared stderr.
# cloudflared outputs lines like:
#   2024-01-01T00:00:00Z INF |  https://xxx-yyy-zzz.trycloudflare.com  |
_TUNNEL_URL_RE = re.compile(r"(https://[a-zA-Z0-9-]+\.trycloudflare\.com)")


class CloudflareTunnel:
    """Manages a cloudflared tunnel subprocess.

    Usage:
        tunnel = CloudflareTunnel(port=8000)
        url = await tunnel.start()  # Returns the public URL or None
        ...
        await tunnel.stop()
    """

    def __init__(self, port: int) -> None:
        self._port = port
        self._process: asyncio.subprocess.Process | None = None
        self._url: str | None = None
        self._monitor_task: asyncio.Task | None = None

    @property
    def url(self) -> str | None:
        """The public tunnel URL, or None if not yet resolved."""
        return self._url

    async def start(self, timeout: float = 30.0) -> str | None:
        """Start cloudflared and wait for the tunnel URL.

        Returns the public URL on success, or None if:
        - cloudflared is not installed
        - the URL could not be parsed within the timeout
        - the process exited unexpectedly

        Never raises -- all errors are logged and the server continues.
        """
        if not shutil.which("cloudflared"):
            logger.error(
                "cloudflared is not installed. "
                "Install it from https://developers.cloudflare.com/cloudflare-one/"
                "connections/connect-networks/downloads/ "
                "or disable the tunnel with OCTOPUS_ENABLE_TUNNEL=false"
            )
            return None

        try:
            self._process = await asyncio.create_subprocess_exec(
                "cloudflared",
                "tunnel",
                "--url",
                f"http://localhost:{self._port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            logger.error("Failed to start cloudflared: %s", e)
            return None

        # Wait for the URL to appear in stderr, with a timeout
        url = await self._wait_for_url(timeout)

        if url:
            self._url = url
            # Start background task to drain stderr and detect crashes
            self._monitor_task = asyncio.create_task(self._monitor_stderr())
        else:
            logger.error(
                "Timed out waiting for cloudflared tunnel URL (%.0fs). "
                "Check cloudflared output for errors.",
                timeout,
            )
            await self.stop()

        return self._url

    async def _wait_for_url(self, timeout: float) -> str | None:
        """Read stderr lines until we find the tunnel URL or timeout."""
        try:
            return await asyncio.wait_for(self._read_until_url(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None

    async def _read_until_url(self) -> str:
        """Read stderr line by line until the URL is found."""
        assert self._process is not None
        assert self._process.stderr is not None

        while True:
            line = await self._process.stderr.readline()
            if not line:
                # Process exited before we got the URL
                raise asyncio.CancelledError("cloudflared exited prematurely")
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded:
                logger.debug("cloudflared: %s", decoded)
            match = _TUNNEL_URL_RE.search(decoded)
            if match:
                return match.group(1)

    async def _monitor_stderr(self) -> None:
        """Continue reading stderr after URL is found, for logging."""
        assert self._process is not None
        assert self._process.stderr is not None

        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if decoded:
                    logger.debug("cloudflared: %s", decoded)
        except asyncio.CancelledError:
            pass

        if self._process.returncode is not None and self._process.returncode != 0:
            logger.warning(
                "cloudflared exited with code %d", self._process.returncode
            )

    async def stop(self) -> None:
        """Terminate the cloudflared subprocess gracefully."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._process and self._process.returncode is None:
            logger.info("Stopping cloudflared tunnel...")
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("cloudflared did not exit in time, killing...")
                self._process.kill()
                await self._process.wait()

        self._process = None
        self._url = None
