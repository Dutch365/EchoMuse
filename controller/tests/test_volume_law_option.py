"""
hv-084: `ha_volume_law` add-on option (env HA_VOLUME_LAW).

legacy (default) keeps the house's pre-2.27 proportional law, round(v*127),
so the fleet's loudness does not move when the controller is updated; stock
is upstream 2.27's band law (1% = -35dB, 100% = unity). tests/test_volume.py
pins the stock law against the firmware's PercentToLevel.
"""

import importlib

import pytest

import em_volume

PCTS = (0, 1, 35, 50, 75, 100)

# pct -> (legacy level, stock level). Legacy is round(p*1.27), Python
# rounding; stock lands on the 36-level button grid from 57.
EXPECTED = {
    0: (0, 0),
    1: (1, 57),
    35: (44, 81),
    50: (64, 91),
    75: (95, 109),
    100: (127, 127),
}


@pytest.fixture
def law(monkeypatch):
    def set_(name):
        monkeypatch.setattr(em_volume, "LAW", name)
    return set_


@pytest.mark.parametrize("pct", PCTS)
def test_legacy_law_levels(law, pct):
    law(em_volume.LAW_LEGACY)
    assert em_volume.ha_volume_to_device(pct / 100) == EXPECTED[pct][0]


@pytest.mark.parametrize("pct", PCTS)
def test_stock_law_levels(law, pct):
    law(em_volume.LAW_STOCK)
    assert em_volume.ha_volume_to_device(pct / 100) == EXPECTED[pct][1]


def test_stock_law_is_louder_at_the_house_presets(law):
    """The reason legacy is the default: night 35% and announcements 75%."""
    def db(level):
        return em_volume.level_to_db(level)
    law(em_volume.LAW_LEGACY)
    night_old, ann_old = em_volume.ha_volume_to_device(0.35), em_volume.ha_volume_to_device(0.75)
    law(em_volume.LAW_STOCK)
    night_new, ann_new = em_volume.ha_volume_to_device(0.35), em_volume.ha_volume_to_device(0.75)
    assert db(night_old) == -41.5 and db(night_new) == -23.0
    assert db(ann_old) == -16.0 and db(ann_new) == -9.0


@pytest.mark.parametrize("name", [em_volume.LAW_LEGACY, em_volume.LAW_STOCK])
def test_round_trip_is_stable_under_both_laws(law, name):
    law(name)
    for pct in range(101):
        level = em_volume.ha_volume_to_device(pct / 100)
        assert 0 <= level <= em_volume.DEVICE_VOLUME_MAX
        back = em_volume.device_level_to_ha(level)
        assert em_volume.ha_volume_to_device(back) == level
    for level in range(em_volume.DEVICE_VOLUME_MAX + 1):
        if name == em_volume.LAW_LEGACY:
            assert em_volume.ha_volume_to_device(em_volume.device_level_to_ha(level)) == level


def test_legacy_matches_the_pre_hv084_house_functions(law):
    law(em_volume.LAW_LEGACY)
    for i in range(0, 1001):
        v = i / 1000
        assert em_volume.ha_volume_to_device(v) == max(0, min(127, round(v * 127)))
    for level in (-5, 0, 1, 57, 64, 127, 140, 175):
        assert em_volume.device_level_to_ha(level) == max(0.0, min(1.0, level / 127))
    assert em_volume.device_level_to_ha(None) == 0.0
    assert em_volume.ha_volume_to_device(1.5) == 127
    assert em_volume.ha_volume_to_device(-0.5) == 0


@pytest.mark.parametrize("env,want", [
    (None, "legacy"), ("", "legacy"), ("legacy", "legacy"), ("stock", "stock"),
    (" STOCK ", "stock"), ("band", "legacy"),
])
def test_env_selects_the_law_and_garbage_is_legacy(monkeypatch, env, want):
    if env is None:
        monkeypatch.delenv("HA_VOLUME_LAW", raising=False)
    else:
        monkeypatch.setenv("HA_VOLUME_LAW", env)
    try:
        assert importlib.reload(em_volume).LAW == want
    finally:
        monkeypatch.delenv("HA_VOLUME_LAW", raising=False)
        importlib.reload(em_volume)


def test_controller_aliases_follow_the_setting(law):
    """em_controller binds aliases at import; they must dispatch per call."""
    alias_to_dev, alias_to_ha = em_volume.ha_volume_to_device, em_volume.device_level_to_ha
    law(em_volume.LAW_LEGACY)
    assert alias_to_dev(0.35) == 44 and alias_to_ha(127) == 1.0
    law(em_volume.LAW_STOCK)
    assert alias_to_dev(0.35) == 81 and alias_to_ha(57) == 0.01


def test_equivalence_table_for_a_later_switch(law):
    """For the PR: the lowest stock % that gives the level nearest today's
    legacy %. Legacy at or below 45% is at or under the band's floor (-35dB),
    which stock cannot go below except to silence: 1% is the closest."""
    law(em_volume.LAW_STOCK)
    stock = {p: em_volume.ha_volume_to_device(p / 100) for p in range(1, 101)}

    def equiv(level):
        return min(stock, key=lambda p: (abs(stock[p] - level), p))
    law(em_volume.LAW_LEGACY)
    table = {p: equiv(em_volume.ha_volume_to_device(p / 100)) for p in (10, 20, 35, 50, 75, 90, 100)}
    assert table == {10: 1, 20: 1, 35: 1, 50: 9, 75: 54, 90: 79, 100: 99}
