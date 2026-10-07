"""Application Credentials platform - Google OAuth2 endpoints for library_api."""
from __future__ import annotations

from homeassistant.components.application_credentials import (
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .const import OAUTH2_AUTHORIZE, OAUTH2_SCOPES, OAUTH2_TOKEN


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    return AuthorizationServer(authorize_url=OAUTH2_AUTHORIZE, token_url=OAUTH2_TOKEN)


class GooglePhotosOAuth2Implementation(config_entry_oauth2_flow.LocalOAuth2Implementation):
    """Adds the scope + offline/consent params Google requires for a refresh token."""

    @property
    def extra_authorize_data(self) -> dict:
        return {
            "scope": " ".join(OAUTH2_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
        }


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    return GooglePhotosOAuth2Implementation(
        hass,
        auth_domain,
        credential.client_id,
        credential.client_secret,
        OAUTH2_AUTHORIZE,
        OAUTH2_TOKEN,
    )


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    return {
        "oauth_consent_url": "https://console.cloud.google.com/apis/credentials/consent",
        "more_info_url": "https://www.home-assistant.io/integrations/application_credentials/",
        "redirect_url": config_entry_oauth2_flow.async_get_redirect_uri(hass),
    }
