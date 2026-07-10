from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description="Governance operator CLI")
    parser.add_argument("--base-url", default=os.getenv("AGENT_BASE_URL", "http://localhost:8080"))
    parser.add_argument("--token", default=os.getenv("AGENT_API_BEARER_TOKEN", ""))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-proposals")
    sub.add_parser("list-review-ready")
    replays = sub.add_parser("list-replays")
    replays.add_argument("--proposal-id", default=None)
    shadows = sub.add_parser("list-shadows")
    shadows.add_argument("--proposal-id", default=None)
    packets = sub.add_parser("list-review-packets")
    packets.add_argument("--proposal-id", default=None)
    sub.add_parser("dashboard-data")
    sub.add_parser("dashboard-url")
    show = sub.add_parser("show-proposal")
    show.add_argument("proposal_id")
    export = sub.add_parser("export-review")
    export.add_argument("proposal_id")
    replay = sub.add_parser("run-replay")
    replay.add_argument("proposal_id")
    shadow = sub.add_parser("run-shadow")
    shadow.add_argument("proposal_id")
    packet = sub.add_parser("create-review-packet")
    packet.add_argument("proposal_id")
    approve = sub.add_parser("approve-proposal")
    approve.add_argument("proposal_id")
    approve.add_argument("--reviewer", default="cli")
    approve.add_argument("--change-control", required=True)
    approve.add_argument("--rationale", default="CLI approval after review")
    reject = sub.add_parser("reject-proposal")
    reject.add_argument("proposal_id")
    reject.add_argument("--reviewer", default="cli")
    reject.add_argument("--rationale", default="CLI rejection")
    sub.add_parser("show-active-config")
    freeze = sub.add_parser("freeze-live")
    freeze.set_defaults(path="/governance/freeze-live")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, headers=_headers(args.token), timeout=30.0) as client:
        result = _dispatch(client, args)
    print(json.dumps(result, indent=2, sort_keys=True))


def _dispatch(client: httpx.Client, args: argparse.Namespace) -> Any:
    if args.command == "list-proposals":
        return _response_json(client.get("/governance/proposals"))
    if args.command == "list-review-ready":
        return _response_json(client.get("/governance/proposals/review-ready"))
    if args.command == "list-replays":
        params = {"proposal_id": args.proposal_id} if args.proposal_id else None
        return _response_json(client.get("/governance/replay-results", params=params))
    if args.command == "list-shadows":
        params = {"proposal_id": args.proposal_id} if args.proposal_id else None
        return _response_json(client.get("/governance/shadow-comparisons", params=params))
    if args.command == "list-review-packets":
        params = {"proposal_id": args.proposal_id} if args.proposal_id else None
        return _response_json(client.get("/governance/review-packets", params=params))
    if args.command == "dashboard-data":
        return _response_json(client.get("/governance/dashboard/data"))
    if args.command == "dashboard-url":
        return {"url": str(client.base_url).rstrip("/") + "/governance/dashboard"}
    if args.command == "show-proposal":
        return _response_json(client.get(f"/governance/proposals/{args.proposal_id}"))
    if args.command == "export-review":
        return _response_json(client.get(f"/governance/proposals/{args.proposal_id}/review-export"))
    if args.command == "run-replay":
        return _response_json(client.post(f"/governance/proposals/{args.proposal_id}/request-replay"))
    if args.command == "run-shadow":
        return _response_json(client.post(f"/governance/proposals/{args.proposal_id}/request-shadow"))
    if args.command == "create-review-packet":
        return _response_json(client.post(f"/governance/proposals/{args.proposal_id}/review-packet"))
    if args.command == "approve-proposal":
        payload = {"reviewer": args.reviewer, "approver_actor": args.reviewer, "change_control_id": args.change_control, "rationale": args.rationale}
        return _response_json(client.post(f"/governance/proposals/{args.proposal_id}/approve", json=payload))
    if args.command == "reject-proposal":
        payload = {"reviewer": args.reviewer, "approver_actor": args.reviewer, "change_control_id": "reject-no-change", "rationale": args.rationale}
        return _response_json(client.post(f"/governance/proposals/{args.proposal_id}/reject", json=payload))
    if args.command == "show-active-config":
        return _response_json(client.get("/governance/config/active"))
    if args.command == "freeze-live":
        return _response_json(client.post("/governance/freeze-live"))
    raise SystemExit(f"unknown command: {args.command}")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _response_json(response: httpx.Response) -> Any:
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    main()
