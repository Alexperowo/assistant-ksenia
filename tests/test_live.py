import threading
import unittest

from butler.live import (
    ConversationCoordinator,
    HybridTurnDetector,
    LiveInterrupted,
    LivePhase,
    TurnDecision,
)
from butler.speech import SpeechCompletion


class _TrackedSpeech:
    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.requests: list[tuple[str, object]] = []
        self.stop_calls = 0
        self.on_stop = None

    def say_tracked(self, text, on_complete) -> bool:
        if not self.accept:
            return False
        self.requests.append((text, on_complete))
        return True

    def complete(self, index: int, *, ok: bool = True, cancelled: bool = False) -> None:
        text, callback = self.requests[index]
        callback(
            SpeechCompletion(
                request_id=str(index + 1),
                original_text=text,
                spoken_text=text,
                ok=ok,
                cancelled=cancelled,
                engine="test",
            )
        )

    def stop(self) -> None:
        self.stop_calls += 1
        if self.on_stop is not None:
            self.on_stop()


class _BlockingTrackedSpeech(_TrackedSpeech):
    def __init__(self) -> None:
        super().__init__()
        self.submission_started = threading.Event()
        self.allow_submission = threading.Event()
        self.events: list[str] = []

    def say_tracked(self, text, on_complete) -> bool:
        self.submission_started.set()
        self.allow_submission.wait(2)
        self.events.append("submitted")
        return super().say_tracked(text, on_complete)

    def stop(self) -> None:
        self.events.append("stopped")
        super().stop()


class LiveConversationTests(unittest.TestCase):
    def _coordinator(self, speech: _TrackedSpeech) -> ConversationCoordinator:
        return ConversationCoordinator(
            speech,
            minimum_phrase_chars=1,
            maximum_phrase_chars=80,
        )

    def test_generated_text_is_not_spoken_until_playback_completes(self):
        speech = _TrackedSpeech()
        live = self._coordinator(speech)
        token = live.begin_response("Расскажи кратко")

        self.assertTrue(live.accept_delta(token, "Первая фраза. Вторая фраза."))
        self.assertTrue(live.finish_response(token))
        before = live.snapshot()
        self.assertEqual(before.generated_text, "Первая фраза. Вторая фраза.")
        self.assertEqual(before.spoken_text, "")
        self.assertEqual(before.queued_segments, 2)
        self.assertEqual(before.phase, LivePhase.SPEAKING)

        speech.complete(0)
        middle = live.snapshot()
        self.assertEqual(middle.spoken_text, "Первая фраза.")
        self.assertEqual(middle.phase, LivePhase.SPEAKING)

        speech.complete(1)
        after = live.snapshot()
        self.assertEqual(after.spoken_text, "Первая фраза. Вторая фраза.")
        self.assertEqual(after.phase, LivePhase.LISTENING)
        self.assertTrue(live.session.wait_until_settled(token.turn_id, timeout=0.01))

    def test_barge_in_cancels_generation_before_stopping_audio(self):
        speech = _TrackedSpeech()
        live = self._coordinator(speech)
        token = live.begin_response("Продолжай")
        live.accept_delta(token, "Первая. Вторая. Третья.")
        speech.complete(0)
        speech.on_stop = lambda: self.assertTrue(token.cancel_event.is_set())

        interrupted = live.interrupt("human_speech")

        self.assertEqual(speech.stop_calls, 1)
        self.assertTrue(interrupted.interrupted)
        self.assertEqual(interrupted.phase, LivePhase.INTERRUPTED)
        self.assertEqual(interrupted.spoken_text, "Первая.")
        self.assertEqual(interrupted.cancelled_segments, 2)
        with self.assertRaises(LiveInterrupted):
            token.checkpoint()

        # A late worker callback must never make cancelled text part of memory.
        speech.complete(1)
        self.assertEqual(live.snapshot().spoken_text, "Первая.")

    def test_out_of_order_completion_commits_only_contiguous_prefix(self):
        speech = _TrackedSpeech()
        live = self._coordinator(speech)
        token = live.begin_response("Проверка")
        live.accept_delta(token, "Один. Два.")
        live.finish_response(token)

        speech.complete(1)
        self.assertEqual(live.snapshot().spoken_text, "")
        speech.complete(0)
        self.assertEqual(live.snapshot().spoken_text, "Один. Два.")

    def test_rejected_audio_is_failed_and_never_committed(self):
        speech = _TrackedSpeech(accept=False)
        live = self._coordinator(speech)
        token = live.begin_response("Проверка")
        live.accept_delta(token, "Ответ.")
        live.finish_response(token)

        snapshot = live.snapshot()
        self.assertEqual(snapshot.generated_text, "Ответ.")
        self.assertEqual(snapshot.spoken_text, "")
        self.assertEqual(snapshot.failed_segments, 1)
        self.assertEqual(snapshot.phase, LivePhase.INTERRUPTED)
        self.assertEqual(snapshot.interruption_reason, "tts_rejected")
        self.assertEqual(speech.stop_calls, 1)

    def test_async_audio_failure_cancels_later_segments(self):
        speech = _TrackedSpeech()
        live = self._coordinator(speech)
        token = live.begin_response("Проверка")
        live.accept_delta(token, "Первая. Вторая.")
        live.finish_response(token)

        speech.complete(0, ok=False)
        failed = live.snapshot()

        self.assertEqual(failed.failed_segments, 1)
        self.assertEqual(failed.cancelled_segments, 1)
        self.assertEqual(failed.spoken_text, "")
        self.assertEqual(failed.interruption_reason, "tts_failure")
        self.assertEqual(speech.stop_calls, 1)
        speech.complete(1)
        self.assertEqual(live.snapshot().spoken_text, "")

    def test_stale_completion_cannot_modify_a_new_turn(self):
        speech = _TrackedSpeech()
        live = self._coordinator(speech)
        first = live.begin_response("Первый вопрос")
        live.accept_delta(first, "Старый ответ.")
        live.interrupt()

        second = live.begin_response("Второй вопрос")
        live.accept_delta(second, "Новый ответ.")
        live.finish_response(second)
        speech.complete(0)
        self.assertEqual(live.snapshot().spoken_text, "")
        speech.complete(1)
        self.assertEqual(live.snapshot().spoken_text, "Новый ответ.")

    def test_interrupt_cannot_stop_before_inflight_audio_submission_finishes(self):
        speech = _BlockingTrackedSpeech()
        live = self._coordinator(speech)
        token = live.begin_response("Проверка гонки")
        feed_finished = threading.Event()
        interrupt_finished = threading.Event()

        def feed() -> None:
            live.accept_delta(token, "Готовая фраза.")
            feed_finished.set()

        def interrupt() -> None:
            live.interrupt("concurrent_barge_in")
            interrupt_finished.set()

        feed_thread = threading.Thread(target=feed)
        feed_thread.start()
        self.assertTrue(speech.submission_started.wait(1))
        interrupt_thread = threading.Thread(target=interrupt)
        interrupt_thread.start()
        self.assertFalse(interrupt_finished.wait(0.05))

        speech.allow_submission.set()
        feed_thread.join(1)
        interrupt_thread.join(1)

        self.assertTrue(feed_finished.is_set())
        self.assertTrue(interrupt_finished.is_set())
        self.assertEqual(speech.events, ["submitted", "stopped"])


class HybridTurnDetectorTests(unittest.TestCase):
    def test_complete_command_uses_short_silence(self):
        detector = HybridTurnDetector()
        detector.observe("Открой браузер.", speech_active=True, at=10.0)

        self.assertEqual(
            detector.observe("Открой браузер.", speech_active=False, at=10.44),
            TurnDecision.KEEP_LISTENING,
        )
        self.assertEqual(
            detector.observe("Открой браузер.", speech_active=False, at=10.46),
            TurnDecision.END_TURN,
        )

    def test_incomplete_thought_and_hesitation_wait_longer(self):
        detector = HybridTurnDetector()
        detector.observe("Сравни Qwen с", speech_active=True, at=20.0)
        self.assertEqual(
            detector.observe("Сравни Qwen с", speech_active=False, at=21.0),
            TurnDecision.KEEP_LISTENING,
        )
        self.assertEqual(
            detector.observe("Сравни Qwen с", speech_active=False, at=22.21),
            TurnDecision.END_TURN,
        )
        self.assertEqual(detector.required_silence("Эээ… секунду…"), 2.2)

    def test_resumed_speech_resets_silence_clock(self):
        detector = HybridTurnDetector()
        detector.observe("Найди модель", speech_active=True, at=1.0)
        detector.observe("Найди модель", speech_active=False, at=1.7)
        detector.observe("Найди модель Qwen", speech_active=True, at=1.8)

        self.assertEqual(
            detector.observe("Найди модель Qwen", speech_active=False, at=2.4),
            TurnDecision.KEEP_LISTENING,
        )
        self.assertEqual(
            detector.observe("Найди модель Qwen", speech_active=False, at=2.66),
            TurnDecision.END_TURN,
        )

    def test_blank_input_never_ends_and_invalid_thresholds_fail(self):
        detector = HybridTurnDetector()
        self.assertEqual(
            detector.observe("", speech_active=False, at=100.0),
            TurnDecision.KEEP_LISTENING,
        )
        with self.assertRaises(ValueError):
            HybridTurnDetector(
                complete_silence_seconds=1.0,
                ordinary_silence_seconds=0.5,
            )


if __name__ == "__main__":
    unittest.main()
