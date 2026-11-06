"""Customer identity for AUTH_MODE=entra (ADR-008, ADR-009).

1. The CLI signs the customer in to Entra External ID with the device code flow
   (public client `banking-cli`) and gets a token for `api://<agent-api>/access_as_user`.
2. The host, as confidential client `banking-agent-api`, exchanges that token on-behalf-of the
   customer for one for `api://<mcp-api>/tools.access`, authenticating with a certificate whose
   private key lives in Key Vault (a managed identity cannot federate into the external tenant).
3. `mcp_token` is what the MCP tool's header_provider sends as `Authorization: Bearer`.

Tokens are never printed or logged. MSAL keeps them in memory and refreshes them.
"""

from collections.abc import Callable
from typing import Any

import msal

from agent_host.config import EntraSettings


class IdentityError(Exception):
    """Sign-in or token exchange failed. The message is safe to show."""


def certificate_credential(pem: str) -> dict[str, str]:
    """MSAL client credential from Key Vault's PEM secret (private key + certificate).

    With `public_certificate`, MSAL derives the SHA-256 thumbprint and sends x5c.
    """
    return {"private_key": pem, "public_certificate": pem}


def load_certificate_pem(settings: EntraSettings, credential: Any) -> str:
    """The certificate's backing secret holds key + cert as PEM (content type x-pem-file)."""
    from azure.keyvault.secrets import SecretClient

    return SecretClient(settings.key_vault_uri, credential).get_secret(settings.cert_name).value


class EntraIdentity:
    def __init__(self, settings: EntraSettings, load_pem: Callable[[], str]) -> None:
        self.settings = settings
        self._load_pem = load_pem
        self._cli = msal.PublicClientApplication(
            settings.cli_client_id, authority=settings.authority
        )
        self._obo: msal.ConfidentialClientApplication | None = None

    def sign_in(self, show: Callable[[str], None] = print) -> str:
        """Device code sign-in. Returns a display name for the signed-in customer."""
        flow = self._cli.initiate_device_flow(scopes=[self.settings.agent_scope])
        if "user_code" not in flow:
            raise IdentityError("No se pudo iniciar el inicio de sesión por código.")
        show(flow["message"])
        result = self._cli.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise IdentityError("Inicio de sesión fallido o cancelado.")
        claims = result.get("id_token_claims") or {}
        return str(claims.get("name") or claims.get("preferred_username") or "cliente")

    def user_token(self) -> str:
        accounts = self._cli.get_accounts()
        result = accounts and self._cli.acquire_token_silent(
            [self.settings.agent_scope], account=accounts[0]
        )
        if not result or "access_token" not in result:
            raise IdentityError("La sesión expiró. Vuelve a iniciar sesión.")
        return result["access_token"]

    def _obo_app(self) -> msal.ConfidentialClientApplication:
        if self._obo is None:
            self._obo = msal.ConfidentialClientApplication(
                self.settings.agent_api_client_id,
                authority=self.settings.authority,
                client_credential=certificate_credential(self._load_pem()),
            )
        return self._obo

    def mcp_token(self) -> str:
        """OBO token for the MCP API. MSAL serves it from cache until it nears expiry."""
        # ponytail: synchronous MSAL call inside header_provider; a refresh blocks the event
        # loop for one HTTP round trip (~hourly). Move to a thread if the host serves many users.
        result = self._obo_app().acquire_token_on_behalf_of(
            user_assertion=self.user_token(), scopes=[self.settings.mcp_scope]
        )
        if "access_token" not in result:
            raise IdentityError("No se pudo obtener acceso al core bancario.")
        return result["access_token"]
