import pytest
from durin.automations.spec import parse_automation
from durin.automations.store import (
    AutomationNotFound,
    automations_dir,
    delete_automation,
    list_automations,
    load_automation,
    save_automation,
)


def _spec(name="renew-certs", workflow="renew-certs-wf"):
    return parse_automation({"name": name, "workflow": workflow})


def test_save_load_roundtrip(tmp_path):
    save_automation(tmp_path, _spec())
    assert (automations_dir(tmp_path) / "renew-certs.json").exists()
    assert load_automation(tmp_path, "renew-certs") == _spec()


def test_list_sorted_and_skips_malformed(tmp_path):
    save_automation(tmp_path, _spec("bbb"))
    save_automation(tmp_path, _spec("aaa"))
    (automations_dir(tmp_path) / "broken.json").write_text("{not json")
    assert [s.name for s in list_automations(tmp_path)] == ["aaa", "bbb"]


def test_list_on_missing_dir_is_empty(tmp_path):
    assert list_automations(tmp_path / "nowhere") == []


def test_load_missing_raises(tmp_path):
    with pytest.raises(AutomationNotFound):
        load_automation(tmp_path, "nope")


def test_delete(tmp_path):
    save_automation(tmp_path, _spec())
    delete_automation(tmp_path, "renew-certs")
    assert list_automations(tmp_path) == []
    with pytest.raises(AutomationNotFound):
        delete_automation(tmp_path, "renew-certs")
