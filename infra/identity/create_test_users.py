"""Create (or reset) two local test customers in the external tenant and map them to the bank.

Customer N signs in as clienteN@<subdomain>.onmicrosoft.com with email + password. Passwords
are random, printed ONCE to this console and never stored; re-running resets them. Then
`clientes.entra_oid` of customers 1 and 2 gets each user's object id (`oid`), which is how the
MCP server maps a token to a customer. Needs `az login` in both tenants and SQL Entra admin.

    uv run python infra/identity/create_test_users.py --tenant-id <id> \
        --domain <subdomain>.onmicrosoft.com --sql-server <server>.database.windows.net
"""

import argparse
import secrets
import string
import sys
from pathlib import Path
from urllib.parse import quote

import graph
from sqlalchemy import update

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mcp_server.db import customers, make_engine  # noqa: E402

CUSTOMER_IDS = (1, 2)
SYMBOLS = "!#%*+-=?@_"


def new_password() -> str:
    groups = (string.ascii_lowercase, string.ascii_uppercase, string.digits, SYMBOLS)
    alphabet = "".join(groups)
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(20))
        if all(any(c in group for c in pwd) for group in groups):
            return pwd


def upsert_user(tok: str, domain: str, email: str, name: str, password: str) -> str:
    """Object id of the local account `email`: creates it, or resets its password."""
    match = f"identities/any(c:c/issuerAssignedId eq '{email}' and c/issuer eq '{domain}')"
    found = graph.call(tok, "GET", f"/users?$select=id&$filter={quote(match)}")["value"]
    profile = {"password": password, "forceChangePasswordNextSignIn": False}
    if found:
        graph.call(tok, "PATCH", f"/users/{found[0]['id']}", {"passwordProfile": profile})
        return found[0]["id"]
    user = graph.call(
        tok,
        "POST",
        "/users",
        {
            "displayName": name,
            "mail": email,
            "identities": [
                {"signInType": "emailAddress", "issuer": domain, "issuerAssignedId": email}
            ],
            "passwordProfile": profile,
            "passwordPolicies": "DisablePasswordExpiration",
        },
    )
    return user["id"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--domain", required=True, help="<subdomain>.onmicrosoft.com")
    parser.add_argument("--sql-server", required=True)
    parser.add_argument("--database", default="sqldb-banking")
    args = parser.parse_args()

    tok = graph.token(args.tenant_id)
    engine = make_engine(
        f"mssql+mssqlpython://@{args.sql_server}:1433/{args.database}"
        "?Authentication=ActiveDirectoryDefault&Encrypt=yes"
    )
    print("Contraseñas (se muestran UNA vez; no se guardan en ningún archivo):")
    for customer_id in CUSTOMER_IDS:
        email = f"cliente{customer_id}@{args.domain}"
        password = new_password()
        oid = upsert_user(tok, args.domain, email, f"Cliente demo {customer_id}", password)
        with engine.begin() as conn:
            conn.execute(
                update(customers).where(customers.c.id == customer_id).values(entra_oid=oid)
            )
        print(f"  cliente {customer_id}: {email}  {password}")
    engine.dispose()


if __name__ == "__main__":
    main()
