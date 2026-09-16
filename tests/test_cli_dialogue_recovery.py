import tempfile
import threading
import time
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from butler.agent import AgentReply
from butler.chat import ChatError
from butler.cli import _agent_chat, _voice_agent_active
from butler.config import ConfigError, load_settings
from butler.model_manager import ModelManagerError
from butler.orchestrator import RoutedAgentSession
from butler.research import ResearchError
from butler.tasking import DurableTaskStore, TaskState
from butler.user_messages import spoken_agent_error
from butler.wake import WakeListenerCancelled


class CliDialogueRecoveryTests(unittest.TestCase):
    """Run the real CLI loop, task journal and router lock with external I/O injected."""

    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        base = load_settings()
        raw = deepcopy(base.raw)
        raw["live"].update(enabled=False, speech_barge_in=False)
        raw["headset_controls"]["enabled"] = False
        raw["developer"]["workspace_dir"] = "workspace"
        self.settings = replace(base, root=root, runtime_dir=root / "runtime", raw=raw)
        self.speech = Mock()
        self.requests = []
        self.monitor_threads = []
        self.enterContext(patch("builtins.print"))

    def run_dialogue(self, error, *, voice):
        def route(text, **kwargs):
            self.requests.append(text)
            if len(self.requests) == 1:
                kwargs["on_status"]("Ищу источники")
                control = kwargs["control"]
                control.store.transition(
                    control.task_id,
                    TaskState.WAITING_CONFIRMATION,
                    "Ожидаю подтверждение",
                    confirmation={"tool": "browser_search", "message": "test"},
                )
                raise error
            return AgentReply("Следующий ответ получен.", ())

        self.enterContext(patch.object(RoutedAgentSession, "_ask_exclusive", side_effect=route))
        inputs = ["Найди новости", "Какое сегодня число?", "выход"]
        if not voice:
            self.enterContext(patch("builtins.input", side_effect=inputs))
            return _agent_chat(self.settings, self.speech)

        self.activation_wake_calls = 0

        def wake_event(*, cancel_event=None, **_kwargs):
            if cancel_event is None:
                self.activation_wake_calls += 1
                return {"event": "wake"}
            self.monitor_threads.append(threading.current_thread())
            deadline = time.monotonic() + 2
            while not cancel_event.is_set() and time.monotonic() < deadline:
                threading.Event().wait(0.005)
            raise WakeListenerCancelled("Test listener cancelled")

        self.enterContext(patch("butler.cli.AudioCaptureService"))
        self.enterContext(patch("butler.cli.ModelResidencyCoordinator"))
        listener = self.enterContext(patch("butler.cli.WakeListener"))
        listener.return_value.wait_event.side_effect = wake_event
        self.wake_listener = listener.return_value
        recognizer = self.enterContext(patch("butler.cli.SpeechRecognizer"))
        recognizer.return_value.prepare.return_value = {"device": "cpu", "engine": "test"}
        recognizer.return_value.listen_after_prompt.side_effect = [{"text": inputs[0]}]
        recognizer.return_value.listen_once.side_effect = [{"text": text} for text in inputs[1:]]
        return _voice_agent_active(self.settings, self.speech)

    def assert_recovers(self, error, *, voice):
        self.assertEqual(self.run_dialogue(error, voice=voice), 0)
        self.assertEqual(self.requests, ["Найди новости", "Какое сегодня число?"])
        tasks = {item["request"]: item for item in DurableTaskStore(self.settings.runtime_dir).list()}
        failed = tasks["Найди новости"]
        self.assertEqual(failed["state"], TaskState.FAILED)
        self.assertEqual(failed["error"], str(error))
        self.assertIsNone(failed["confirmation"])
        self.assertEqual(tasks["Какое сегодня число?"]["state"], TaskState.COMPLETED)
        self.assertTrue(all(not thread.is_alive() for thread in self.monitor_threads))
        if voice:
            self.assertEqual(self.activation_wake_calls, 1)
        # A failed task must not continue voicing stale progress behind the error.
        calls = self.speech.method_calls
        progress = next(i for i, call in enumerate(calls) if call.args == ("Ищу источники",))
        failure = next(i for i, call in enumerate(calls) if call.args == (spoken_agent_error(error),))
        self.assertTrue(any(call[0] == "stop" for call in calls[progress + 1:failure]))

    def test_console_continues_after_model_start_failure(self):
        self.assert_recovers(ModelManagerError("Model start failed"), voice=False)

    def test_console_continues_after_research_deadline(self):
        from butler.research import ResearchError
        self.assert_recovers(ResearchError("deadline_exceeded"), voice=False)

    def test_voice_continues_after_research_deadline(self):
        from butler.research import ResearchError
        self.assert_recovers(ResearchError("deadline_exceeded"), voice=True)

    def test_voice_continues_after_model_start_failure(self):
        self.assert_recovers(ModelManagerError("Model start failed"), voice=True)

    def test_console_clears_failed_chat_state_before_next_turn(self):
        self.assert_recovers(ChatError("Search unavailable"), voice=False)

    def test_voice_clears_failed_chat_state_before_next_turn(self):
        self.assert_recovers(ChatError("Search unavailable"), voice=True)

    def test_voice_continues_after_search_unavailable(self):
        self.assert_recovers(ResearchError("search_unavailable"), voice=True)

    def test_console_continues_after_search_permission_refusal(self):
        self.assert_recovers(ResearchError("confirmation_required"), voice=False)

    def test_console_does_not_hide_invalid_configuration(self):
        with self.assertRaises(ConfigError):
            self.run_dialogue(ConfigError("Invalid security configuration"), voice=False)
        self.assertEqual(self.requests, ["Найди новости"])

    def test_voice_does_not_hide_invalid_configuration(self):
        with self.assertRaises(ConfigError):
            self.run_dialogue(ConfigError("Invalid security configuration"), voice=True)
        self.assertEqual(self.requests, ["Найди новости"])
        self.assertTrue(all(not thread.is_alive() for thread in self.monitor_threads))


    def test_voice_multi_turn_dialogue_without_wake_word(self):
        def route(text, **kwargs):
            self.requests.append(text)
            return AgentReply(f"Ответ на {text}", ())

        self.enterContext(patch.object(RoutedAgentSession, "_ask_exclusive", side_effect=route))
        inputs = ["Привет", "Как дела?", "Какая погода?", "выход"]

        self.activation_wake_calls = 0

        def wake_event(*, cancel_event=None, **_kwargs):
            if cancel_event is None:
                self.activation_wake_calls += 1
                return {"event": "wake"}
            self.monitor_threads.append(threading.current_thread())
            deadline = time.monotonic() + 2
            while not cancel_event.is_set() and time.monotonic() < deadline:
                threading.Event().wait(0.005)
            raise WakeListenerCancelled("Test listener cancelled")

        self.enterContext(patch("butler.cli.AudioCaptureService"))
        self.enterContext(patch("butler.cli.ModelResidencyCoordinator"))
        listener = self.enterContext(patch("butler.cli.WakeListener"))
        listener.return_value.wait_event.side_effect = wake_event
        recognizer = self.enterContext(patch("butler.cli.SpeechRecognizer"))
        recognizer.return_value.prepare.return_value = {"device": "cpu", "engine": "test"}
        recognizer.return_value.listen_after_prompt.side_effect = [{"text": inputs[0]}]
        recognizer.return_value.listen_once.side_effect = [{"text": text} for text in inputs[1:]]

        self.assertEqual(_voice_agent_active(self.settings, self.speech), 0)
        self.assertEqual(self.requests, ["Привет", "Как дела?", "Какая погода?"])
        self.assertEqual(self.activation_wake_calls, 1)

    def test_voice_silence_timeout_pauses_dialogue_and_requires_wake_word(self):
        from butler.stt import SpeechRecognitionTimeout

        def route(text, **kwargs):
            self.requests.append(text)
            return AgentReply(f"Ответ на {text}", ())

        self.enterContext(patch.object(RoutedAgentSession, "_ask_exclusive", side_effect=route))

        self.activation_wake_calls = 0

        def wake_event(*, cancel_event=None, **_kwargs):
            if cancel_event is None:
                self.activation_wake_calls += 1
                return {"event": "wake"}
            self.monitor_threads.append(threading.current_thread())
            deadline = time.monotonic() + 2
            while not cancel_event.is_set() and time.monotonic() < deadline:
                threading.Event().wait(0.005)
            raise WakeListenerCancelled("Test listener cancelled")

        self.enterContext(patch("butler.cli.AudioCaptureService"))
        self.enterContext(patch("butler.cli.ModelResidencyCoordinator"))
        listener = self.enterContext(patch("butler.cli.WakeListener"))
        listener.return_value.wait_event.side_effect = wake_event
        recognizer = self.enterContext(patch("butler.cli.SpeechRecognizer"))
        recognizer.return_value.prepare.return_value = {"device": "cpu", "engine": "test"}
        recognizer.return_value.listen_after_prompt.side_effect = [
            {"text": "Первый вопрос"},
            {"text": "Второй вопрос"},
        ]
        recognizer.return_value.listen_once.side_effect = [
            SpeechRecognitionTimeout("silence"),
            {"text": "выход"},
        ]

        self.assertEqual(_voice_agent_active(self.settings, self.speech), 0)
        self.assertEqual(self.requests, ["Первый вопрос", "Второй вопрос"])
        self.assertEqual(self.activation_wake_calls, 2)

    def test_voice_explicit_end_dialogue_command_pauses_dialogue(self):
        def route(text, **kwargs):
            self.requests.append(text)
            return AgentReply(f"Ответ на {text}", ())

        self.enterContext(patch.object(RoutedAgentSession, "_ask_exclusive", side_effect=route))

        self.activation_wake_calls = 0

        def wake_event(*, cancel_event=None, **_kwargs):
            if cancel_event is None:
                self.activation_wake_calls += 1
                return {"event": "wake"}
            self.monitor_threads.append(threading.current_thread())
            deadline = time.monotonic() + 2
            while not cancel_event.is_set() and time.monotonic() < deadline:
                threading.Event().wait(0.005)
            raise WakeListenerCancelled("Test listener cancelled")

        self.enterContext(patch("butler.cli.AudioCaptureService"))
        self.enterContext(patch("butler.cli.ModelResidencyCoordinator"))
        listener = self.enterContext(patch("butler.cli.WakeListener"))
        listener.return_value.wait_event.side_effect = wake_event
        recognizer = self.enterContext(patch("butler.cli.SpeechRecognizer"))
        recognizer.return_value.prepare.return_value = {"device": "cpu", "engine": "test"}
        recognizer.return_value.listen_after_prompt.side_effect = [
            {"text": "Первый вопрос"},
            {"text": "Второй вопрос"},
        ]
        recognizer.return_value.listen_once.side_effect = [
            {"text": "закончи разговор"},
            {"text": "выход"},
        ]

        self.assertEqual(_voice_agent_active(self.settings, self.speech), 0)
        self.assertEqual(self.requests, ["Первый вопрос", "Второй вопрос"])
        self.assertEqual(self.activation_wake_calls, 2)
        self.assertTrue(any(call.args == ("Разговор завершён.",) for call in self.speech.say_and_wait.call_args_list))

    def test_voice_stop_speaking_command_keeps_dialogue_active(self):
        def route(text, **kwargs):
            self.requests.append(text)
            return AgentReply(f"Ответ на {text}", ())

        self.enterContext(patch.object(RoutedAgentSession, "_ask_exclusive", side_effect=route))

        self.activation_wake_calls = 0

        def wake_event(*, cancel_event=None, **_kwargs):
            if cancel_event is None:
                self.activation_wake_calls += 1
                return {"event": "wake"}
            self.monitor_threads.append(threading.current_thread())
            deadline = time.monotonic() + 2
            while not cancel_event.is_set() and time.monotonic() < deadline:
                threading.Event().wait(0.005)
            raise WakeListenerCancelled("Test listener cancelled")

        self.enterContext(patch("butler.cli.AudioCaptureService"))
        self.enterContext(patch("butler.cli.ModelResidencyCoordinator"))
        listener = self.enterContext(patch("butler.cli.WakeListener"))
        listener.return_value.wait_event.side_effect = wake_event
        recognizer = self.enterContext(patch("butler.cli.SpeechRecognizer"))
        recognizer.return_value.prepare.return_value = {"device": "cpu", "engine": "test"}
        recognizer.return_value.listen_after_prompt.side_effect = [
            {"text": "замолчи"},
        ]
        recognizer.return_value.listen_once.side_effect = [
            {"text": "Следующий вопрос"},
            {"text": "выход"},
        ]

        self.assertEqual(_voice_agent_active(self.settings, self.speech), 0)
        self.assertEqual(self.requests, ["Следующий вопрос"])
        self.assertEqual(self.activation_wake_calls, 1)

    def test_voice_continues_after_task_cancelled(self):
        from butler.tasking import TaskCancelled

        def route(text, **kwargs):
            self.requests.append(text)
            if len(self.requests) == 1:
                raise TaskCancelled("Cancelled by user")
            return AgentReply("Ответ после отмены", ())

        self.enterContext(patch.object(RoutedAgentSession, "_ask_exclusive", side_effect=route))

        self.activation_wake_calls = 0

        def wake_event(*, cancel_event=None, **_kwargs):
            if cancel_event is None:
                self.activation_wake_calls += 1
                return {"event": "wake"}
            self.monitor_threads.append(threading.current_thread())
            deadline = time.monotonic() + 2
            while not cancel_event.is_set() and time.monotonic() < deadline:
                threading.Event().wait(0.005)
            raise WakeListenerCancelled("Test listener cancelled")

        self.enterContext(patch("butler.cli.AudioCaptureService"))
        self.enterContext(patch("butler.cli.ModelResidencyCoordinator"))
        listener = self.enterContext(patch("butler.cli.WakeListener"))
        listener.return_value.wait_event.side_effect = wake_event
        recognizer = self.enterContext(patch("butler.cli.SpeechRecognizer"))
        recognizer.return_value.prepare.return_value = {"device": "cpu", "engine": "test"}
        recognizer.return_value.listen_after_prompt.side_effect = [
            {"text": "Отменимая задача"},
        ]
        recognizer.return_value.listen_once.side_effect = [
            {"text": "Новая задача"},
            {"text": "выход"},
        ]

        self.assertEqual(_voice_agent_active(self.settings, self.speech), 0)
        self.assertEqual(self.requests, ["Отменимая задача", "Новая задача"])
        self.assertEqual(self.activation_wake_calls, 1)


if __name__ == "__main__":
    unittest.main()
