"""Выбор узла звука гарнитуры, когда она подключена сразу по обычному Bluetooth и по LE Audio."""
import pytest

voice_in = pytest.importorskip("voice_in")

SOURCES = ("1028\tbluez_input.88:92:CC:D8:81:0F\tPipeWire\tfloat32le 1ch 48000Hz\tRUNNING\n"
           "1207\tbluez_input.88_92_CC_D8_81_0F.0\tPipeWire\ts24-32le 1ch 32000Hz\tSUSPENDED\n"
           "1208\tbluez_output.88_92_CC_D8_81_0F.1.monitor\tPipeWire\ts24-32le 2ch 32000Hz\tSUSPENDED\n")
CARD = "bluez_card.88_92_CC_D8_81_0F"


@pytest.mark.parametrize("profile,want", [
    ("bap-duplex", "bluez_input.88_92_CC_D8_81_0F.0"),   # живой разговор — микрофон LE Audio, а не молчащий
    ("headset-head-unit", "bluez_input.88:92:CC:D8:81:0F"),
])
def test_dual_connected_headset_uses_active_profile_node(profile, want, monkeypatch):
    monkeypatch.setattr(voice_in, "sh", lambda *a: SOURCES)
    monkeypatch.setattr(voice_in, "card_profile", lambda card: profile)
    assert voice_in.bt_node(CARD, "sources") == want


def test_single_node_needs_no_profile(monkeypatch):
    monkeypatch.setattr(voice_in, "sh", lambda *a: SOURCES.splitlines()[1] + "\n")
    monkeypatch.setattr(voice_in, "card_profile", lambda card: pytest.fail("лишний вызов pactl"))
    assert voice_in.bt_node(CARD, "sources") == "bluez_input.88_92_CC_D8_81_0F.0"
