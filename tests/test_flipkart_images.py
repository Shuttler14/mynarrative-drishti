"""Flipkart HTML strategy must extract product images.

Regression: image_url was hardcoded "" → every Flipkart item died at the
imageless-product gate (RECO-v4.3) — the scraper contributed titles but
zero usable inventory.
"""
import asyncio
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from api.services import price_scraper as ps  # noqa: E402

FIXTURE_HTML = """
<html><body>
<script>window.someOtherVar = 1;</script>
<div data-id="SHTXAMPLE01" style="width:25%">
  <div class="bLCLBY nr15la">
    <a class="CIaYa1" target="_blank" rel="noopener noreferrer"
       href="/women-solid-white-formal-shirt/p/itm92c58850b254?pid=SHTXAMPLE01&lid=LST"
       title="Women Regular Fit Solid Spread Collar Formal Shirt">
    </a>
    <img class="MZeksS" alt="" src="https://rukminim2.flixcart.com/image/612/612/xif0q/shirt/x/c/x/s-white-formal-original-imahzpr9s82h65fz.jpeg?q=70"/>
    <div>₹799</div><div>₹1,299</div><span>4.3 ★</span>
  </div>
</div>
<div data-id="SHTXAMPLE02" style="width:25%">
  <a href="/women-blue-casual-shirt/p/itmabcdef12345?pid=SHTXAMPLE02&lid=LST"
     title="Women Slim Fit Blue Casual Shirt"></a>
  <img class="MZeksS" src="//rukminim2.flixcart.com/image/612/612/xif0q/shirt/b/l/u/blue-casual-original-ima987654321.jpeg?q=70"/>
  <div>₹549</div><div>₹999</div><span>4.1 ★</span>
</div>
</body></html>
"""


class _Resp:
    text = FIXTURE_HTML


async def _fake_fetch(url, headers, **kwargs):
    return _Resp()


async def _fake_rate_limit(domain):
    return None


def test_flipkart_html_strategy_extracts_images(monkeypatch):
    monkeypatch.setattr(ps, "_fetch_with_retry", _fake_fetch)
    monkeypatch.setattr(ps, "_rate_limit", _fake_rate_limit)
    monkeypatch.setattr(ps, "_get_cached", lambda *a, **k: None)
    monkeypatch.setattr(ps, "_set_cache", lambda *a, **k: None)

    products = asyncio.run(ps.scrape_flipkart("women formal shirt", max_results=5))

    assert len(products) == 2, [p.get("title") for p in products]
    first, second = products

    assert first["image_url"].startswith("https://rukminim2.flixcart.com/image/"), first
    assert first["price"] == 799, first
    assert "Formal Shirt" in first["title"], first["title"]

    # protocol-relative URLs must be absolutized (VTON/fetch cannot use //)
    assert second["image_url"].startswith("https://rukminim2.flixcart.com/image/"), second
    assert second["price"] == 549, second
    for p in products:
        assert p["image_url"], f"imageless flipkart item leaked: {p}"
