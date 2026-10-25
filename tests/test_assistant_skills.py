"""The /morning skill: one file, mirrored byte for byte, that names the
commands it runs, saves the pack before checking against it, writes by key,
marks a kept brief as derived, and never applies without confirmation."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLAUDE = ROOT / ".claude" / "skills" / "morning" / "SKILL.md"
AGENTS = ROOT / ".agents" / "skills" / "morning" / "SKILL.md"


def test_the_morning_skill_is_mirrored_byte_for_byte():
    assert CLAUDE.read_bytes() == AGENTS.read_bytes()


def test_the_morning_skill_names_its_contract():
    text = CLAUDE.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text.startswith("---\nname: morning\n")
    assert "python -m lifecrm nudges --json --out <pack file>" in text
    assert "python -m lifecrm nudges --check <brief file> --pack <pack file>" in text
    assert "never against a rebuild" in text
    assert "nudges snooze <key> --days 7" in text and "nudges dismiss <key>" in text
    assert '"derived_from": "lifecrm morning"' in text
    assert "**Never apply without it.**" in text
    assert "Never diagnose" in text and "not what happened" in text
    assert "python -m lifecrm import <brief file> --type manual --date <today> --json" in text
