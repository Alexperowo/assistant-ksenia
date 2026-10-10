import sys, asyncio, json; sys.path.insert(0, "/home/user/assistant-ksenia/voice-in"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
import conftest, voice_in
state = {"profile": "headset-head-unit"}   # left in HFP: voice-in was restarted (SIGTERM -> os._exit) mid-/listen
calls = []
voice_in.find_bt_card = lambda: "bluez_card.88_92_CC_00_11_22"
voice_in.card_profile = lambda c: state["profile"]
voice_in.card_profiles = lambda c: ["off", "a2dp-sink-ldac", "a2dp-sink", "headset-head-unit"]
voice_in.bt_node = lambda c, k: "bluez_output.88:92:CC:00:11:22.1"
voice_in.find_source = lambda c: "bluez_input.88:92:CC:00:11:22.0"
async def set_profile(card, prof): calls.append(prof); state["profile"] = prof
voice_in.set_profile = set_profile
class Ear:
    lock = asyncio.Lock(); streaming = False; vp = None
    async def record_utterance(self, *a, **k): return None, {"reason": "no_speech"}
voice_in.ear = Ear()
class Req:
    transport = None
async def main():
    for i in range(3):
        r = await voice_in.handle_listen(Req())
        print(f"/listen #{i+1}: set-card-profile calls={calls} -> card profile now {state['profile']!r}")
asyncio.run(main())
