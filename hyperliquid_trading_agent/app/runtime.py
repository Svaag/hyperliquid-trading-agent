from __future__ import annotations

import asyncio
import signal
import sys

from hyperliquid_trading_agent.app.config import ServiceRole, load_settings
from hyperliquid_trading_agent.app.logging import configure_logging, get_logger
from hyperliquid_trading_agent.app.workers.agent_worker import AgentWorker
from hyperliquid_trading_agent.app.workers.base import BaseWorker
from hyperliquid_trading_agent.app.workers.discord_bot_worker import DiscordBotWorker
from hyperliquid_trading_agent.app.workers.discord_publisher_worker import DiscordPublisherWorker
from hyperliquid_trading_agent.app.workers.liquidations_worker import LiquidationsWorker
from hyperliquid_trading_agent.app.workers.newswire_worker import NewswireWorker
from hyperliquid_trading_agent.app.workers.scheduler_worker import SchedulerWorker
from hyperliquid_trading_agent.app.workers.trader_worker import TraderWorker
from hyperliquid_trading_agent.app.workers.world_model_worker import WorldModelWorker

log = get_logger(__name__)

WORKERS: dict[ServiceRole, type[BaseWorker]] = {
    ServiceRole.NEWSWIRE: NewswireWorker,
    ServiceRole.WORLD_MODEL: WorldModelWorker,
    ServiceRole.TRADER: TraderWorker,
    ServiceRole.DISCORD_PUBLISHER: DiscordPublisherWorker,
    ServiceRole.DISCORD_BOT: DiscordBotWorker,
    ServiceRole.AGENT: AgentWorker,
    ServiceRole.LIQUIDATIONS: LiquidationsWorker,
    ServiceRole.SCHEDULER: SchedulerWorker,
}


async def run_role(role: ServiceRole) -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    if settings.service_role != role:
        raise SystemExit(f"SERVICE_ROLE={settings.service_role!s} does not match command role {role.value!r}")
    worker_cls = WORKERS.get(role)
    if worker_cls is None:
        raise SystemExit(f"No worker exists for role {role.value!r}")
    worker = worker_cls(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:  # pragma: no cover - Windows/event-loop compatibility
            pass
    log.info("worker_starting", role=role.value, instance_id=worker.instance_id)
    await worker.run_forever()
    log.info("worker_stopped", role=role.value, instance_id=worker.instance_id)


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        valid = "|".join(role.value for role in WORKERS)
        raise SystemExit(f"Usage: hyperliquid-trading-agent-runtime {{{valid}}}")
    try:
        role = ServiceRole(args[0])
    except ValueError as exc:
        raise SystemExit(f"Unknown SERVICE_ROLE {args[0]!r}") from exc
    asyncio.run(run_role(role))


if __name__ == "__main__":
    main()
