"""Create the first admin user. Password comes from the environment or is generated."""

from __future__ import annotations

import os
import secrets
import sys

from api import auth, db, repo
from api.logging_ import setup_logging
from api.policies import SYSTEM


def main() -> int:
    setup_logging()
    db.migrate()
    username = os.getenv("ADMIN_USER", "owner")
    if repo.count(SYSTEM, "admin_users", where="username = ?", params=[username]):
        print(f"user {username!r} already exists — nothing to do")
        return 0
    password = os.getenv("ADMIN_PASSWORD") or secrets.token_urlsafe(18)
    auth.create_user(username, password, role="owner")
    print(f"created owner {username!r}")
    if not os.getenv("ADMIN_PASSWORD"):
        print(f"generated password (store it in your password manager now): {password}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
