from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
import socket
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


_SEARCH_PROVIDER_HOSTS = frozenset(
    {"html.duckduckgo.com", "duckduckgo.com", "www.duckduckgo.com", "www.bing.com"}
)


def public_http_url(value: object) -> bool:
    raw = str(value or "")
    if not raw or "\\" in raw or any(ord(character) < 32 or ord(character) == 127 for character in raw):
        return False
    try:
        parsed = urlparse(raw)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False
    if parsed.username or parsed.password:
        return False
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return False
    try:
        legacy_ipv4 = ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(hostname)))
    except OSError:
        legacy_ipv4 = None
    if legacy_ipv4 is not None:
        return legacy_ipv4.is_global
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        return address.is_global
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    labels = ascii_hostname.split(".")
    if (
        len(ascii_hostname) > 253
        or any(not label or len(label) > 63 for label in labels)
        or any(label.startswith("-") or label.endswith("-") for label in labels)
    ):
        return False
    try:
        resolved = socket.getaddrinfo(
            ascii_hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except (OSError, UnicodeError):
        return False
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for item in resolved:
        try:
            addresses.append(ipaddress.ip_address(str(item[4][0]).split("%", 1)[0]))
        except (IndexError, TypeError, ValueError):
            return False
    return bool(addresses) and all(address.is_global for address in addresses)


def normalize_search_result_url(value: object, base_url: str = "https://duckduckgo.com/") -> str:
    """Resolve a public search-result URL without following an untrusted redirect."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    absolute = urljoin(base_url, raw)
    try:
        parsed = urlparse(absolute)
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold()
    if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            absolute = target
    return absolute if public_http_url(absolute) else ""


def search_provider_url(value: object) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").casefold() in _SEARCH_PROVIDER_HOSTS


class _SearchRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        if not search_provider_url(target):
            raise HTTPError(target, 403, "Небезопасное перенаправление поисковика.", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, target)


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.title_depth = 0
        self.snippet_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if self.title_depth:
            self.title_depth += 1
        elif tag == "a" and "result__a" in classes:
            self.current = {
                "title": "",
                "url": attributes.get("href", ""),
                "description": "",
            }
            self.results.append(self.current)
            self.title_depth = 1
        if self.snippet_depth:
            self.snippet_depth += 1
        elif self.current is not None and "result__snippet" in classes:
            self.snippet_depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self.title_depth:
            self.title_depth -= 1
        if self.snippet_depth:
            self.snippet_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        if self.title_depth:
            self.current["title"] += data
        if self.snippet_depth:
            self.current["description"] += data


def parse_duckduckgo_results(document: str) -> list[dict[str, str]]:
    parser = _DuckDuckGoParser()
    parser.feed(document)
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in parser.results:
        item_url = normalize_search_result_url(raw.get("url"))
        if not item_url or item_url in seen:
            continue
        seen.add(item_url)
        host = (urlparse(item_url).hostname or "").removeprefix("www.")
        results.append(
            {
                "title": " ".join(raw.get("title", "").split()),
                "url": item_url,
                "description": " ".join(raw.get("description", "").split()),
                "published": "",
                "source": host,
                "source_url": f"https://{host}/" if host else "",
            }
        )
        if len(results) >= 10:
            break
    return results


def _download_search_document(url: str) -> str:
    """Download only from the fixed search providers, with a strict size limit."""
    if not search_provider_url(url):
        raise ValueError("Неподдерживаемый поисковый провайдер.")
    request = Request(
        url,
        headers={
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
        },
    )
    opener = build_opener(_SearchRedirectHandler())
    with opener.open(request, timeout=25) as response:
        if not search_provider_url(response.geturl()):
            raise ValueError("Поисковик перенаправил запрос на неизвестный адрес.")
        payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("Ответ поисковика слишком большой.")
        charset = response.headers.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _parse_bing_rss(document: str) -> list[dict[str, str]]:
    root = ElementTree.fromstring(document)
    results: list[dict[str, str]] = []
    for item in root.findall(".//item")[:10]:
        def text_of(name: str) -> str:
            child = item.find(name)
            return "" if child is None or child.text is None else child.text.strip()

        item_url = normalize_search_result_url(text_of("link"), "https://www.bing.com/")
        if not item_url:
            continue
        results.append(
            {
                "title": text_of("title"),
                "url": item_url,
                "description": text_of("description"),
                "published": text_of("pubDate"),
                "source": "",
                "source_url": "",
            }
        )
    return results


def general_search(query: str, max_text: int) -> dict[str, object]:
    search_url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    results: list[dict[str, str]] = []
    errors: list[str] = []
    try:
        results = parse_duckduckgo_results(_download_search_document(search_url))
    except (OSError, ValueError, ElementTree.ParseError) as exc:
        errors.append(f"DuckDuckGo: {type(exc).__name__}")
    provider = "DuckDuckGo HTML"
    if not results:
        has_cyrillic = bool(re.search(r"[А-Яа-яЁё]", query))
        market = "ru-RU" if has_cyrillic else "en-US"
        country = "ru" if has_cyrillic else "us"
        bing_url = (
            "https://www.bing.com/search?format=rss"
            f"&mkt={market}&setlang={market}&cc={country}&q=" + quote_plus(query)
        )
        try:
            results = _parse_bing_rss(_download_search_document(bing_url))
            provider = "Bing RSS (резерв)"
            search_url = bing_url
        except (OSError, ValueError, ElementTree.ParseError) as exc:
            errors.append(f"Bing: {type(exc).__name__}")
    text = "\n\n".join(
        f"{item['title']}\n{item['description']}\n{item['url']}" for item in results
    )
    return {
        "url": search_url,
        "title": f"Результаты поиска: {query}",
        "text": text[:max_text],
        "content_selector": "search-results",
        "performed": [],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "search_provider": provider,
        "search_errors": errors,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ограниченный браузерный мост")
    parser.add_argument("--mode", choices=["search", "open", "interact"], required=True)
    value_group = parser.add_mutually_exclusive_group(required=True)
    value_group.add_argument("--value")
    value_group.add_argument("--value-stdin", action="store_true")
    parser.add_argument("--executable", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--headless", choices=["true", "false"], default="true")
    parser.add_argument("--max-text", type=int, default=6000)
    return parser.parse_args()


def extract_offers(documents: list[object], page_url: str) -> list[dict[str, object]]:
    """Extract schema.org Product/Offer data without trusting page instructions."""
    found: list[dict[str, object]] = []

    def visit(value: object, inherited_name: str = "") -> None:
        if isinstance(value, list):
            for item in value:
                visit(item, inherited_name)
            return
        if not isinstance(value, dict):
            return
        local_name = str(value.get("name", inherited_name) or inherited_name)
        raw_type = value.get("@type", "")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        lowered = {str(item).casefold() for item in types}
        if "product" in lowered:
            visit(value.get("offers", []), local_name)
        if lowered.intersection({"offer", "aggregateoffer"}):
            price = value.get("price") or value.get("lowPrice")
            high_price = value.get("highPrice")
            currency = value.get("priceCurrency", "")
            if price not in (None, ""):
                found.append(
                    {
                        "name": local_name,
                        "price": str(price),
                        "high_price": str(high_price or ""),
                        "currency": str(currency),
                        "availability": str(value.get("availability", "")).rsplit("/", 1)[-1],
                        "url": str(value.get("url") or page_url),
                        "source": "json-ld",
                    }
                )
        for key, child in value.items():
            if key not in {"offers"} and isinstance(child, (dict, list)):
                visit(child, local_name)

    for document in documents:
        visit(document)
    unique: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in found:
        signature = (
            str(item.get("name", "")),
            str(item.get("price", "")),
            str(item.get("currency", "")),
            str(item.get("url", "")),
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(item)
        if len(unique) >= 20:
            break
    return unique


async def process_request(context, mode: str, value: str, max_text: int) -> dict[str, object]:
    """Handle one isolated page inside a shared persistent browser context."""
    page = await context.new_page()
    try:
            async def guard_local_network(route) -> None:
                request_url = str(route.request.url or "")
                scheme = urlparse(request_url).scheme.casefold()
                if scheme in {"about", "blob", "data"} or public_http_url(request_url):
                    await route.continue_()
                else:
                    await route.abort("blockedbyclient")

            await page.route("**/*", guard_local_network)
            if hasattr(page, "route_web_socket"):
                await page.route_web_socket("**/*", lambda web_socket: web_socket.close())
            actions = []
            if mode == "search":
                # Bing otherwise inherits an arbitrary profile/geolocation locale.
                # In tests that made the abbreviation "VR" mean German banks
                # (Volksbanken Raiffeisenbanken), not virtual reality.
                has_cyrillic = bool(re.search(r"[А-Яа-яЁё]", value))
                news_query = bool(
                    re.search(r"\b(news|latest|breaking)\b|новост", value, flags=re.IGNORECASE)
                )
                if news_query:
                    search_value = value
                    fresh_query = bool(
                        re.search(
                            r"\b(latest|current|recent|today)\b|последн|свеж|сегодня",
                            value,
                            flags=re.IGNORECASE,
                        )
                    )
                    if fresh_query and "when:" not in value.casefold():
                        search_value += " when:30d"
                    language = "ru" if has_cyrillic else "en-US"
                    country = "RU" if has_cyrillic else "US"
                    edition = "RU:ru" if has_cyrillic else "US:en"
                    url = (
                        "https://news.google.com/rss/search?q="
                        + quote_plus(search_value)
                        + f"&hl={language}&gl={country}&ceid={edition}"
                    )
                    search_provider = "Google News RSS"
                else:
                    # DuckDuckGo's HTML endpoint preserves exact technical
                    # queries better than Bing RSS and exposes result metadata
                    # without running page scripts.
                    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(value)
                    search_provider = "DuckDuckGo HTML"
            elif mode == "open":
                if not public_http_url(value):
                    raise ValueError("Разрешены только публичные http:// и https:// адреса.")
                url = value
            elif mode == "interact":
                payload = json.loads(value)
                if not isinstance(payload, dict):
                    raise ValueError("Неверное описание действий браузера.")
                url = str(payload.get("url", ""))
                actions = payload.get("actions", [])
                if not public_http_url(url):
                    raise ValueError("Разрешены только публичные http:// и https:// адреса.")
                if not isinstance(actions, list) or not 1 <= len(actions) <= 10:
                    raise ValueError("Нужно от одного до десяти действий.")
            else:
                raise ValueError(f"Неизвестный режим браузера: {mode}")
            if mode == "search" and search_provider == "DuckDuckGo HTML":
                return await asyncio.to_thread(general_search, value, max_text)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if not public_http_url(page.url):
                raise ValueError("Перенаправление на локальный или непубличный адрес запрещено.")
            if mode in {"search", "open"}:
                # Many stores render listings after DOMContentLoaded. A short,
                # bounded grace period captures them without waiting forever for ads.
                await page.wait_for_timeout(1500)
            allowed_keys = {"Enter", "Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"}
            performed = []
            for action in actions:
                if not isinstance(action, dict):
                    raise ValueError("Каждое действие должно быть объектом.")
                kind = str(action.get("type", ""))
                if kind == "click":
                    selector = str(action.get("selector", ""))
                    await page.locator(selector).first.click(timeout=10000)
                elif kind == "click_text":
                    text = str(action.get("text", ""))
                    await page.get_by_text(text, exact=True).first.click(timeout=10000)
                elif kind == "fill":
                    selector = str(action.get("selector", ""))
                    await page.locator(selector).first.fill(str(action.get("text", "")), timeout=10000)
                elif kind == "press":
                    selector = str(action.get("selector", "body"))
                    key = str(action.get("key", ""))
                    if key not in allowed_keys:
                        raise ValueError(f"Клавиша браузера не разрешена: {key}")
                    await page.locator(selector).first.press(key, timeout=10000)
                elif kind == "wait":
                    milliseconds = min(5000, max(0, int(action.get("milliseconds", 500))))
                    await page.wait_for_timeout(milliseconds)
                else:
                    raise ValueError(f"Действие браузера не разрешено: {kind}")
                performed.append(kind)
            if actions:
                await page.wait_for_timeout(500)
            text = await page.locator("body").inner_text(timeout=15000)
            content_selector = "body"
            if mode in {"open", "interact"}:
                # Prefer article content over menus, cookie banners and sidebars.
                # This both improves evidence quality and reduces LLM prompt time.
                for selector in ("article", "main", '[role="main"]'):
                    locator = page.locator(selector)
                    candidates = []
                    for index in range(min(3, await locator.count())):
                        candidate = str(
                            await locator.nth(index).inner_text(timeout=5000) or ""
                        ).strip()
                        if len(candidate) >= 300:
                            candidates.append(candidate)
                    if candidates:
                        text = max(candidates, key=len)
                        content_selector = selector
                        break
            result = {
                "url": page.url,
                "title": await page.title(),
                "text": text[:max_text],
                "content_selector": content_selector,
                "performed": performed,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
            if mode in {"open", "interact"}:
                documents: list[object] = []
                scripts = page.locator('script[type="application/ld+json"]')
                for index in range(min(30, await scripts.count())):
                    raw = str(await scripts.nth(index).text_content() or "").strip()
                    if not raw or len(raw) > 1_000_000:
                        continue
                    try:
                        documents.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
                offers = extract_offers(documents, page.url)
                if not offers:
                    amount = await page.locator(
                        'meta[property="product:price:amount"], meta[itemprop="price"]'
                    ).first.get_attribute("content") if await page.locator(
                        'meta[property="product:price:amount"], meta[itemprop="price"]'
                    ).count() else ""
                    currency = await page.locator(
                        'meta[property="product:price:currency"], meta[itemprop="priceCurrency"]'
                    ).first.get_attribute("content") if await page.locator(
                        'meta[property="product:price:currency"], meta[itemprop="priceCurrency"]'
                    ).count() else ""
                    if amount:
                        offers = [{
                            "name": result["title"], "price": amount,
                            "high_price": "", "currency": currency or "",
                            "availability": "", "url": page.url, "source": "meta",
                        }]
                result["offers"] = offers
            if mode == "search":
                items = page.locator("item")
                result["results"] = []
                for index in range(min(10, await items.count())):
                    item = items.nth(index)

                    async def item_text(selector: str) -> str:
                        locator = item.locator(selector)
                        if not await locator.count():
                            return ""
                        return str(await locator.first.text_content() or "").strip()

                    result["results"].append(
                        {
                            "title": await item_text("title"),
                            "url": await item_text("link"),
                            "description": await item_text("description"),
                            "published": await item_text("pubDate"),
                            "source": await item_text("source"),
                            "source_url": str(
                                await item.locator("source").first.get_attribute("url") or ""
                            ) if await item.locator("source").count() else "",
                        }
                    )
                result["search_provider"] = search_provider
                if search_provider == "Google News RSS":
                    # Google News now wraps publisher URLs in an opaque
                    # batchexecute token. Resolve a bounded number in parallel
                    # so the reader can open the actual article, not an empty
                    # Google interstitial.
                    try:
                        from googlenewsdecoder import gnewsdecoder

                        decode_slots = asyncio.Semaphore(4)

                        async def decode_item(item: dict[str, object]) -> None:
                            async with decode_slots:
                                wrapped_url = str(item.get("url", ""))
                                decoded = await asyncio.to_thread(gnewsdecoder, wrapped_url)
                                if not isinstance(decoded, dict) or not decoded.get("status"):
                                    return
                                decoded_url = str(decoded.get("decoded_url", ""))
                                if decoded_url.startswith(("http://", "https://")):
                                    item["aggregator_url"] = wrapped_url
                                    item["url"] = decoded_url

                        await asyncio.gather(
                            *(decode_item(item) for item in result["results"][:8]),
                            return_exceptions=True,
                        )
                    except ImportError:
                        # The installer pins the decoder. Keeping wrapped links
                        # here makes an incomplete manual install fail softly.
                        pass
            return result
    finally:
        await page.close()


async def run() -> int:
    args = parse_args()
    value = sys.stdin.read() if args.value_stdin else str(args.value or "")
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        launch_arguments = ["--disable-gpu", "--no-first-run", "--no-default-browser-check"]
        if args.mode == "interact":
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=args.profile,
                executable_path=args.executable,
                headless=args.headless == "true",
                args=launch_arguments,
                viewport={"width": 1440, "height": 900},
                service_workers="block",
            )
            try:
                result = await process_request(context, args.mode, value, args.max_text)
            finally:
                await context.close()
        else:
            browser = await playwright.chromium.launch(
                executable_path=args.executable,
                headless=args.headless == "true",
                args=launch_arguments,
            )
            context = await browser.new_context(
                java_script_enabled=False,
                service_workers="block",
                viewport={"width": 1440, "height": 900},
            )
            try:
                result = await process_request(context, args.mode, value, args.max_text)
            finally:
                await context.close()
                await browser.close()
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
