from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from app.db import init_db
from app.router_credentials import (
    KNOWN_SCOPES,
    RouterCredentialError,
    issue_router_credential,
    list_router_credentials,
    revoke_router_credential,
    rotate_router_credential,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage AutoVPN router credentials")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue", help="issue a credential and print its token once")
    issue.add_argument("--client-id", type=int, required=True)
    issue.add_argument(
        "--scope",
        action="append",
        dest="scopes",
        choices=sorted(KNOWN_SCOPES),
        required=True,
    )
    issue.add_argument("--expires-at")
    issue.add_argument("--label", default="")
    commands.add_parser("list", help="list credential metadata without token digests")
    revoke = commands.add_parser("revoke", help="revoke a credential")
    revoke.add_argument("credential_id")
    rotate = commands.add_parser("rotate", help="replace and print a token once")
    rotate.add_argument("credential_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    init_db()
    try:
        if args.command == "issue":
            issued = issue_router_credential(
                args.client_id,
                args.scopes,
                expires_at=args.expires_at,
                label=args.label,
            )
            print(
                json.dumps(
                    {
                        "credential_id": issued.credential_id,
                        "client_id": issued.client_id,
                        "scopes": issued.scopes,
                        "label": issued.label,
                        "created_at": issued.created_at,
                    },
                    sort_keys=True,
                )
            )
            print(f"token={issued.token}")
        elif args.command == "list":
            print(json.dumps(list_router_credentials(), sort_keys=True))
        elif args.command == "revoke":
            revoke_router_credential(args.credential_id)
            print(json.dumps({"credential_id": args.credential_id, "revoked": True}))
        elif args.command == "rotate":
            token = rotate_router_credential(args.credential_id)
            print(json.dumps({"credential_id": args.credential_id, "rotated": True}))
            print(f"token={token}")
    except RouterCredentialError as exc:
        _parser().error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
