"""Create the sign-in user flow of the external tenant and link the CLI app to it (idempotent).

External ID signs users in only to apps associated with a user flow; azuread has no resource
for it, so this script uses Graph. Sign-up is disabled: the bank creates its customers
(create_test_users.py); nobody self-registers.

    uv run python infra/identity/user_flow.py --tenant-id <id> --app-id <banking-cli client id>
"""

import argparse

import graph

FLOW_NAME = "banco-andino-signin"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--app-id", required=True, help="client id of banking-cli")
    args = parser.parse_args()
    tok = graph.token(args.tenant_id)

    flows = graph.call(tok, "GET", "/identity/authenticationEventsFlows")["value"]
    flow = next((f for f in flows if f.get("displayName") == FLOW_NAME), None)
    if flow is None:
        flow = graph.call(
            tok,
            "POST",
            "/identity/authenticationEventsFlows",
            {
                "@odata.type": "#microsoft.graph.externalUsersSelfServiceSignUpEventsFlow",
                "displayName": FLOW_NAME,
                "onAuthenticationMethodLoadStart": {
                    "@odata.type": "#microsoft.graph."
                    "onAuthenticationMethodLoadStartExternalUsersSelfServiceSignUp",
                    "identityProviders": [{"id": "EmailPassword-OAUTH"}],
                },
                "onInteractiveAuthFlowStart": {
                    "@odata.type": "#microsoft.graph."
                    "onInteractiveAuthFlowStartExternalUsersSelfServiceSignUp",
                    "isSignUpAllowed": False,
                },
            },
        )
        print(f"Flujo creado: {FLOW_NAME} ({flow['id']})")

    base = f"/identity/authenticationEventsFlows/{flow['id']}/conditions/applications"
    linked = graph.call(tok, "GET", f"{base}/includeApplications")["value"]
    if any(a.get("appId") == args.app_id for a in linked):
        print(f"La app {args.app_id} ya está asociada a {FLOW_NAME}.")
        return
    graph.call(
        tok,
        "POST",
        f"{base}/includeApplications",
        {
            "@odata.type": "#microsoft.graph.authenticationConditionApplication",
            "appId": args.app_id,
        },
    )
    print(f"App {args.app_id} asociada a {FLOW_NAME}.")


if __name__ == "__main__":
    main()
