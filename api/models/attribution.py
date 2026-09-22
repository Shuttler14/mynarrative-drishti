"""
MY NARRATIVE — Attribution Pydantic Models
Change ID: ADD-CHK-017-260922
"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class ProductRegisterRequest(BaseModel):
    brand_id: str
    product_name: str
    shopify_product_id: Optional[str] = None
    shopify_variant_ids: Optional[List[str]] = []
    external_id: Optional[str] = None
    canonical_url: Optional[str] = None
    product_data: Optional[dict] = {}


class ProductRegisterBatchRequest(BaseModel):
    brand_id: str
    products: List[ProductRegisterRequest]


class ClickRecordRequest(BaseModel):
    mn_product_id: str
    host_brand_id: str
    advertiser_brand_id: str
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    fingerprint: Optional[str] = None
    campaign_id: Optional[str] = None
    vton_session_id: Optional[str] = None
    source: str = "widget"
    source_detail: Optional[str] = None
    destination_url: Optional[str] = None


class OrderItem(BaseModel):
    mn_product_id: Optional[str] = None
    shopify_product_id: Optional[str] = None
    shopify_variant_id: Optional[str] = None
    order_item_id: Optional[str] = None
    price: float = 0
    quantity: int = 1
    sku: Optional[str] = None
    advertiser_brand_id: str = ""
    host_brand_id: Optional[str] = None
    discount: Optional[float] = 0
    tax: Optional[float] = 0
    shipping: Optional[float] = 0


class PurchaseAttributeRequest(BaseModel):
    user_id: str
    order_id: str
    order_items: List[OrderItem]
    source: str = "checkout"


class MerchantPixelEventRequest(BaseModel):
    merchant_brand_id: str
    event_type: str
    mn_click_id: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    product_data: Optional[dict] = None
    order_data: Optional[dict] = None


class RefundItem(BaseModel):
    order_item_id: str
    refund_amount: float


class RefundRequest(BaseModel):
    order_id: str
    refund_items: List[RefundItem]
    reason: str = "customer_refund"
