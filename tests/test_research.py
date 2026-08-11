import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from butler.research import (
    ResearchCoordinator,
    _bounded_evidence_packet,
    _deterministic_query,
    _page_is_usable,
    _select_sources,
    _source_limit_for_request,
    is_web_research_request,
    select_research_mode,
)
from butler.tools import ToolResult


class ResearchTests(unittest.TestCase):
    def test_explicit_official_site_request_uses_research_route(self):
        self.assertTrue(
            is_web_research_request(
                "Найди на официальном сайте Python страницу релиза и дай прямую ссылку"
            )
        )
        self.assertFalse(
            is_web_research_request("Найди файл отчёта на компьютере")
        )

    def test_technical_request_gets_compact_english_search_query(self):
        self.assertEqual(
            _deterministic_query(
                "Найди на официальном сайте Python страницу релиза Python 3.12.10"
            ),
            "Python 3.12.10 official release",
        )

    def test_official_domain_is_prioritized(self):
        selected = _select_sources(
            [
                {
                    "results": [
                        {
                            "title": "Python 3.12.10 tutorial",
                            "url": "https://learnpython.example/python-3-12-10",
                        },
                        {
                            "title": "Python Release Python 3.12.10",
                            "url": "https://www.python.org/downloads/release/python-31210/",
                        },
                    ]
                }
            ],
            2,
            "Найди на официальном сайте Python релиз Python 3.12.10",
        )
        self.assertEqual(
            selected[0]["url"],
            "https://www.python.org/downloads/release/python-31210/",
        )

    def test_web_research_does_not_capture_local_or_external_actions(self):
        self.assertTrue(is_web_research_request("Найди последние новости о VR-интернете"))
        self.assertTrue(is_web_research_request("Сравни реальные цены в магазинах"))
        self.assertFalse(is_web_research_request("Найди файл конфигурации в проекте"))
        self.assertFalse(is_web_research_request("Отправь сообщение через интернет"))

    def test_mode_is_explicit_and_bounded(self):
        self.assertEqual(select_research_mode("Быстро найди новости").name, "fast")
        self.assertEqual(select_research_mode("Исследуй тщательно").name, "deep")
        self.assertLessEqual(select_research_mode("Исследуй тщательно").source_limit, 8)

    def test_official_page_lookup_is_small_but_price_research_is_not(self):
        normal = select_research_mode("обычный запрос")
        self.assertEqual(
            _source_limit_for_request(
                "Найди страницу релиза на официальном сайте и дай ссылку", normal
            ),
            3,
        )
        self.assertEqual(
            _source_limit_for_request(
                "Сравни цены товара в официальном магазине и у продавцов", normal
            ),
            normal.source_limit,
        )

    def test_evidence_limit_keeps_valid_json(self):
        packet, count = _bounded_evidence_packet(
            [
                {"url": "https://one.test", "text": "а" * 5000},
                {"url": "https://two.test", "text": "б" * 5000},
            ],
            4500,
        )
        parsed = __import__("json").loads(packet)
        self.assertEqual(len(parsed), count)
        self.assertLessEqual(len(packet), 4500)

    def test_source_selection_rejects_irrelevant_vr_and_verification_pages(self):
        selected = _select_sources(
            [
                {
                    "results": [
                        {"title": "Online banking", "url": "https://vr-bank.test"},
                        {
                            "title": "Meta Quest virtual reality update",
                            "url": "https://news.test/quest",
                        },
                        {
                            "title": "VR report",
                            "url": "https://example.test/verify-human",
                        },
                    ]
                }
            ],
            3,
            "Последние новости о VR-интернете",
        )
        self.assertEqual([item["url"] for item in selected], ["https://news.test/quest"])

    def test_unusable_page_is_not_treated_as_open_evidence(self):
        self.assertFalse(
            _page_is_usable(
                {"url": "https://site.test/verify-human", "text": "Access denied" * 50}
            )
        )
        self.assertTrue(_page_is_usable({"url": "https://site.test/a", "text": "факт " * 50}))

    @patch("butler.research.complete_chat")
    def test_research_uses_two_model_calls_and_parallel_evidence(self, complete_chat):
        complete_chat.side_effect = [
            {"choices": [{"message": {"content": '{"queries":["тестовый запрос"]}'}}]},
            {"choices": [{"message": {"content": "Подтверждённый итог."}}]},
        ]
        settings = SimpleNamespace(
            raw={
                "routing": {
                    "research_default_mode": "normal",
                    "research_evidence_max_chars": 20_000,
                },
                "diagnostics": {"enabled": False},
            }
        )

        def execute(name, arguments, confirmed=False):
            if name == "browser_search":
                return ToolResult(
                    True,
                    "ok",
                    "найдено",
                    {
                        "results": [
                            {"title": "Один", "url": "https://one.test/a"},
                            {"title": "Два", "url": "https://two.test/b"},
                        ]
                    },
                )
            return ToolResult(
                True,
                "ok",
                "прочитано",
                {
                    "title": arguments["url"],
                    "url": arguments["url"],
                    "text": "факт",
                    "retrieved_at": "2026-08-10T20:00:00Z",
                },
            )

        session = SimpleNamespace(
            tools=SimpleNamespace(execute=execute),
            record_exchange=Mock(),
        )
        statuses = []
        reply = ResearchCoordinator(settings).run(
            "Найди последние новости", session, on_status=statuses.append
        )

        self.assertEqual(reply.text, "Подтверждённый итог.")
        self.assertEqual(complete_chat.call_count, 2)
        final_messages = complete_chat.call_args_list[1].args[1]
        self.assertEqual([item["role"] for item in final_messages], ["system", "user"])
        self.assertEqual(len(reply.tool_events), 4)
        self.assertTrue(any("параллельно 2" in item for item in statuses))
        session.record_exchange.assert_called_once()


if __name__ == "__main__":
    unittest.main()
