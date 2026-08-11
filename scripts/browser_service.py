from __future__ import annotations

import argparse
import asyncio
import json
import sys

from browser_worker import process_request


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Постоянный браузерный сервис Ксении")
    parser.add_argument("--executable", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--headless", choices=["true", "false"], default="true")
    parser.add_argument("--max-text", type=int, default=6000)
    parser.add_argument("--max-parallel", type=int, default=4)
    return parser.parse_args()


async def run() -> int:
    args = parse_args()
    from playwright.async_api import async_playwright

    write_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(max(1, min(8, args.max_parallel)))
    active: set[asyncio.Task] = set()

    async def send(payload: dict[str, object]) -> None:
        async with write_lock:
            print(json.dumps(payload, ensure_ascii=False), flush=True)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=args.profile,
            executable_path=args.executable,
            headless=args.headless == "true",
            args=["--disable-gpu", "--no-first-run", "--no-default-browser-check"],
            viewport={"width": 1440, "height": 900},
        )

        async def handle(command: dict[str, object]) -> None:
            request_id = str(command.get("id", ""))
            try:
                async with semaphore:
                    value = await process_request(
                        context,
                        str(command.get("mode", "")),
                        str(command.get("value", "")),
                        args.max_text,
                    )
                await send({"event": "result", "id": request_id, "ok": True, "value": value})
            except Exception as exc:
                await send(
                    {
                        "event": "result",
                        "id": request_id,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        try:
            await send(
                {
                    "event": "ready",
                    "max_parallel": max(1, min(8, args.max_parallel)),
                    "page_count": len(context.pages),
                }
            )
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    break
                try:
                    command = json.loads(line)
                except json.JSONDecodeError:
                    await send({"event": "protocol_error", "error": "Некорректный JSON"})
                    continue
                if not isinstance(command, dict):
                    continue
                if command.get("cmd") == "shutdown":
                    break
                task = asyncio.create_task(handle(command))
                active.add(task)
                task.add_done_callback(active.discard)
            if active:
                await asyncio.gather(*active, return_exceptions=True)
        finally:
            await context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
