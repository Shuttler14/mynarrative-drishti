import numpy as np
import pytest
from PIL import Image, ImageDraw

from vtoe.config import settings


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch):
    settings.vram_budget_gb = 14.5
    yield


@pytest.fixture
def person_image() -> Image.Image:
    img = Image.new("RGB", (768, 1024), (240, 240, 240))
    d = ImageDraw.Draw(img)
    d.ellipse([320, 120, 450, 260], fill=(225, 190, 165))
    d.rectangle([280, 280, 490, 720], fill=(210, 175, 150))
    return img


@pytest.fixture
def garment_image() -> Image.Image:
    img = Image.new("RGB", (768, 1024), (255, 255, 255))
    ImageDraw.Draw(img).rectangle([200, 200, 560, 820], fill=(180, 40, 40))
    return img


@pytest.fixture
def red_mask() -> Image.Image:
    arr = np.zeros((1024, 768), np.uint8)
    arr[200:820, 200:560] = 255
    return Image.fromarray(arr)
