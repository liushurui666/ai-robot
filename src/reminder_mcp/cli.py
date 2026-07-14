from __future__ import annotations

import argparse
import json
import os

from .storage import ReminderStore


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Manage Feishu reminder delivery targets"
    )
    root.add_argument(
        "--db", default=os.getenv("REMINDER_DB_PATH", "~/.nanobot/reminder/reminder.db")
    )
    commands = root.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add-target")
    add.add_argument("alias")
    add.add_argument("--kind", choices=["feishu_webhook", "feishu_chat"], required=True)
    add.add_argument("--recipient", help="Feishu chat_id for feishu_chat")
    add.add_argument(
        "--endpoint-env", help="Environment variable containing webhook URL"
    )
    add.add_argument(
        "--secret-env", help="Environment variable containing webhook signing secret"
    )

    commands.add_parser("list-targets")
    return root


def main() -> None:
    args = parser().parse_args()
    database = ReminderStore(args.db)
    if args.command == "add-target":
        result = database.add_target(
            alias=args.alias,
            kind=args.kind,
            recipient=args.recipient,
            endpoint_env=args.endpoint_env,
            secret_env=args.secret_env,
        )
    else:
        result = database.list_targets()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
