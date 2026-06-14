from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOPIFY_", extra="ignore")

    env: str = Field(default="local", alias="ENV")
    shop_domain: str = ""
    admin_api_token: str = ""
    webhook_secret: str = ""
    api_version: str = "2024-07"

    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    product_collection: str = "product_clip"
    clip_model: str = "patrickjohncyh/fashion-clip"
    embed_dim: int = 512
    device: str = "cpu"
    model_cache: str = "/models"

    gap_api_url: str = "http://gap-api.ai"
    enable_gap: bool = True

    @property
    def admin_base(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{self.api_version}"


settings = Settings()
