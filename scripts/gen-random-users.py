#!/usr/bin/env python3
"""Generate random user accounts server-side (via `vl usr-acct add`) and mirror
their credentials to disk under ~/.veritaslock/users/<username>/, matching the
layout of the users that are already there (username.txt / email.txt /
password.txt / user.id).

`vl usr-acct add` both creates the user on the IdP and caches it locally, so
after this runs the new users show up in `vl usr-acct list` too.

Usage:
    scripts/gen-random-users.py fulcrum anchorpoint            # 196 users each
    scripts/gen-random-users.py fulcrum --count 50
    scripts/gen-random-users.py fulcrum anchorpoint --dry-run

Env:
    VL          path to the vl executable (default: .venv/bin/vl, then `vl`)
    VL_ENV      environment name passed as --env (default: vl's default env)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

USERS_DIR = Path.home() / ".veritaslock" / "users"
EMAIL_DOMAIN = "example.com"
DEFAULT_COUNT = 196

FIRST_NAMES = [
    "alice", "aaron", "amanda", "andrew", "angela", "anthony", "ashley",
    "brian", "brenda", "bruce", "carol", "charles", "chris", "cynthia",
    "daniel", "david", "deborah", "diana", "donald", "dorothy", "edward",
    "elizabeth", "emily", "eric", "frank", "george", "gloria", "gregory",
    "harold", "helen", "henry", "irene", "jack", "james", "janet", "jason",
    "jennifer", "jessica", "john", "joseph", "joyce", "julia", "karen",
    "keith", "kenneth", "kevin", "kimberly", "larry", "laura", "linda",
    "lisa", "louis", "margaret", "maria", "mark", "martha", "mary",
    "matthew", "melissa", "michael", "nancy", "nathan", "nicole", "oscar",
    "patricia", "paul", "peter", "rachel", "randy", "raymond", "rebecca",
    "richard", "robert", "roger", "ronald", "rose", "ruth", "ryan", "samuel",
    "sandra", "sarah", "scott", "sharon", "stephen", "steven", "susan",
    "teresa", "thomas", "timothy", "tyler", "victor", "virginia", "walter",
    "wayne", "wendy", "william",
]

LAST_NAMES = [
    "anderson", "brown", "clark", "collins", "davis", "garcia", "gonzalez",
    "hall", "harris", "hernandez", "jackson", "johnson", "jones", "lee",
    "lewis", "lopez", "martin", "martinez", "miller", "moore", "perez",
    "robinson", "rodriguez", "smith", "taylor", "thomas", "thompson",
    "walker", "white", "williams", "wilson", "adams", "baker", "campbell",
    "carter", "cooper", "evans", "edwards", "flores", "green", "hill",
    "james", "kelly", "king", "morgan", "murphy", "nelson", "parker",
    "phillips", "reed", "rivera", "roberts", "scott", "stewart", "torres",
    "turner", "ward", "watson", "wood", "wright", "young",
]


def resolve_vl() -> str:
    if env := os.environ.get("VL"):
        return env
    if Path(".venv/bin/vl").is_file():
        return ".venv/bin/vl"
    if found := shutil.which("vl"):
        return found
    sys.exit("error: could not find a 'vl' executable (set $VL or activate the venv)")


def existing_usernames() -> set[str]:
    if not USERS_DIR.is_dir():
        return set()
    return {p.name for p in USERS_DIR.iterdir() if p.is_dir()}


def next_username(taken: set[str], rng: random.Random) -> str:
    """Pick a random firstinitial+lastname handle not in `taken`, adding a numeric
    suffix on collision. Records the result in `taken`."""
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    base = f"{first[0]}{last}"
    username = base
    n = 1
    while username in taken:
        n += 1
        username = f"{base}{n}"
    taken.add(username)
    return username


def make_usernames(count: int, taken: set[str], rng: random.Random) -> list[str]:
    return [next_username(taken, rng) for _ in range(count)]


PASSWORD_RE = re.compile(r"shown once\):\s*(\S+)")


def create_user(vl: str, username: str, org: str, env_args: list[str]) -> tuple[str, str]:
    """Return (server_id, password) for a freshly created user."""
    email = f"{username}@{EMAIL_DOMAIN}"
    proc = subprocess.run(
        [vl, "usr-acct", "add", username, "--email", email, "--org", org, *env_args],
        capture_output=True,
        text=True,
        env={**os.environ, "VL_OUTPUT": "json"},
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stdout + proc.stderr).strip().replace("\n", " "))

    lines = proc.stdout.strip().splitlines()
    pw_match = PASSWORD_RE.search(proc.stdout)
    # the JSON object is everything except the trailing "Password ..." line
    json_text = "\n".join(l for l in lines if not l.startswith("Password"))
    dto = json.loads(json_text)
    password = pw_match.group(1) if pw_match else ""
    return dto["server_id"], password


def write_disk(username: str, email: str, server_id: str, password: str) -> None:
    d = USERS_DIR / username
    d.mkdir(parents=True, exist_ok=True)
    (d / "username.txt").write_text(username + "\n")
    (d / "email.txt").write_text(email + "\n")
    (d / "password.txt").write_text(password + "\n")
    (d / "user.id").write_text(server_id + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("orgs", nargs="+", help="org name(s) to create users in")
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"users per org (default: {DEFAULT_COUNT})")
    ap.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible names")
    ap.add_argument("--dry-run", action="store_true", help="print what would be created, make no changes")
    args = ap.parse_args()

    vl = resolve_vl()
    env_args = ["--env", os.environ["VL_ENV"]] if os.environ.get("VL_ENV") else []
    rng = random.Random(args.seed)

    taken = existing_usernames()
    plan = {org: make_usernames(args.count, taken, rng) for org in args.orgs}

    total = sum(len(v) for v in plan.values())
    print(f"Planning {total} users across {len(plan)} org(s): "
          + ", ".join(f"{o}={len(u)}" for o, u in plan.items()))

    if args.dry_run:
        for org, users in plan.items():
            print(f"\n[{org}]")
            for u in users:
                print(f"  {u}  <{u}@{EMAIL_DOMAIN}>")
        return 0

    created = 0
    failed: list[str] = []
    for org, users in plan.items():
        for u in list(users):
            # A handle can collide with a server user that isn't mirrored to disk
            # (e.g. one created outside this script, or soft-deleted). Retry with
            # a fresh name a few times before giving up on that slot.
            for attempt in range(5):
                email = f"{u}@{EMAIL_DOMAIN}"
                try:
                    server_id, password = create_user(vl, u, org, env_args)
                    write_disk(u, email, server_id, password)
                    created += 1
                    print(f"OK    {org}/{u}")
                    break
                except Exception as exc:  # noqa: BLE001 - keep going, report at end
                    if "already exists" in str(exc).lower() and attempt < 4:
                        new = next_username(taken, rng)
                        print(f"BUSY  {org}/{u} taken server-side — retrying as {new}")
                        u = new
                        continue
                    failed.append(f"{org}/{u}: {exc}")
                    print(f"FAIL  {org}/{u} — {exc}")
                    break

    print("\n" + "-" * 40)
    print(f"created: {created}")
    print(f"failed:  {len(failed)}")
    for f in failed:
        print(f"  {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
