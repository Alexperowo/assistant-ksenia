import sys
import socket
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from browser_worker import (  # noqa: E402
    _SearchRedirectHandler,
    extract_offers,
    normalize_search_result_url,
    parse_duckduckgo_results,
    public_http_url as worker_public_http_url,
    search_provider_url,
)


class BrowserOfferExtractionTests(unittest.TestCase):
    def test_worker_rejects_ambiguous_numeric_and_private_dns_hosts(self):
        self.assertFalse(worker_public_http_url("http://2130706433/"))
        self.assertFalse(worker_public_http_url("http://127.1/"))
        with patch(
            "browser_worker.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443))
            ],
        ):
            self.assertFalse(worker_public_http_url("https://example.test/"))

    def test_read_worker_disables_script_state_and_active_channels(self):
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "browser_worker.py"
        ).read_text(encoding="utf-8-sig")
        self.assertIn("java_script_enabled=False", source)
        self.assertIn('service_workers="block"', source)
        self.assertIn("route_web_socket", source)

    def test_search_fetch_accepts_only_fixed_https_providers(self):
        self.assertTrue(search_provider_url("https://html.duckduckgo.com/html/?q=test"))
        self.assertTrue(search_provider_url("https://www.bing.com/search?q=test"))
        self.assertFalse(search_provider_url("http://html.duckduckgo.com/html/?q=test"))
        self.assertFalse(search_provider_url("https://127.0.0.1/search"))
        self.assertFalse(search_provider_url("https://example.com/search"))

    def test_search_redirect_is_rejected_before_contacting_an_unknown_host(self):
        handler = _SearchRedirectHandler()
        request = Request("https://html.duckduckgo.com/html/?q=test")
        with self.assertRaises(HTTPError) as raised:
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://127.0.0.1/private",
            )
        self.assertEqual(raised.exception.code, 403)
        self.assertEqual(raised.exception.url, "https://127.0.0.1/private")

    def test_duckduckgo_redirect_is_decoded_and_private_target_rejected(self):
        self.assertEqual(
            normalize_search_result_url(
                "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2Fdownloads%2F"
            ),
            "https://www.python.org/downloads/",
        )
        self.assertEqual(
            normalize_search_result_url(
                "https://duckduckgo.com/l/?uddg=http%3A%2F%2F127.0.0.1%2Fsecret"
            ),
            "",
        )
        with patch(
            "browser_worker.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        ):
            self.assertEqual(
                normalize_search_result_url(
                    "https://evilduckduckgo.com/l/?uddg=https%3A%2F%2Fpython.org%2F"
                ),
                "https://evilduckduckgo.com/l/?uddg=https%3A%2F%2Fpython.org%2F",
            )

    def test_duckduckgo_html_result_is_parsed(self):
        results = parse_duckduckgo_results(
            """
            <div class="result results_links">
              <h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpython.org%2Frelease">
                Python <b>Release</b>
              </a></h2>
              <a class="result__snippet">Official <b>release</b> details.</a>
            </div>
            """
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://python.org/release")
        self.assertEqual(results[0]["title"], "Python Release")
        self.assertEqual(results[0]["description"], "Official release details.")

    def test_extracts_nested_product_offer(self):
        offers = extract_offers(
            [
                {
                    "@type": "Product",
                    "name": "Тестовый товар",
                    "offers": {
                        "@type": "Offer",
                        "price": "12345.00",
                        "priceCurrency": "RUB",
                        "availability": "https://schema.org/InStock",
                    },
                }
            ],
            "https://shop.example/product",
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["price"], "12345.00")
        self.assertEqual(offers[0]["currency"], "RUB")
        self.assertEqual(offers[0]["availability"], "InStock")

    def test_duplicate_offer_is_removed(self):
        offer = {"@type": "Offer", "price": "100", "priceCurrency": "RUB"}
        self.assertEqual(len(extract_offers([offer, offer], "https://shop.test")), 1)


if __name__ == "__main__":
    unittest.main()
