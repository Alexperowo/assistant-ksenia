import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from butler.audio_routing import (
    execute_audio_output_command,
    find_matching_output_device,
    normalize_audio_command,
    notify_bluetooth_disconnected,
    parse_audio_output_command,
)
from butler.config import load_settings


class AudioRoutingTests(unittest.TestCase):
    def test_normalize_audio_command(self):
        self.assertEqual(
            normalize_audio_command("Ксения, звук на динамики"),
            "звук на динамики",
        )
        self.assertEqual(
            normalize_audio_command("Сеня звук на наушники пожалуйста"),
            "звук на наушники",
        )
        self.assertEqual(
            normalize_audio_command("  пожалуйста Ксения слушай переключи на колонки  "),
            "переключи на колонки",
        )

    def test_parse_speakers_command(self):
        cases = [
            "звук на динамики",
            "звук в динамики",
            "звук на колонки",
            "переключи на динамики",
            "переключи звук на динамики",
            "переведи звук на колонки",
            "выведи звук на динамики",
            "вывод на динамики",
            "Ксения, звук на динамики",
            "Ксения, переключи на динамики пожалуйста",
        ]
        for phrase in cases:
            with self.subTest(phrase=phrase):
                self.assertEqual(parse_audio_output_command(phrase), "speakers")

    def test_parse_headphones_command(self):
        cases = [
            "звук на наушники",
            "звук в наушники",
            "переключи на наушники",
            "переключи звук на наушники",
            "переведи звук на наушники",
            "выведи звук на наушники",
            "вывод на наушники",
            "включи наушники",
            "звук на гарнитуру",
            "звук в гарнитуру",
            "звук на jbl",
            "Ксения, звук на наушники",
            "Ксения, переключи на наушники пожалуйста",
        ]
        for phrase in cases:
            with self.subTest(phrase=phrase):
                self.assertEqual(parse_audio_output_command(phrase), "headphones")

    def test_parse_default_and_status_command(self):
        self.assertEqual(parse_audio_output_command("звук по умолчанию"), "default")
        self.assertEqual(parse_audio_output_command("сбрось звук"), "default")
        self.assertEqual(parse_audio_output_command("системный звук"), "default")
        self.assertEqual(parse_audio_output_command("где звук"), "status")
        self.assertEqual(parse_audio_output_command("куда идет звук"), "status")
        self.assertEqual(parse_audio_output_command("устройство вывода"), "status")

    def test_non_routing_phrases_return_none(self):
        cases = [
            "какой звук у динамика",
            "расскажи про наушники",
            "почему не работают динамики",
            "что такое jbl",
            "погода в москве",
            "ксения привет",
        ]
        for phrase in cases:
            with self.subTest(phrase=phrase):
                self.assertIsNone(parse_audio_output_command(phrase))

    def test_find_matching_output_device_speakers(self):
        devices = [
            {"name": "Display 1", "host_api": "Windows WASAPI"},
            {"name": "Speakers (Realtek HD Audio output)", "host_api": "Windows WASAPI"},
            {"name": "Speakers (Realtek HD Audio output)", "host_api": "MME"},
        ]
        result = find_matching_output_device(devices, "speakers")
        self.assertIsNotNone(result)
        selector, label = result
        self.assertEqual(selector, "Speakers")
        self.assertIn("динамики", label)

    def test_find_matching_output_device_jbl_tour(self):
        devices = [
            {"name": "Speakers (Realtek)", "host_api": "Windows WASAPI"},
            {"name": "(2- JBL Tour One M3)", "host_api": "Windows WASAPI"},
            {"name": "JBL Sense Pro", "host_api": "Windows DirectSound"},
        ]
        result = find_matching_output_device(devices, "headphones")
        self.assertIsNotNone(result)
        selector, label = result
        self.assertEqual(selector, "JBL Tour")
        self.assertIn("JBL Tour One M3", label)

    def test_find_matching_output_device_jbl_sense(self):
        devices = [
            {"name": "Speakers (Realtek)", "host_api": "Windows WASAPI"},
            {"name": "JBL Sense Pro", "host_api": "Windows WASAPI"},
        ]
        result = find_matching_output_device(devices, "headphones")
        self.assertIsNotNone(result)
        selector, label = result
        self.assertEqual(selector, "JBL Sense")
        self.assertIn("JBL Sense Pro", label)

    def test_find_matching_output_device_generic_headphones(self):
        devices = [
            {"name": "Speakers (Realtek)", "host_api": "Windows WASAPI"},
            {"name": "Sony WH-1000XM4 Headset", "host_api": "Windows WASAPI"},
        ]
        result = find_matching_output_device(devices, "headphones")
        self.assertIsNotNone(result)
        selector, label = result
        self.assertEqual(selector, "Headphones")
        self.assertIn("наушники", label)

    def test_execute_audio_output_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config").mkdir()
            default_config = {
                "assistant": {"name": "Ксения", "default_role": "ui_fast"},
                "paths": {"llama_server": "llama-server.exe", "models_dir": "models", "runtime_dir": "runtime"},
                "models": {"ui_fast": {"label": "Fast", "path": "fast.gguf", "context_size": 4096}},
                "voice": {"output_device": ""},
            }
            (root / "config" / "default.json").write_text(
                json.dumps(default_config), encoding="utf-8"
            )
            settings = load_settings(root)

            speech = MagicMock()
            speech.output_device = ""

            recognizer = MagicMock()
            recognizer.list_output_devices.return_value = [
                {"name": "Speakers (Realtek HD Audio)", "host_api": "Windows WASAPI"},
                {"name": "(2- JBL Tour One M3)", "host_api": "Windows WASAPI"},
            ]

            # 1. Switch to speakers
            reply = execute_audio_output_command("speakers", settings, speech, recognizer)
            self.assertIn("динамики", reply)
            speech.switch_output_device.assert_called_with("Speakers")
            user_json = json.loads((root / "config" / "user.json").read_text(encoding="utf-8"))
            self.assertEqual(user_json["voice"]["output_device"], "Speakers")

            # 2. Switch to headphones
            reply = execute_audio_output_command("headphones", settings, speech, recognizer)
            self.assertIn("наушники", reply)
            self.assertIn("JBL Tour One M3", reply)
            speech.switch_output_device.assert_called_with("JBL Tour")
            user_json = json.loads((root / "config" / "user.json").read_text(encoding="utf-8"))
            self.assertEqual(user_json["voice"]["output_device"], "JBL Tour")

            # 3. Switch to default
            reply = execute_audio_output_command("default", settings, speech, recognizer)
            self.assertIn("по умолчанию", reply)
            speech.switch_output_device.assert_called_with("")
            user_json = json.loads((root / "config" / "user.json").read_text(encoding="utf-8"))
            self.assertNotIn("output_device", user_json.get("voice", {}))

    def test_notify_bluetooth_disconnected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config").mkdir()
            default_config = {
                "assistant": {"name": "Ксения", "default_role": "ui_fast"},
                "paths": {"llama_server": "llama-server.exe", "models_dir": "models", "runtime_dir": "runtime"},
                "models": {"ui_fast": {"label": "Fast", "path": "fast.gguf", "context_size": 4096}},
                "voice": {"output_device": ""},
            }
            (root / "config" / "default.json").write_text(
                json.dumps(default_config), encoding="utf-8"
            )
            (root / "config" / "user.json").write_text(
                json.dumps({"voice": {"output_device": "JBL Tour"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            settings = load_settings(root=root)
            speech = MagicMock()

            message = notify_bluetooth_disconnected(settings, speech)
            self.assertIn("Связь с наушниками потеряна", message)
            self.assertIn("динамики компьютера", message)
            speech.switch_output_device.assert_called_with("")
            speech.say_and_wait.assert_called_with(message)
            user_json = json.loads((root / "config" / "user.json").read_text(encoding="utf-8"))
            self.assertNotIn("output_device", user_json.get("voice", {}))


if __name__ == "__main__":
    unittest.main()
