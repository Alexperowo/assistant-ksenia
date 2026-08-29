import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from voice_worker import PlaybackController, split_tts_text  # noqa: E402


class VoiceWorkerTests(unittest.TestCase):
    def test_long_text_is_split_without_losing_words(self):
        text = (
            "Первое достаточно длинное предложение для проверки. " * 12
            + "Последняя часть ответа должна обязательно прозвучать."
        )
        chunks = split_tts_text(text, max_chars=140)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 140 for chunk in chunks))
        self.assertEqual(" ".join(chunks), " ".join(text.split()))
        self.assertTrue(chunks[-1].endswith("прозвучать."))

    def test_stop_cannot_miss_player_during_process_start_race(self):
        popen_started = threading.Event()
        allow_popen = threading.Event()
        terminated = threading.Event()

        class FakePlayer:
            stderr = None

            def poll(self):
                return None if not terminated.is_set() else -15

            def terminate(self):
                terminated.set()

            def wait(self):
                terminated.wait(0.5)
                return -15 if terminated.is_set() else 0

        def start_player(*_args, **_kwargs):
            popen_started.set()
            allow_popen.wait(1)
            return FakePlayer()

        controller = PlaybackController()
        generation = controller.generation()
        with patch("voice_worker.subprocess.Popen", side_effect=start_player):
            playback = threading.Thread(
                target=controller.play,
                args=(Path("speech.wav"), generation),
            )
            playback.start()
            self.assertTrue(popen_started.wait(1))
            stopping = threading.Thread(target=controller.stop)
            stopping.start()
            time.sleep(0.05)
            allow_popen.set()
            stopping.join(1)
            playback.join(1)

        self.assertTrue(terminated.is_set())
        self.assertFalse(stopping.is_alive())
        self.assertFalse(playback.is_alive())


if __name__ == "__main__":
    unittest.main()
