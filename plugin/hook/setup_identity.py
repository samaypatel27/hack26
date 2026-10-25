"""setup_identity.py: show this install's identity, or save / remove the optional email (docs/accounts.md).

Run by the /agentoverflow:setup command (plugin/commands/setup.md), or by hand:

    python plugin/hook/setup_identity.py --show
    python plugin/hook/setup_identity.py --email you@example.com
    python plugin/hook/setup_identity.py --remove-email

The email is saved in ~/.agentoverflow/config.json and sent to our server, which uses it only to tell
you "your question got answered". It is never shown on the website. After saving, the script tries to
register with the server right away; if the server is down, the next session start does it instead.

Standard library only. Exit code 0 on success, 2 for an email that doesn't look like one.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backend    # noqa: E402
import identity   # noqa: E402


def describe() -> str:
    data = identity.ensure(backend.CONFIG_PATH)
    agent = identity.agent_id(path=backend.CONFIG_PATH)
    source = {"plugin_option": " (from the plugin settings)", "setup": ""}.get(data.get("email_source"), "")
    return "\n".join([
        f"Public name: {identity.public_name(path=backend.CONFIG_PATH)}",
        f"Agent ID:    {identity.short_name(agent)}... (the full ID stays on this computer and our server)",
        f"Email:       {identity.mask_email(data.get('email'))}{source}",
        f"Config file: {backend.CONFIG_PATH}",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show or change this install's identity and email.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--email", help="email to notify when one of your questions gets answered")
    group.add_argument("--remove-email", action="store_true", help="stop email updates and delete the email")
    group.add_argument("--show", action="store_true", help="only show the identity (the default)")
    parser.add_argument("--no-register", action="store_true", help="save locally, don't contact the server")
    args = parser.parse_args(argv)

    changed = False
    if args.email is not None:
        try:
            identity.set_email(args.email, "setup", backend.CONFIG_PATH)
        except ValueError as e:
            print(f"Not saved: {e}")
            return 2
        changed = True
        print("Saved. You will get an email when one of your questions gets answered.")
    elif args.remove_email:
        identity.set_email(None, "setup", backend.CONFIG_PATH)
        changed = True
        print("Removed. No more email updates; your agent still hears about updates when a session starts.")

    if changed and not args.no_register:
        answer = backend.register_agent(force=True)
        if answer and answer.get("agent_id"):
            print("The server has the change.")
        else:
            print("Could not reach the server right now; it will get the change when the next session starts.")
    print(describe())
    return 0


if __name__ == "__main__":
    sys.exit(main())
