"""The demo scripts open the demo's Obsidian vault with -Gui obsidian /
--gui obsidian, run against a fake repo whose `lifecrm` is a stub that
records its arguments (test_fix_ops_scripts' harness) — nothing is built,
served or opened."""

from test_fix_ops_scripts import (_demo_ps, _demo_sh, _fake_repo, _ps_fake_repo,
                                  _records, _same)
from test_failure_visibility import needs_powershell
from test_mac_scripts import needs_bash


def _built(repo, combo):
    marker = repo / "demo" / combo / ".lifecrm-demo"
    marker.parent.mkdir(parents=True)
    marker.write_text("lifecrm demo\n", encoding="utf-8")


@needs_powershell
def test_demo_ps1_opens_the_vault_with_gui_obsidian(tmp_path):
    repo = _ps_fake_repo(tmp_path)
    _built(repo, "windows-iphone")                  # this branch's default demo
    record = tmp_path / "calls.jsonl"
    done = _demo_ps(repo, tmp_path, record, "-Gui", "obsidian")
    assert done.returncode == 0, done.stdout + done.stderr
    (call,) = _records(record)
    assert call["argv"][0] == "--root" and \
        _same(call["argv"][1], repo / "demo" / "windows-iphone")
    assert call["argv"][2:] == ["obsidian", "open"]


@needs_powershell
def test_demo_ps1_refuses_another_gui(tmp_path):
    repo = _ps_fake_repo(tmp_path)
    record = tmp_path / "calls.jsonl"
    done = _demo_ps(repo, tmp_path, record, "-Gui", "tui")
    assert done.returncode != 0 and _records(record) == []


@needs_bash
def test_demo_sh_opens_the_vault_with_gui_obsidian(tmp_path):
    repo = _fake_repo(tmp_path, "demo.sh")
    _built(repo, "iphone")
    record = tmp_path / "calls.jsonl"
    done = _demo_sh(repo, tmp_path, record, "--gui", "obsidian")
    assert done.returncode == 0, done.stdout + done.stderr
    (call,) = _records(record)
    assert call["argv"][2:] == ["obsidian", "open"]
    assert _same(call["argv"][1], repo / "demo" / "iphone")


@needs_bash
def test_demo_sh_refuses_another_gui(tmp_path):
    repo = _fake_repo(tmp_path, "demo.sh")
    record = tmp_path / "calls.jsonl"
    done = _demo_sh(repo, tmp_path, record, "--gui", "tui")
    assert done.returncode == 2 and "--gui must be web or obsidian" in done.stderr
    assert _records(record) == []
