r"""Audit fixes for the demo: the target guard compares folders by file
identity, and the demo's config names its owner.

check_target used to compare resolved path text. On Windows Path.resolve()
keeps the \\?\ long-path prefix, a \\?\Volume{GUID}\ volume path and the
\\localhost\C$ admin-share spellings, so each of those reached the real
data\staging while comparing unequal to it — and a later --force deleted it.
Every test here runs against a stand-in project (never the real data/).
Each refusal and the owner table failed before its fix; the "passes" tests
guard against false refusals (the default demo/<combo>/ among them).
"""

import os
import tomllib
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm.demo import build as demo_build
from lifecrm.demo import persona
from lifecrm.demo.build import DemoBuilder, DemoError, check_dir_option, check_target

ANCHOR = date(2026, 10, 21)
runner = CliRunner()
windows = pytest.mark.skipif(os.name != "nt", reason="Windows path spellings")
SPELLINGS = ("long", "long_unc", "admin_share", "volume")


@pytest.fixture
def fake_project(tmp_path, monkeypatch) -> Path:
    """A stand-in project root, so nothing near the real data/ is probed."""
    proj = tmp_path / "LifeCRM"
    for d in ("data/staging", "data/transcripts", "backups"):
        (proj / d).mkdir(parents=True)
    (proj / "data" / "lifecrm.db").write_bytes(b"")
    monkeypatch.setattr(demo_build, "PROJECT_ROOT", proj)
    return proj


def _volume_name(drive: str) -> str | None:
    import ctypes
    buf = ctypes.create_unicode_buffer(64)
    ok = ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive + "\\", buf, 64)
    return buf.value if ok else None


def _raw_spelling(path: Path, kind: str) -> str:
    s = str(path)
    drive, rest = path.drive, s[len(path.drive):]
    if kind == "long":
        return "\\\\?\\" + s
    if len(drive) != 2 or drive[1] != ":":
        pytest.skip(f"{path} is not on a drive letter")
    if kind == "volume":
        vol = _volume_name(drive)
        if not vol:
            pytest.skip(f"no volume name for {drive}")
        return vol + rest.lstrip("\\")
    if kind == "admin_share":
        return "\\\\localhost\\" + drive[0] + "$" + rest
    return "\\\\?\\UNC\\localhost\\" + drive[0] + "$" + rest


def _spell(path: Path, kind: str) -> str:
    """`path` under another name that reaches the same folder, or a skip
    when this machine cannot build that spelling (no admin share, say)."""
    real = path
    while not real.exists():
        real = real.parent
    try:
        reaches = os.path.samefile(_raw_spelling(real, kind), real)
    except OSError:
        reaches = False
    if not reaches:
        pytest.skip(f"the {kind} spelling does not reach {real} on this machine")
    return _raw_spelling(path, kind)


# ---------------------------------------------------------------- spellings

@windows
@pytest.mark.parametrize("kind", SPELLINGS)
@pytest.mark.parametrize("inside", ["data/staging", "data/newdemo", "data",
                                    "backups/x", "backups"])
def test_another_spelling_of_a_protected_folder_is_refused(fake_project, kind, inside):
    with pytest.raises(DemoError, match="refusing"):
        check_target(Path(_spell(fake_project / inside, kind)))


@windows
@pytest.mark.parametrize("kind", SPELLINGS)
def test_another_spelling_of_the_project_root_or_a_folder_above_it_is_refused(
        fake_project, kind):
    with pytest.raises(DemoError, match="own data root"):
        check_target(Path(_spell(fake_project, kind)))
    with pytest.raises(DemoError, match="refusing"):
        check_target(Path(_spell(fake_project.parent, kind)))


@windows
@pytest.mark.parametrize("kind", SPELLINGS)
def test_a_protected_folder_not_created_yet_is_refused_under_any_spelling(
        fake_project, kind):
    (fake_project / "backups").rmdir()
    for inside in ("backups", "BACKUPS/x", "backups/demo/deeper"):
        with pytest.raises(DemoError, match="refusing"):
            check_target(Path(_spell(fake_project, kind) + "\\" + inside.replace("/", "\\")))


@windows
def test_a_folder_whose_data_is_the_real_data_is_refused_under_any_spelling(
        fake_project, tmp_path):
    import _winapi
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / demo_build.MARKER).write_text("lifecrm demo\n", encoding="utf-8")
    _winapi.CreateJunction(str(fake_project / "data"), str(elsewhere / "data"))
    with pytest.raises(DemoError, match="own data root"):
        check_target(Path(_spell(elsewhere, "long")))


@windows
@pytest.mark.parametrize("kind", SPELLINGS)
def test_force_never_deletes_a_protected_folder_reached_by_another_spelling(
        fake_project, kind):
    # An earlier build got its marker into the real staging folder; real
    # drafts were staged there since. --force must not delete them.
    staging = fake_project / "data" / "staging"
    (staging / demo_build.MARKER).write_text("lifecrm demo\n", encoding="utf-8")
    (staging / "draft-2026-10-22.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DemoError, match="refusing"):
        DemoBuilder(Path(_spell(staging, kind)), "android", ANCHOR,
                    echo=lambda *_: None, ask=False, force=True,
                    runner=lambda args: (0, "", "")).prepare()
    assert (staging / "draft-2026-10-22.json").exists()


@windows
def test_the_recheck_before_delete_compares_by_identity(fake_project, tmp_path):
    root = tmp_path / "demo"
    builder = DemoBuilder(root, "android", ANCHOR, echo=lambda *_: None,
                          ask=False, force=True, runner=lambda args: (0, "", ""))
    root.mkdir()
    (root / demo_build.MARKER).write_text("lifecrm demo\n", encoding="utf-8")
    (root / "draft-2026-10-20.json").write_text("{}", encoding="utf-8")
    # Between the check in __init__ and the delete, the real config started
    # naming the folder, spelled with the long-path prefix.
    (fake_project / "config.toml").write_text(
        f"[paths]\narchive_dir = '\\\\?\\{root}'\n", encoding="utf-8")
    with pytest.raises(DemoError, match="refusing"):
        builder.prepare()
    assert (root / "draft-2026-10-20.json").exists()


@windows
def test_an_unrelated_folder_passes_whatever_its_spelling(fake_project, tmp_path):
    # Identity, not text: no false refusals for a folder that merely sits
    # next to the protected ones, or for the default demo/<combo>/.
    for ok in (tmp_path / "elsewhere", fake_project / "data2",
               fake_project / "demo" / "android"):
        assert check_target(Path(_spell(ok, "long"))) is not None
        assert check_target(ok) == ok.resolve()


# ---------------------------------------------------------------- --dir as typed

@pytest.mark.parametrize("typed", [
    "\\\\?\\C:\\Users\\x\\LifeCRM\\data\\staging",
    "\\\\?\\UNC\\localhost\\C$\\Users\\x\\demo",
    "\\\\?\\Volume{76e676f3-381a-4a7e-8f22-5d700bb32335}\\demo",
    "\\\\localhost\\C$\\Users\\x\\demo",
    "\\\\127.0.0.1\\C$\\demo",
    "\\\\.\\C:\\demo",
])
def test_device_and_share_paths_are_refused_as_typed(typed):
    with pytest.raises(DemoError, match="plain folder path"):
        check_dir_option(typed)


@windows
def test_forward_slash_device_paths_are_refused_too():
    with pytest.raises(DemoError, match="plain folder path"):
        check_dir_option("//?/C:/Users/x/demo")


@pytest.mark.parametrize("typed", ["demo/x", "~/lifecrm-demo", "C:\\Users\\x\\demo",
                                   "/Users/x/demo"])
def test_plain_paths_pass_the_typed_check(typed):
    check_dir_option(typed)


def test_the_demo_command_refuses_a_device_path_in_one_line(fake_project, monkeypatch):
    staging = fake_project / "data" / "staging"
    (staging / "draft-2026-10-22.json").write_text("{}", encoding="utf-8")
    built = []

    class Builder:
        def __init__(self, *a, **kw):
            built.append(a)

        def run(self):
            return {}
    monkeypatch.setattr(demo_build, "DemoBuilder", Builder)
    r = runner.invoke(app, ["demo", "--combo", "android", "--no-ask",
                            "--dir", "\\\\?\\" + str(staging)])
    assert r.exit_code == 1, r.output
    lines = [ln for ln in r.output.splitlines() if ln.strip()]
    assert len(lines) == 1 and "plain folder path" in lines[0], r.output
    assert built == []
    assert sorted(p.name for p in staging.iterdir()) == ["draft-2026-10-22.json"]


# ---------------------------------------------------------------- the owner

@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_demo_config_names_its_owner(tmp_path, combo):
    owner = tomllib.loads(demo_build._config_text(tmp_path, combo))["owner"]
    me = persona.OWNER
    assert owner == {"name": me.name,
                     "handles": [f"email:{me.email}", f"phone:{me.phone}"]}
    for h in owner["handles"]:
        kind, _, value = h.partition(":")
        assert kind in ("email", "phone") and value


def test_the_owner_table_loads_with_the_rest_of_the_demo_config(tmp_path):
    (tmp_path / "config.toml").write_text(demo_build._config_text(tmp_path, "android"),
                                          encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.timezone == persona.TIMEZONE
    assert cfg.connector_settings("android_sms")["account"] == persona.OWNER.phone
