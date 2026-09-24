
from functools import lru_cache
from decouple import config
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = config("APP_NAME")
    DATABASE_URL: str = config("DATABASE_URL")
    PUBLIC_BASE_URL: str = config("PUBLIC_BASE_URL", default="")
    INSTAGRAM_REDIRECT_URI: str = config("INSTAGRAM_REDIRECT_URI", default="")

    # Meta app credentials used by the one-click Instagram connection flow.
    INSTAGRAM_CLIENT_ID: str = config("INSTAGRAM_CLIENT_ID", default="")
    INSTAGRAM_CLIENT_SECRET: str = config("INSTAGRAM_CLIENT_SECRET", default="")

    # Meta app credentials used by the one-click Facebook Page connection flow.
    FACEBOOK_REDIRECT_URI: str = config("FACEBOOK_REDIRECT_URI", default="")
    FACEBOOK_CLIENT_ID: str = config("FACEBOOK_CLIENT_ID", default="")
    FACEBOOK_CLIENT_SECRET: str = config("FACEBOOK_CLIENT_SECRET", default="")

    # X app credentials used by the one-click X connection flow.
    X_REDIRECT_URI: str = config("X_REDIRECT_URI", default="")
    X_CLIENT_ID: str = config("X_CLIENT_ID", default="")
    X_CLIENT_SECRET: str = config("X_CLIENT_SECRET", default="")

    # Google app credentials used by the one-click Blogger connection flow.
    GOOGLE_REDIRECT_URI: str = config("GOOGLE_REDIRECT_URI", default="")
    GOOGLE_CLIENT_ID: str = config("GOOGLE_CLIENT_ID", default="")
    GOOGLE_CLIENT_SECRET: str = config("GOOGLE_CLIENT_SECRET", default="")

    # Wix app credentials used by the one-click Wix connection flow.
    WIX_REDIRECT_URI: str = config("WIX_REDIRECT_URI", default="")
    WIX_APP_ID: str = config("WIX_APP_ID", default="")
    WIX_APP_SECRET: str = config("WIX_APP_SECRET", default="")
    # shareUrlId of the app's share-install link, required by the external
    # install flow for unlisted apps. Find it by opening the share link and
    # copying its location UUID (wix.com/app-market/install/<shareUrlId>).
    WIX_SHARE_URL_ID: str = config("WIX_SHARE_URL_ID", default="")

    SECRET_KEY: str = config("SECRET_KEY")
    ALGORITHM: str = config("ALGORITHM")

    # Fernet key used to encrypt credentials for wordpress, blogger, and wix only.
    # Generate one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    CREDENTIALS_ENCRYPTION_KEY: str = config("CREDENTIALS_ENCRYPTION_KEY", default="")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = config("ACCESS_TOKEN_EXPIRE_MINUTES")
    API_V1_STR: str = config("API_V1_STR")
    
    SUPERADMIN_EMAIL: str = config("SUPERADMIN_EMAIL")
    SUPERADMIN_PASSWORD: str = config("SUPERADMIN_PASSWORD")

    STRIPE_SECRET_KEY: str = config("STRIPE_SECRET_KEY")
    STRIPE_PUBLISHABLE_KEY: str = config("STRIPE_PUBLISHABLE_KEY")
    STRIPE_WEBHOOK_SECRET: str = config("STRIPE_WEBHOOK_SECRET")

    class Config:
        extra = "ignore"


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
