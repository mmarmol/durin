"""Provenance-aware builtin workflow seeding.

The contract: a seed the user never touched follows the wheel (auto-refresh on
upgrade); a seed the user edited is theirs (an update becomes a suggestion,
never a clobber); a pre-manifest workspace is adopted — files matching the
current template become tracked, diverged ones surface once as
unknown-provenance suggestions and a human decides.
"""

import json
from pathlib import Path

import pytest

from durin.workflow.seeds import (
    apply_suggestion,
    dismiss_suggestion,
    list_suggestions,
    refresh_seeds,
)


@pytest.fixture()
def tmp_env(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "workflows").mkdir(parents=True)
    templates = tmp_path / "templates"
    templates.mkdir()
    return workspace, templates


def write_template(templates: Path, name: str, payload: dict) -> Path:
    p = templates / f"{name}.json"
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return p


def wf_file(workspace: Path, name: str) -> Path:
    return workspace / "workflows" / f"{name}.json"


def manifest(workspace: Path) -> dict:
    p = workspace / "workflows" / ".seeds.json"
    return json.loads(p.read_text()) if p.is_file() else {}


TPL_V1 = {"name": "demo", "nodes": [{"id": "a", "kind": "work", "prompt": "v1"}]}
TPL_V2 = {"name": "demo", "nodes": [{"id": "a", "kind": "work", "prompt": "v2"}]}


class TestFreshInstall:
    def test_installs_missing_templates_and_records_provenance(self, tmp_env):
        workspace, templates = tmp_env
        write_template(templates, "demo", TPL_V1)

        report = refresh_seeds(workspace, templates_dir=templates)

        assert report.installed == ["demo"]
        assert json.loads(wf_file(workspace, "demo").read_text()) == TPL_V1
        entry = manifest(workspace)["demo"]
        assert entry["provenance"] == "seed"

    def test_idempotent_second_pass_changes_nothing(self, tmp_env):
        workspace, templates = tmp_env
        write_template(templates, "demo", TPL_V1)
        refresh_seeds(workspace, templates_dir=templates)

        report = refresh_seeds(workspace, templates_dir=templates)

        assert report.installed == []
        assert report.refreshed == []
        assert report.suggested == []


class TestUntouchedUpgrade:
    def test_new_template_version_overwrites_untouched_seed(self, tmp_env):
        workspace, templates = tmp_env
        write_template(templates, "demo", TPL_V1)
        refresh_seeds(workspace, templates_dir=templates)

        write_template(templates, "demo", TPL_V2)
        report = refresh_seeds(workspace, templates_dir=templates)

        assert report.refreshed == ["demo"]
        assert json.loads(wf_file(workspace, "demo").read_text()) == TPL_V2
        assert list_suggestions(workspace, templates_dir=templates) == []


class TestEditedSeed:
    def _install_and_edit(self, workspace, templates):
        write_template(templates, "demo", TPL_V1)
        refresh_seeds(workspace, templates_dir=templates)
        edited = dict(TPL_V1, description="customized by user")
        wf_file(workspace, "demo").write_text(json.dumps(edited, indent=2) + "\n")
        return edited

    def test_edit_without_new_template_is_left_alone(self, tmp_env):
        workspace, templates = tmp_env
        edited = self._install_and_edit(workspace, templates)

        report = refresh_seeds(workspace, templates_dir=templates)

        assert report.suggested == []
        assert json.loads(wf_file(workspace, "demo").read_text()) == edited

    def test_new_template_over_edited_seed_becomes_suggestion(self, tmp_env):
        workspace, templates = tmp_env
        edited = self._install_and_edit(workspace, templates)
        write_template(templates, "demo", TPL_V2)

        report = refresh_seeds(workspace, templates_dir=templates)

        assert report.suggested == ["demo"]
        assert json.loads(wf_file(workspace, "demo").read_text()) == edited
        (sugg,) = list_suggestions(workspace, templates_dir=templates)
        assert sugg["name"] == "demo"
        assert sugg["reason"] == "edited"
        assert "v2" in sugg["diff"]

    def test_suggestion_is_stable_across_passes(self, tmp_env):
        workspace, templates = tmp_env
        self._install_and_edit(workspace, templates)
        write_template(templates, "demo", TPL_V2)
        refresh_seeds(workspace, templates_dir=templates)

        report = refresh_seeds(workspace, templates_dir=templates)

        assert report.suggested == []          # already pending, not duplicated
        assert len(list_suggestions(workspace, templates_dir=templates)) == 1


class TestAdoption:
    def test_matching_file_without_manifest_is_adopted(self, tmp_env):
        workspace, templates = tmp_env
        write_template(templates, "demo", TPL_V1)
        wf_file(workspace, "demo").write_text(
            json.dumps(TPL_V1, indent=2) + "\n", encoding="utf-8")

        report = refresh_seeds(workspace, templates_dir=templates)

        assert report.adopted == ["demo"]
        assert manifest(workspace)["demo"]["provenance"] == "seed"

    def test_diverged_file_without_manifest_is_unknown_and_suggested(self, tmp_env):
        workspace, templates = tmp_env
        write_template(templates, "demo", TPL_V2)
        stale = json.dumps(TPL_V1, indent=2) + "\n"
        wf_file(workspace, "demo").write_text(stale, encoding="utf-8")

        report = refresh_seeds(workspace, templates_dir=templates)

        assert report.unknown == ["demo"]
        assert wf_file(workspace, "demo").read_text() == stale   # never clobbered
        assert manifest(workspace)["demo"]["provenance"] == "unknown"
        (sugg,) = list_suggestions(workspace, templates_dir=templates)
        assert sugg["reason"] == "unknown-provenance"

    def test_unknown_file_later_matching_template_flips_to_seed(self, tmp_env):
        workspace, templates = tmp_env
        write_template(templates, "demo", TPL_V2)
        wf_file(workspace, "demo").write_text(json.dumps(TPL_V1, indent=2) + "\n")
        refresh_seeds(workspace, templates_dir=templates)

        wf_file(workspace, "demo").write_text(json.dumps(TPL_V2, indent=2) + "\n")
        refresh_seeds(workspace, templates_dir=templates)

        assert manifest(workspace)["demo"]["provenance"] == "seed"
        assert list_suggestions(workspace, templates_dir=templates) == []


class TestApplyAndDismiss:
    def _edited_with_pending(self, workspace, templates):
        write_template(templates, "demo", TPL_V1)
        refresh_seeds(workspace, templates_dir=templates)
        wf_file(workspace, "demo").write_text(
            json.dumps(dict(TPL_V1, description="mine"), indent=2) + "\n")
        write_template(templates, "demo", TPL_V2)
        refresh_seeds(workspace, templates_dir=templates)

    def test_apply_overwrites_records_and_clears(self, tmp_env):
        workspace, templates = tmp_env
        self._edited_with_pending(workspace, templates)

        out = apply_suggestion(workspace, "demo", templates_dir=templates)

        assert out["applied"] is True
        assert json.loads(wf_file(workspace, "demo").read_text()) == TPL_V2
        assert manifest(workspace)["demo"]["provenance"] == "seed"
        assert list_suggestions(workspace, templates_dir=templates) == []
        report = refresh_seeds(workspace, templates_dir=templates)
        assert report.suggested == [] and report.refreshed == []

    def test_dismiss_tombstones_this_template_version_only(self, tmp_env):
        workspace, templates = tmp_env
        self._edited_with_pending(workspace, templates)

        dismiss_suggestion(workspace, "demo", templates_dir=templates)

        assert list_suggestions(workspace, templates_dir=templates) == []
        report = refresh_seeds(workspace, templates_dir=templates)
        assert report.suggested == []            # tombstoned version stays silent

        tpl_v3 = {"name": "demo", "nodes": [{"id": "a", "kind": "work", "prompt": "v3"}]}
        write_template(templates, "demo", tpl_v3)
        report = refresh_seeds(workspace, templates_dir=templates)
        assert report.suggested == ["demo"]      # a NEW version is a new decision

    def test_apply_unknown_name_reports_error(self, tmp_env):
        workspace, templates = tmp_env
        out = apply_suggestion(workspace, "nope", templates_dir=templates)
        assert out["applied"] is False


class TestNonSeedFilesUntouched:
    def test_user_workflows_and_dotfiles_are_ignored(self, tmp_env):
        workspace, templates = tmp_env
        write_template(templates, "demo", TPL_V1)
        mine = wf_file(workspace, "my-own-flow")
        mine.write_text('{"name": "my-own-flow", "nodes": []}')

        report = refresh_seeds(workspace, templates_dir=templates)

        assert mine.read_text() == '{"name": "my-own-flow", "nodes": []}'
        names = report.installed + report.refreshed + report.adopted + report.unknown
        assert "my-own-flow" not in names
        assert ".seeds" not in names
