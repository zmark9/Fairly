"""Show or change who gets each home's cleaning email (config.json -> cleaning_notify).

    py cleaning.py                                   list every home and its setting
    py cleaning.py on destin                         switch a home on
    py cleaning.py off destin                        switch a home off
    py cleaning.py set papago "Jane Doe" jane@example.com   change the recipient (stays on/off as it was)

Homes can be named by slug (papago_park, big_sky, destin) or any unique part of it
("papago", "sky"). Add --push to commit and push the change, so the next cloud run uses it;
without --push, only the local config.json changes.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

CONFIG = Path(__file__).with_name("config.json")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def find_home(homes, name):
    name = name.lower()
    exact = [h for h in homes if h["slug"] == name]
    matches = exact or [h for h in homes if name in h["slug"] or name in h["name"].lower()]
    if len(matches) != 1:
        options = ", ".join(h["slug"] for h in homes)
        sys.exit(f"'{name}' matches {len(matches)} homes. Use one of: {options}")
    return matches[0]


def describe(home):
    cn = home.get("cleaning_notify") or {}
    state = "ON " if cn.get("active", True) and cn.get("to_email") else "off"
    who = f"{cn.get('to_name') or '(no name)'} <{cn['to_email']}>" if cn.get("to_email") else "(no recipient)"
    return f"  {state}  {home['slug']:<12} {who}"


def apply(config, action, home_name=None, to_name=None, to_email=None):
    """Change config in place; returns a one-line description of the change."""
    home = find_home(config["homes"], home_name)
    cn = home.setdefault("cleaning_notify", {"active": False, "to_email": "", "to_name": ""})
    if action == "on":
        if not cn.get("to_email"):
            sys.exit(f"{home['slug']} has no recipient yet. First: py cleaning.py set {home['slug']} \"Name\" email")
        cn["active"] = True
    elif action == "off":
        cn["active"] = False
    elif action == "set":
        if not EMAIL.match(to_email or ""):
            sys.exit(f"'{to_email}' doesn't look like an email address.")
        cn["to_name"], cn["to_email"] = to_name, to_email
        cn.setdefault("active", False)
    return f"Cleaning email {home['slug']}: " + describe(home).strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", choices=["list", "on", "off", "set"], default="list")
    parser.add_argument("home", nargs="?")
    parser.add_argument("name", nargs="?", help="recipient name (set only)")
    parser.add_argument("email", nargs="?", help="recipient email (set only)")
    parser.add_argument("--push", action="store_true", help="commit and push config.json")
    args = parser.parse_args(argv)

    config = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    if args.action != "list":
        if not args.home or (args.action == "set" and not args.email):
            parser.error("usage: on|off <home>, or set <home> \"Name\" email")
        message = apply(config, args.action, args.home, args.name, args.email)
        CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(message)
        if args.push:
            repo = CONFIG.parent
            subprocess.run(["git", "-C", str(repo), "pull", "-q", "--rebase", "--autostash", "origin", "main"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message, "--", "config.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "main"], check=True)
            print("Pushed; the next run uses it.")
        else:
            print("Changed config.json locally only. Re-run with --push to make it live.")
    print("\nCleaning emails:")
    for home in config["homes"]:
        print(describe(home))


if __name__ == "__main__":
    main()
