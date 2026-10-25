import hmac
import os

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from common.secret_config import get_secret_value


def authorize_admin(headers):
    normalized = {key.lower(): value for key, value in (headers or {}).items()}
    authorization = normalized.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return 401, None

    environment = os.environ.get("APP_ENV", "production").lower()
    if environment == "local":
        local_token = get_secret_value("LOCAL_ADMIN_TOKEN")
        if local_token and hmac.compare_digest(token, local_token):
            return 200, "local-demo-operator"
        return 401, None

    audience = os.environ.get("GOOGLE_OIDC_AUDIENCE", "")
    allowed_emails = {
        email.strip().lower()
        for email in os.environ.get("ADMIN_ALLOWED_EMAILS", "").split(",")
        if email.strip()
    }
    if not audience or not allowed_emails:
        return 503, None

    try:
        claims = id_token.verify_oauth2_token(token, Request(), audience=audience)
    except (GoogleAuthError, ValueError):
        return 401, None

    email = str(claims.get("email", "")).lower()
    if claims.get("email_verified") is not True or email not in allowed_emails:
        return 403, None
    return 200, email
