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
    show = sub.add_parser("show-proposal")
    show.add_argument("proposal_id")
    replay = sub.add_parser("run-replay")
    replay.add_argument("proposal_id")
    shadow = sub.add_parser("run-shadow")
    shadow.add_argument("proposal_id")
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
        return client.get("/governance/proposals").raise_for_status_or_json()
    if args.command == "show-proposal":
        return client.get(f"/governance/proposals/{args.proposal_id}").raise_for_status_or_json()
    if args.command == "run-replay":
        return client.post(f"/governance/proposals/{args.proposal_id}/request-replay").raise_for_status_or_json()
    if args.command == "run-shadow":
        return client.post(f"/governance/proposals/{args.proposal_id}/request-shadow").raise_for_status_or_json()
    if args.command == "approve-proposal":
        payload = {"reviewer": args.reviewer, "approver_actor": args.reviewer, "change_control_id": args.change_control, "rationale": args.rationale}
        return client.post(f"/governance/proposals/{args.proposal_id}/approve", json=payload).raise_for_status_or_json()
    if args.command == "reject-proposal":
        payload = {"reviewer": args.reviewer, "approver_actor": args.reviewer, "change_control_id": "reject-no-change", "rationale": args.rationale}
        return client.post(f"/governance/proposals/{args.proposal_id}/reject", json=payload).raise_for_status_or_json()
    if args.command == "show-active-config":
        return client.get("/governance/config/active").raise_for_status_or_json()
    if args.command == "freeze-live":
        return client.post("/governance/freeze-live").raise_for_status_or_json()
    raise SystemExit(f"unknown command: {args.command}")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _raise_for_status_or_json(response: httpx.Response) -> Any:
    response.raise_for_status()
    return response.json()


httpx.Response.raise_for_status_or_json = _raise_for_status_or_json  # type: ignore[attr-defined,method-assign]


if __name__ == "__main__":
    main()
