import pytest

from app.compat import DOCTOR_WARNING, alternatives_info, compatible_alternatives, compatible_donors
from app.schemas import BLOOD_GROUPS


def test_o_negative_recipient_only_o_negative():
    assert compatible_donors("O-", "prbc") == ["O-"]
    assert compatible_alternatives("O-", "prbc") == []


def test_ab_positive_prbc_accepts_all_same_group_first():
    donors = compatible_donors("AB+", "prbc")
    assert sorted(donors) == sorted(BLOOD_GROUPS)
    assert donors[0] == "AB+" and donors[-1] == "O-"


def test_rh_negative_never_gets_positive_red_cells():
    assert compatible_donors("A-", "prbc") == ["A-", "O-"]
    assert compatible_alternatives("B+", "prbc") == ["B-", "O+", "O-"]


def test_ffp_is_reversed():
    assert compatible_donors("AB+", "ffp") == ["AB+", "AB-"]
    assert sorted(compatible_donors("O-", "ffp")) == sorted(BLOOD_GROUPS)
    assert set(compatible_donors("A+", "ffp")) == {"A+", "A-", "AB+", "AB-"}


def test_whole_blood_same_abo_only():
    assert compatible_donors("A+", "whole_blood") == ["A+", "A-"]
    assert compatible_donors("O-", "whole_blood") == ["O-"]


def test_platelets_and_info_carry_doctor_warning():
    info = alternatives_info("B+", "platelets")
    assert info["warning"] == DOCTOR_WARNING and "treating doctor approves" in info["warning"]
    assert "preferred" in info["note"]
    assert "B+" not in info["groups"] and "AB+" in info["groups"]


def test_unknown_group_rejected():
    with pytest.raises(ValueError):
        compatible_donors("X+", "prbc")
