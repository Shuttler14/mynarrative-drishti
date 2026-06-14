from __future__ import annotations
import uuid
from pydantic import BaseModel, Field


class ShopifyImage(BaseModel):
    src: str


class ShopifyVariant(BaseModel):
    id: int
    title: str
    price: str
    available: bool = True
    option1: str | None = None
    option2: str | None = None


class ShopifyProduct(BaseModel):
    id: int
    title: str
    body_html: str | None = None
    vendor: str | None = None
    product_type: str | None = None
    tags: str | list[str] = ""
    status: str = "active"
    images: list[ShopifyImage] = Field(default_factory=list)
    variants: list[ShopifyVariant] = Field(default_factory=list)
    options: list[dict] = Field(default_factory=list)


class CanonicalProduct(BaseModel):
    product_id: uuid.UUID
    shopify_id: int
    title: str
    canonical_title: str
    brand: str | None
    category: str | None
    gender: str | None
    color_family: str | None
    color_hex: str | None
    ontology_node_id: str | None
    role: str
    price: int | None
    in_stock: bool
    primary_image: str | None
    images: list[str]
    attributes: dict
    status: str
