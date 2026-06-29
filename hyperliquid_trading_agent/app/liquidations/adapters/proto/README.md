# Hyperliquid L1 gateway gRPC stubs (vendored)

`hyperliquid_l1_gateway.proto` is a **provider-neutral** transcription of the
Hyperliquid node's own L1 gateway streaming service, which managed providers
(Dwellir, Quicknode, …) re-expose. The generated `*_pb2.py` / `*_pb2_grpc.py`
are committed so the optional gRPC adapter works without a build step.

The adapter (`adapters/hyperliquid_grpc.py`) depends only on
`HyperliquidL1Gateway.StreamFills(Position) → stream BlockFills` and treats the
`BlockFills.data` field as opaque JSON — `decode_block_fills()` json-decodes it
into `{ block_number, events: [[address, fill], …] }` and runs each `fill`
(a node `node_fills` object) through the golden-tested `parse_grpc_fill`. So the
proto surface stays tiny and tolerant of provider schema drift.

## Regenerate

Generated with `grpcio-tools==1.81.1` / `protobuf==6.33.6`. From the repo root:

```sh
uv pip install grpcio-tools          # dev-only; not a runtime dep
python -m grpc_tools.protoc \
  --proto_path=. \
  --python_out=. \
  --grpc_python_out=. \
  hyperliquid_trading_agent/app/liquidations/adapters/proto/hyperliquid_l1_gateway.proto
```

The `--proto_path=.` (repo root) is what makes the generated `_pb2_grpc.py`
import its `_pb2` peer with a full, package-qualified path.

## Using a different provider

If your provider publishes a richer/different `.proto` (e.g. structured
`BlockFills` fields rather than a single JSON `data` field), drop their `.proto`
here, regenerate, and adapt only `_open_stream` in `hyperliquid_grpc.py` to hand
`decode_block_fills` the same JSON envelope. Nothing else changes.

## Runtime / config

`pip install '.[grpc]'`, then set `liquidations_hl_grpc_enabled=true`,
`hl_grpc_endpoint=<host:port>`, `hl_grpc_api_key=<key>` (sent in the
`hl_grpc_auth_header` metadata, default `x-api-key`), and run
`scripts/grpc_liq_smoke.py` to verify before enabling on a live deploy.
