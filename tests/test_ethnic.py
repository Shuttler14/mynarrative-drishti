from vtoe.ethnic.drape_params import get
from vtoe.ethnic.lora_registry import for_subtype


def test_saree_gets_floor_extension_and_high_ip_scale():
    p = get("saree", "ethnic")
    assert p.extend_to_floor is True
    assert p.ip_adapter_scale >= 0.8


def test_kurta_no_floor_extension():
    assert get("kurta", "ethnic").extend_to_floor is False


def test_unknown_subtype_falls_back_to_type():
    assert get(None, "western").prompt_suffix == get("western", "western").prompt_suffix


def test_lora_mapping():
    assert for_subtype("saree") == ("mynarrative/lora-saree", 0.8)
    assert for_subtype("anarkali")[0] == "mynarrative/lora-kurta"
    assert for_subtype("unknown") is None
