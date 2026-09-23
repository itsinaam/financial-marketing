
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

    # WhatsApp Cloud API sender used for team alerts (one sender for the whole app).
    WHATSAPP_PHONE_NUMBER_ID: str = config("WHATSAPP_PHONE_NUMBER_ID", default="")
    WHATSAPP_ACCESS_TOKEN: str = config("WHATSAPP_ACCESS_TOKEN", default="")
    WHATSAPP_API_VERSION: str = config("WHATSAPP_API_VERSION", default="v21.0")

    # X app credentials used by the one-click X connection flow.
    X_REDIRECT_URI: str = config("X_REDIRECT_URI", default="")
    X_CLIENT_ID: str = config("X_CLIENT_ID", default="")
    X_CLIENT_SECRET: str = config("X_CLIENT_SECRET", default="")

    SECRET_KEY: str = config("SECRET_KEY")
    ALGORITHM: str = config("ALGORITHM")
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
