from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from urllib.parse import urlencode

import requests

PINTEREST_AUTH_URL = "https://www.pinterest.com/oauth/"
PINTEREST_TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"
PINTEREST_SCOPES = "boards:read,pins:write"


class PinterestOAuthError(Exception):
    """Pinterest OAuth could not be completed."""


class PinterestService:
    @staticmethod
    def get_authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": PINTEREST_SCOPES,
            "state": state,
        }
        return f"{PINTEREST_AUTH_URL}?{urlencode(params)}"

    @staticmethod
    def exchange_authorization_code(
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
    ) -> Dict[str, Any]:
        try:
            response = requests.post(
                PINTEREST_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                auth=(client_id, client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PinterestOAuthError("Could not exchange the Pinterest authorization code.") from exc

        if response.status_code != 200:
            detail = data.get("message") or data.get("error", "Pinterest rejected the token request.")
            raise PinterestOAuthError(str(detail)[:300])

        access_token = data.get("access_token")
        if not access_token:
            raise PinterestOAuthError("Pinterest did not return an access token.")

        expires_in = data.get("expires_in")
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            if expires_in is not None
            else None
        )
        return {
            "access_token": access_token,
            "refresh_token": data.get("refresh_token"),
            "expires_at": expires_at,
        }