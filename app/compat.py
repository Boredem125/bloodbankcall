"""Blood group compatibility, for information only.

Shown to a family when no exact match was confirmed, so they can ask the treating
doctor. We never search for alternatives on our own: only the doctor decides.
"""
from __future__ import annotations

from .schemas import BLOOD_GROUPS

DOCTOR_WARNING = "Use another group only if the treating doctor approves."

# ABO antigens on the red cells of each group, and antibodies in its plasma.
_ANTIGENS = {"O": set(), "A": {"A"}, "B": {"B"}, "AB": {"A", "B"}}
_ANTIBODIES = {"O": {"A", "B"}, "A": {"B"}, "B": {"A"}, "AB": set()}

NOTES = {
    "prbc": "Red cells: recipient plasma must not attack donor cells. Rh-negative patients get Rh-negative only.",
    "whole_blood": "Whole blood carries donor plasma too, so only the same ABO group is listed. "
                   "Rh-negative patients get Rh-negative only.",
    "ffp": "Plasma follows the reverse rule: donor plasma must not attack the patient's cells. Rh does not matter.",
    "platelets": "Platelets: ABO-compatible plasma is preferred, not strictly required. "
                 "Rh-negative patients should get Rh-negative where possible.",
}


def _split(group: str) -> tuple[str, bool]:
    return group[:-1], group.endswith("+")


def _red_cells_ok(recipient: str, donor: str) -> bool:
    (r_abo, r_pos), (d_abo, d_pos) = _split(recipient), _split(donor)
    return not (_ANTIGENS[d_abo] & _ANTIBODIES[r_abo]) and (r_pos or not d_pos)


def _plasma_ok(recipient: str, donor: str) -> bool:
    return not (_ANTIBODIES[_split(donor)[0]] & _ANTIGENS[_split(recipient)[0]])


def compatible_donors(group: str, component: str) -> list[str]:
    """All donor groups acceptable for `group`, best first (the same group leads)."""
    if group not in BLOOD_GROUPS:
        raise ValueError(f"unknown blood group {group!r}")
    r_abo, r_pos = _split(group)
    if component == "prbc":
        donors = [d for d in BLOOD_GROUPS if _red_cells_ok(group, d)]
    elif component == "whole_blood":
        donors = [d for d in BLOOD_GROUPS if _split(d)[0] == r_abo and _red_cells_ok(group, d)]
    elif component in ("ffp", "platelets"):
        donors = [d for d in BLOOD_GROUPS if _plasma_ok(group, d)]
    else:
        raise ValueError(f"unknown component {component!r}")

    def key(d: str) -> tuple[int, int, int]:
        d_abo, d_pos = _split(d)
        abo = 0 if d_abo == r_abo else 2 if d_abo in ("O", "AB") else 1
        return (abo, int(d_pos != r_pos), BLOOD_GROUPS.index(d))

    return sorted(donors, key=key)


def compatible_alternatives(group: str, component: str) -> list[str]:
    """Other groups the treating doctor might approve, best first. Excludes `group` itself."""
    return [d for d in compatible_donors(group, component) if d != group]


def alternatives_info(group: str, component: str) -> dict:
    return {
        "groups": compatible_alternatives(group, component),
        "note": NOTES[component],
        "warning": DOCTOR_WARNING,
    }
