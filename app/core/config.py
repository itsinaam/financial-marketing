
from functools import lru_cache
from decouple import config
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = config("APP_NAME")
    DATABASE_URL: str = config("DATABASE_URL")

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
