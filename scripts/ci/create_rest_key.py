#!/usr/bin/env python3
"""Create the Tryton REST application key (res.user.application).

The HesabYar adapter authenticates to Tryton's REST API with a
user-application key (``application='rest'``, state ``validated``),
sent as ``Authorization: Bearer <key>``. The key lives in the Tryton
database, so a fresh DB needs one created.

Usage:
    python3 scripts/ci/create_rest_key.py \
        --base-url http://localhost:8000 \
        --database hesabyar_tryton_test \
        --admin-password <admin password> \
        [--key hesabyar-dev-rest-key] [--admin-user admin]

Idempotent: if a validated ``rest`` application key with the same value
already exists for the admin user, it is left alone.

Uses only the Python standard library (urllib) -- no extra deps.
Exits non-zero on any failure (login, search, create).
"""

from __future__ import annotations

import argparse
import base64
import json
import urllib.error
import urllib.request


def rpc(base_url: str, method: str, params: list, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        base_url,
        data=json.dumps({"id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise SystemExit(f"JSON-RPC {method} failed: HTTP {exc.code}: {body[:500]}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="trytond base URL, e.g. http://localhost:8000")
    parser.add_argument("--database", required=True, help="Tryton database name")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", required=True)
    parser.add_argument("--key", default="hesabyar-dev-rest-key")
    args = parser.parse_args()

    rpc_url = f"{args.base_url.rstrip('/')}/{args.database}/rpc/"

    login = rpc(rpc_url, "common.db.login", [args.admin_user, {"password": args.admin_password}])
    if "error" in login:
        raise SystemExit(f"admin login failed: {login['error']}")
    uid, token, _ = login["result"]
    session = {"Authorization": "Session " + base64.b64encode(f"{args.admin_user}:{uid}:{token}".encode()).decode()}
    print(f"Logged in as {args.admin_user!r} (uid={uid})")

    # search_read params: (domain, offset, limit, order, fields, context)
    # -- a flat list, NOT nested (see docs/dev-setup.md §4).
    existing = rpc(
        rpc_url,
        "model.res.user.application.search_read",
        [
            [["key", "=", args.key], ["user", "=", uid], ["application", "=", "rest"]],
            0,
            None,
            None,
            ["id", "state"],
            {},
        ],
        session,
    )
    if "error" in existing:
        raise SystemExit(f"application-key search failed: {existing['error']}")
    if existing["result"]:
        rec = existing["result"][0]
        print(f"REST key already exists (id={rec['id']}, state={rec['state']}) -- leaving untouched")
        return

    created = rpc(
        rpc_url,
        "model.res.user.application.create",
        [[{"key": args.key, "user": uid, "application": "rest"}], {}],
        session,
    )
    if "error" in created:
        raise SystemExit(f"application-key create failed: {created['error']}")
    # create returns a list of ids, not records.
    print(f"Created REST key (id={created['result'][0]})")


if __name__ == "__main__":
    main()
