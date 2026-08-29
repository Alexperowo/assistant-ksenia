import json
import unittest

from butler.turn_detection import (
    AdaptiveNoiseGate,
    HybridTurnDetector,
    StreamingTurnEndpoint,
    TurnDecision,
    VoskTranscriptStream,
)


class AdaptiveNoiseGateTests(unittest.TestCase):
    def test_immediate_speech_is_not_absorbed_into_noise_calibration(self):
        gate = AdaptiveNoiseGate(minimum_threshold=220, multiplier=2.2)

        self.assertTrue(gate.observe(500))
        self.assertEqual(gate.threshold, 220)
        self.assertEqual(gate.noise_floor, 0)

    def test_only_below_threshold_samples_adapt_the_noise_floor(self):
        gate = AdaptiveNoiseGate(minimum_threshold=220, multiplier=2.2)

        self.assertFalse(gate.observe(80))
        self.assertFalse(gate.observe(100))
        self.assertEqual(gate.noise_floor, 90)
        self.assertTrue(gate.observe(300))


class _FakeVoskRecognizer:
    def __init__(self, events):
        self._events = list(events)
        self._current = (False, "{}")

    def AcceptWaveform(self, pcm: bytes) -> bool:
        event = self._events.pop(0)
        if isinstance(event, Exception):
            raise event
        self._current = event
        return bool(event[0])

    def PartialResult(self) -> str:
        return self._current[1]

    def Result(self) -> str:
        return self._current[1]


class VoskTranscriptStreamTests(unittest.TestCase):
    def test_final_segments_and_partial_text_are_accumulated_in_order(self):
        recognizer = _FakeVoskRecognizer(
            [
                (False, json.dumps({"partial": "сравни"})),
                (True, json.dumps({"text": "сравни qwen"})),
                (False, json.dumps({"partial": "с ornith"})),
            ]
        )
        stream = VoskTranscriptStream(recognizer)

        first = stream.accept(b"one")
        finalized = stream.accept(b"two")
        latest = stream.accept(b"three")

        self.assertEqual(first.text, "сравни")
        self.assertTrue(first.changed)
        self.assertTrue(finalized.finalized)
        self.assertEqual(latest.text, "сравни qwen с ornith")

    def test_invalid_payload_or_recognizer_failure_preserves_last_good_text(self):
        recognizer = _FakeVoskRecognizer(
            [
                (False, json.dumps({"partial": "открой браузер"})),
                (False, "not-json"),
                RuntimeError("decoder failed"),
            ]
        )
        stream = VoskTranscriptStream(recognizer)

        good = stream.accept(b"one")
        malformed = stream.accept(b"two")
        failed = stream.accept(b"three")

        self.assertEqual(good.text, "открой браузер")
        self.assertEqual(malformed.text, good.text)
        self.assertFalse(malformed.changed)
        self.assertEqual(failed.text, good.text)
        self.assertFalse(failed.changed)


class StreamingTurnEndpointTests(unittest.TestCase):
    def test_incomplete_partial_extends_silence_and_repeated_text_is_not_reemitted(self):
        payload = json.dumps({"partial": "сравни qwen с"})
        endpoint = StreamingTurnEndpoint(
            _FakeVoskRecognizer([(False, payload), (False, payload), (False, payload)]),
            HybridTurnDetector(incomplete_silence_seconds=2.2),
        )

        voiced = endpoint.observe(b"voice", speech_active=True, at=20.0)
        paused = endpoint.observe(b"silence", speech_active=False, at=21.0)
        finished = endpoint.observe(b"silence", speech_active=False, at=22.21)

        self.assertTrue(voiced.transcript_changed)
        self.assertFalse(paused.transcript_changed)
        self.assertEqual(paused.decision, TurnDecision.KEEP_LISTENING)
        self.assertEqual(finished.decision, TurnDecision.END_TURN)


if __name__ == "__main__":
    unittest.main()
