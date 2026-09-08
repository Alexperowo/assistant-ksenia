import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from butler.config import ConfigError, research_timeout_seconds
from butler.research import ResearchCoordinator, ResearchError, _ResearchBudget
from butler.tasking import DurableTaskStore, TaskCancelled, TaskControl
from butler.tools import ToolResult


class ResearchBudgetTests(unittest.TestCase):
    def settings(self, seconds=0.1, mode='normal'):
        return SimpleNamespace(raw={
            'routing': {'research_default_mode': mode, 'research_timeout_seconds': {mode: seconds}},
            'diagnostics': {'enabled': False},
        })

    def test_timeout_config_is_finite_typed_and_mode_specific(self):
        self.assertEqual(research_timeout_seconds({}, 'fast'), 60)
        self.assertEqual(research_timeout_seconds({'routing': {'research_timeout_seconds': {'normal': 42}}}, 'normal'), 42)
        for value in (False, '30', 0, -1, float('nan'), float('inf'), 3601, 10**1000):
            with self.subTest(value=repr(value)[:40]), self.assertRaises(ConfigError):
                research_timeout_seconds({'routing': {'research_timeout_seconds': {'fast': value}}}, 'fast')
        for configured in (None, [], {'typo': 3}):
            with self.assertRaises(ConfigError):
                research_timeout_seconds({'routing': {'research_timeout_seconds': configured}}, 'fast')

    def test_parallel_waits_share_one_budget_and_all_workers_finish(self):
        coordinator = ResearchCoordinator(self.settings())
        coordinator._queries = Mock(return_value=['one', 'two'])
        session = SimpleNamespace(tools=Mock(), record_exchange=Mock())
        running = []
        finished = []
        def execute(name, args, *, confirmed, checkpoint):
            running.append(args['query'])
            try:
                while True:
                    checkpoint()
                    threading.Event().wait(0.01)
            finally:
                finished.append(args['query'])
        session.tools.execute.side_effect = execute
        started = time.monotonic()
        with patch('butler.research.complete_chat') as llm:
            with self.assertRaises(ResearchError) as failure:
                coordinator.run('Найди новости', session)
            self.assertEqual(failure.exception.code, 'deadline_exceeded')
            llm.assert_not_called()
        self.assertLess(time.monotonic() - started, 1)
        self.assertCountEqual(running, ['one', 'two'])
        self.assertCountEqual(finished, running)
        session.record_exchange.assert_not_called()

    def test_query_planner_timeout_does_not_trigger_fallback_search(self):
        coordinator = ResearchCoordinator(self.settings())
        session = SimpleNamespace(tools=Mock(), record_exchange=Mock())
        def stalled_llm(*_args, checkpoint, **_kwargs):
            while True:
                checkpoint()
                threading.Event().wait(0.01)
        with patch('butler.research.complete_chat', side_effect=stalled_llm) as llm:
            with self.assertRaises(ResearchError) as failure:
                coordinator.run('Найди новости', session)
            self.assertEqual(failure.exception.code, 'deadline_exceeded')
            self.assertEqual(llm.call_count, 1)
        session.tools.execute.assert_not_called()
        session.record_exchange.assert_not_called()

    def test_stages_do_not_reset_budget_or_save_late_answer(self):
        coordinator = ResearchCoordinator(self.settings(10, 'deep'))
        coordinator._queries = Mock(return_value=['one'])
        session = SimpleNamespace(tools=Mock(), record_exchange=Mock())
        elapsed = [0.0]
        def execute(name, args, *, confirmed, checkpoint):
            checkpoint()
            elapsed[0] += 3
            if name == 'browser_search':
                return ToolResult(True, 'ok', 'ok', {'results': [{'url': 'https://news.test/a', 'title': 'one'}]})
            return ToolResult(True, 'ok', 'ok', {'text': 'Проверенный факт. ' * 30})
        session.tools.execute.side_effect = execute
        def llm(*_args, checkpoint, **_kwargs):
            checkpoint()
            elapsed[0] += 5
            return {'choices': [{'message': {'content': 'Late answer'}}]}
        with patch('butler.research.time.monotonic', side_effect=lambda: elapsed[0]), patch('butler.research.complete_chat', side_effect=llm) as complete:
            with self.assertRaises(ResearchError) as failure:
                coordinator.run('one', session)
            self.assertEqual(failure.exception.code, 'deadline_exceeded')
            self.assertEqual(complete.call_count, 1)  # No verification after deadline.
        session.record_exchange.assert_not_called()

    def test_pause_does_not_bypass_wall_clock_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DurableTaskStore(Path(directory))
            task = store.create('test', channel='console')
            store.request_pause(task.id)
            budget = _ResearchBudget(TaskControl(store, task.id), time.monotonic(), 0.05)
            started = time.monotonic()
            with self.assertRaises(ResearchError):
                budget.checkpoint()
            self.assertLess(time.monotonic() - started, 1)

    def test_explicit_task_cancel_takes_precedence_over_expired_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DurableTaskStore(Path(directory))
            task = store.create('test', channel='console')
            store.cancel(task.id)
            budget = _ResearchBudget(TaskControl(store, task.id), time.monotonic() - 10, 1)
            with self.assertRaises(TaskCancelled):
                budget.checkpoint()
