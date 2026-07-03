"""
Bank card offers database — aggregated from major Indian e-commerce platforms.
Covers HDFC, ICICI, SBI, Axis, Kotak, AMEX, and major fintech cards.
Updated: July 2026
"""
from typing import Optional


# ── Card offer data structure ──
# Each offer: {bank, card_type, platform, discount_pct, max_discount, min_order, code, valid_until, terms}

CARD_OFFERS: dict[str, list[dict]] = {
    # ── Amazon India ──
    "amazon": [
        {"bank": "HDFC", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1500, "code": "HDFC10", "valid_until": "2026-08-31", "terms": "On all HDFC credit cards. One per customer."},
        {"bank": "ICICI", "card_type": "credit", "discount_pct": 10, "max_discount": 750, "min_order": 2000, "code": "ICICI10", "valid_until": "2026-08-31", "terms": "ICICI Bank Credit Card EMI only."},
        {"bank": "SBI", "card_type": "credit", "discount_pct": 5, "max_discount": 250, "min_order": 1000, "code": "SBICARD", "valid_until": "2026-07-31", "terms": "SBI Credit Card only. Instant discount."},
        {"bank": "Axis", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 2000, "code": "AXIS10", "valid_until": "2026-09-30", "terms": "Axis Bank Credit Card. Min order ₹2000."},
        {"bank": "Kotak", "card_type": "credit", "discount_pct": 7.5, "max_discount": 300, "min_order": 1500, "code": "KOTAK75", "valid_until": "2026-08-15", "terms": "Kotak Credit Card. First 500 orders only."},
        {"bank": "AMEX", "card_type": "credit", "discount_pct": 10, "max_discount": 1000, "min_order": 3000, "code": "AMEX10", "valid_until": "2026-09-30", "terms": "American Express Card. Platinum only."},
        {"bank": "AU", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1500, "code": "AUBANK", "valid_until": "2026-08-31", "terms": "AU Small Finance Bank Credit Card."},
    ],

    # ── Myntra ──
    "myntra": [
        {"bank": "HDFC", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1999, "code": "HDFC10", "valid_until": "2026-07-31", "terms": "HDFC Credit Card. Min ₹1999. Max 3 uses/user."},
        {"bank": "HDFC", "card_type": "debit", "discount_pct": 5, "max_discount": 250, "min_order": 1499, "code": "HDFC5", "valid_until": "2026-07-31", "terms": "HDFC Debit Card. Min ₹1499."},
        {"bank": "ICICI", "card_type": "credit", "discount_pct": 10, "max_discount": 750, "min_order": 2499, "code": "ICICI10", "valid_until": "2026-08-15", "terms": "ICICI Credit Card. No EMI."},
        {"bank": "SBI", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1999, "code": "SBI10", "valid_until": "2026-07-31", "terms": "SBI Credit Card. Limited slots."},
        {"bank": "Axis", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1999, "code": "AXIS10", "valid_until": "2026-08-31", "terms": "Axis Bank Credit Card. No COD."},
        {"bank": "Kotak", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1999, "code": "KOTAK10", "valid_until": "2026-07-31", "terms": "Kotak Credit Card. Min ₹1999."},
        {"bank": "OneCard", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1999, "code": "ONECARD", "valid_until": "2026-08-31", "terms": "OneCard Metal Credit Card."},
        {"bank": "Flipkart.Axis", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1999, "code": "FKAXIS", "valid_until": "2026-07-31", "terms": "Flipkart Axis Bank Credit Card."},
    ],

    # ── Flipkart ──
    "flipkart": [
        {"bank": "HDFC", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1999, "code": "HDFC10", "valid_until": "2026-08-31", "terms": "HDFC Credit Card. Instant discount."},
        {"bank": "ICICI", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1999, "code": "ICICI10", "valid_until": "2026-08-15", "terms": "ICICI Credit Card. No EMI."},
        {"bank": "SBI", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1999, "code": "SBI10", "valid_until": "2026-07-31", "terms": "SBI Credit Card. Limited time."},
        {"bank": "Axis", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1999, "code": "AXIS10", "valid_until": "2026-09-30", "terms": "Axis Bank Credit Card. Min ₹1999."},
        {"bank": "Flipkart.Axis", "card_type": "credit", "discount_pct": 5, "max_discount": 250, "min_order": 1499, "code": "FKAXIS5", "valid_until": "2026-07-31", "terms": "Flipkart Axis Bank Credit Card. 5% assured."},
        {"bank": "Flipkart.Paytm", "card_type": "credit", "discount_pct": 5, "max_discount": 200, "min_order": 999, "code": "FKPAYTM", "valid_until": "2026-08-15", "terms": "Flipkart Paytm Credit Card."},
    ],

    # ── AJIO ──
    "ajio": [
        {"bank": "HDFC", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 2499, "code": "HDFC10", "valid_until": "2026-08-31", "terms": "HDFC Credit Card. No EMI."},
        {"bank": "ICICI", "card_type": "credit", "discount_pct": 10, "max_discount": 750, "min_order": 2999, "code": "ICICI10", "valid_until": "2026-08-15", "terms": "ICICI Credit Card. Excl. EMI."},
        {"bank": "SBI", "card_type": "credit", "discount_pct": 10, "max_discount": 500, "min_order": 1999, "code": "SBI10", "valid_until": "2026-07-31", "terms": "SBI Credit Card. Min ₹1999."},
    ],
}


def get_best_card_offer(
    platform: str,
    price: float,
    user_card_bank: Optional[str] = None,
    user_card_type: str = "credit",
) -> Optional[dict]:
    """
    Find the best card offer for a given platform and price.
    If user_card_bank is specified, prioritize that bank's offers.
    """
    offers = CARD_OFFERS.get(platform.lower(), [])
    if not offers:
        return None

    eligible = []
    for offer in offers:
        if price < offer["min_order"]:
            continue
        if offer["card_type"] != user_card_type:
            continue
        
        # Calculate actual discount
        discount = min(price * offer["discount_pct"] / 100, offer["max_discount"])
        
        eligible.append({
            **offer,
            "actual_discount": round(discount),
            "final_price": round(price - discount),
        })

    if not eligible:
        return None

    # Sort by actual discount (highest first)
    eligible.sort(key=lambda x: x["actual_discount"], reverse=True)

    # If user specified a bank, prioritize that
    if user_card_bank:
        bank_match = [o for o in eligible if o["bank"].lower() == user_card_bank.lower()]
        if bank_match:
            return bank_match[0]

    return eligible[0]


def apply_card_offers(
    comparisons: list[dict],
    user_card_bank: Optional[str] = None,
    user_card_type: str = "credit",
) -> list[dict]:
    """
    Apply card offers to all comparison results.
    Returns the comparison list with card offer details added.
    """
    for comp in comparisons:
        platform = comp.get("source", "")
        price = comp.get("price", 0)
        
        if price <= 0:
            comp["card_offer"] = None
            continue
        
        offer = get_best_card_offer(platform, price, user_card_bank, user_card_type)
        comp["card_offer"] = offer
    
    return comparisons


def get_all_offers_for_platform(platform: str) -> list[dict]:
    """Get all available card offers for a platform."""
    return CARD_OFFERS.get(platform.lower(), [])
