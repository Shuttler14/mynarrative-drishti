from shopify_sync.models import ShopifyProduct, ShopifyVariant, ShopifyImage
from shopify_sync.normalize import normalize, product_uuid


def _p(**kw):
    base = dict(id=12345, title="Libas Women Red Anarkali Kurta Set", vendor="Libas",
                product_type="Kurtas", tags="women,ethnic,red",
                images=[ShopifyImage(src="http://img/1.jpg")],
                variants=[ShopifyVariant(id=1, title="M", price="1999.00", available=True, option2="Red")])
    base.update(kw)
    return ShopifyProduct(**base)


def test_deterministic_uuid():
    assert product_uuid(12345) == product_uuid(12345)
    assert product_uuid(12345) != product_uuid(99999)


def test_classifies_ethnic_and_gender_and_color():
    n = normalize(_p())
    assert n.ontology_node_id in ("g_anarkali", "g_kurta")
    assert n.gender == "female"
    assert n.color_family == "red"
    assert n.price == 199900


def test_min_variant_price_used():
    n = normalize(_p(variants=[
        ShopifyVariant(id=1, title="M", price="1999.00", available=True),
        ShopifyVariant(id=2, title="L", price="1499.00", available=True)]))
    assert n.price == 149900


def test_draft_status_preserved():
    assert normalize(_p(status="draft")).status == "draft"
