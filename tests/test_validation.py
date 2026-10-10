import pytest

from conftest import INSTRUCTION
from reasoning_agent import build_system_prompt
from validation import InstructionError, SiteConfig, validate_instruction


def test_valid_instruction_cleaned(site):
    out = validate_instruction({**INSTRUCTION, "product": "  ABCD ", "extra": "ignored"}, site)
    assert out["product"] == "ABCD" and "extra" not in out


@pytest.mark.parametrize("change,needle", [
    ({"source": "Manufacturing_1"}, "not a known location"),
    ({"success_dest": ""}, "non-empty"),
    ({"fail_dest": None}, "non-empty"),
    ({"product": "AB;rm -rf"}, "product"),
    ({"product": "x" * 65}, "product"),
    ({"fail_dest": "package_area"}, "must differ"),
    ({"request_id": 7}, "request_id"),
])
def test_invalid_fields(site, change, needle):
    with pytest.raises(InstructionError, match=needle):
        validate_instruction({**INSTRUCTION, **change}, site)


def test_missing_keys(site):
    bad = dict(INSTRUCTION)
    del bad["quality_check"]
    with pytest.raises(InstructionError, match="missing"):
        validate_instruction(bad, site)


def test_qc_zone_must_have_station(site):
    with pytest.raises(InstructionError, match="no QC station"):
        validate_instruction(INSTRUCTION, site, inspection_zones=["other_zone"])
    assert validate_instruction(INSTRUCTION, site, inspection_zones=["quality_station"])


def test_site_requires_locations():
    with pytest.raises(ValueError):
        SiteConfig.from_dict({"locations": {}})


def test_system_prompt_lists_exactly_site_locations(site):
    prompt = build_system_prompt(site)
    for name in site.locations:
        assert f'"{name}"' in prompt
