from app.core.config import settings
from app.engine.hints import HTTPQL_NOTE, scan_instruction


def test_the_httpql_note_follows_the_users_instruction_on_a_new_run_only(monkeypatch):
    monkeypatch.setattr(settings, "strix_httpql_hint", True)
    assert scan_instruction("Только /api", resumed=False) == f"Только /api\n\n{HTTPQL_NOTE}"
    assert scan_instruction(None, resumed=False) == HTTPQL_NOTE
    assert scan_instruction("  ", resumed=False) == HTTPQL_NOTE
    # a resumed run got the note when it started; a resume with nothing new to say stays without an instruction
    assert scan_instruction(None, resumed=True) is None
    assert scan_instruction("дальше", resumed=True) == "дальше"


def test_the_httpql_note_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "strix_httpql_hint", False)
    assert scan_instruction("x", resumed=False) == "x"
    assert scan_instruction(None, resumed=False) is None
