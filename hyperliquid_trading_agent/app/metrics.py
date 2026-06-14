from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info

SERVICE_INFO = Info("hyperliquid_trading_agent_build", "Build/runtime information")
UP = Gauge("hyperliquid_trading_agent_up", "Service liveness")
DISCORD_MESSAGES = Counter("hyperliquid_trading_agent_discord_messages_total", "Discord messages handled", ["result"])
TOOL_CALLS = Counter("hyperliquid_trading_agent_tool_calls_total", "Agent tool calls", ["tool", "result"])
HYPERLIQUID_REQUESTS = Counter("hyperliquid_trading_agent_hl_requests_total", "Hyperliquid /info requests", ["type", "result"])
HYPERLIQUID_LATENCY = Histogram("hyperliquid_trading_agent_hl_request_seconds", "Hyperliquid request latency", ["type"])
MODEL_CALLS = Counter("hyperliquid_trading_agent_model_calls_total", "LLM calls", ["provider", "result"])
MODEL_LATENCY = Histogram("hyperliquid_trading_agent_model_call_seconds", "LLM call latency", ["provider"])
DECISION_RUNS = Counter("hyperliquid_trading_agent_decision_runs_total", "High-stakes decision runs", ["status"])
DECISION_LATENCY = Histogram("hyperliquid_trading_agent_decision_run_seconds", "High-stakes decision run latency", ["status"])
