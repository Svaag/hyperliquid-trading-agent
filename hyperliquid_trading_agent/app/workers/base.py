from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from hyperliquid_trading_agent import __version__
from hyperliquid_trading_agent.app.config import ServiceRole, Settings
from hyperliquid_trading_agent.app.db.repository import Repository
from hyperliquid_trading_agent.app.db.session import create_engine, create_sessionmaker
from hyperliquid_trading_agent.app.infra.leader_lock import postgres_advisory_lock
from hyperliquid_trading_agent.app.logging import get_logger
from hyperliquid_trading_agent.app.newswire.normalize import now_ms

log = get_logger(__name__)

CommandHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


class BaseWorker:
    role: ServiceRole
    lock_name: str | None = None

    def __init__(self, settings: Settings):
        self.settings = settings
        self.instance_id = f"{self.role.value}-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        self.started_at_ms = now_ms()
        self.engine: AsyncEngine | None = None
        self.sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self.repository = Repository(None)
        self._stop = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None
        self._last_heartbeat_prune_at_ms = 0

    async def run_forever(self) -> None:
        self.engine = create_engine(self.settings)
        self.sessionmaker = create_sessionmaker(self.engine)
        self.repository = Repository(self.sessionmaker)
        failed = False
        try:
            await self._heartbeat("starting")
            if self.lock_name:
                async with self.sessionmaker() as session:
                    async with postgres_advisory_lock(session, self.lock_name):
                        await self._run_with_heartbeat()
            else:
                await self._run_with_heartbeat()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed = True
            await self.repository.mark_service_failed(
                self.role.value,
                self.instance_id,
                error=f"{type(exc).__name__}: {exc}",
                metadata=self.heartbeat_metadata(),
            )
            raise
        finally:
            if not failed:
                await self.stop()
                await self.repository.mark_service_stopped(
                    self.role.value,
                    self.instance_id,
                    metadata=self.heartbeat_metadata(),
                )
            await self._prune_heartbeat_history(force=True)
            if self.engine is not None:
                await self.engine.dispose()

    async def _run_with_heartbeat(self) -> None:
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name=f"{self.role.value}-heartbeat")
        await self._heartbeat("running")
        try:
            await self.run()
        finally:
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
                self._heartbeat_task = None

    async def run(self) -> None:
        await self.wait_until_stopped()

    async def stop(self) -> None:
        self._stop.set()
        await self.repository.mark_service_stopping(self.role.value, self.instance_id, metadata=self.heartbeat_metadata())

    def request_stop(self) -> None:
        self._stop.set()

    async def wait_until_stopped(self) -> None:
        await self._stop.wait()

    async def _heartbeat_loop(self) -> None:
        interval = max(1, int(self.settings.service_heartbeat_interval_seconds))
        while not self._stop.is_set():
            await self._heartbeat("running")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _heartbeat(self, status: str) -> None:
        await self.repository.upsert_service_heartbeat(
            service_role=self.role.value,
            instance_id=self.instance_id,
            status=status,
            started_at_ms=self.started_at_ms,
            hostname=socket.gethostname(),
            pid=os.getpid(),
            version=__version__,
            metadata=self.heartbeat_metadata(),
        )
        await self._prune_heartbeat_history()

    async def _prune_heartbeat_history(self, *, force: bool = False) -> None:
        now = now_ms()
        if not force and now - self._last_heartbeat_prune_at_ms < 5 * 60_000:
            return
        prune = getattr(self.repository, "prune_service_heartbeat_history", None)
        if callable(prune):
            retention_ms = max(60, int(self.settings.service_heartbeat_history_retention_seconds)) * 1000
            try:
                await prune(before_ms=now - retention_ms)
            except Exception as exc:  # pragma: no cover - cleanup must not stop a worker
                log.warning(
                    "service_heartbeat_history_prune_failed",
                    role=self.role.value,
                    error=type(exc).__name__,
                )
        self._last_heartbeat_prune_at_ms = now

    def heartbeat_metadata(self) -> dict[str, Any]:
        return {}

    async def command_loop(self, handlers: dict[str, CommandHandler]) -> None:
        poll_seconds = max(0.1, float(self.settings.worker_command_poll_seconds))
        stale_ms = max(1, int(self.settings.worker_command_claim_stale_seconds)) * 1000
        while not self._stop.is_set():
            command = await self.repository.claim_next_worker_command(target_role=self.role.value, instance_id=self.instance_id, stale_after_ms=stale_ms)
            if command is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=poll_seconds)
                except TimeoutError:
                    continue
                continue
            handler = handlers.get(str(command.get("command_type") or ""))
            try:
                if handler is None:
                    raise RuntimeError(f"unsupported_command:{command.get('command_type')}")
                result = await handler(command)
                await self.repository.complete_worker_command(str(command["command_id"]), result=result or {})
            except Exception as exc:  # pragma: no cover - worker safety net
                log.warning("worker_command_failed", role=self.role.value, command_id=command.get("command_id"), error=type(exc).__name__)
                await self.repository.fail_worker_command(str(command["command_id"]), error=f"{type(exc).__name__}: {exc}")
