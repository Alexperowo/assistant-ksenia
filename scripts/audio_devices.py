from __future__ import annotations

import argparse
import json

from audio_input import open_best_input_stream


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Инвентаризация аудиовходов Ксении")
    parser.add_argument(
        "--probe",
        metavar="NAME",
        help="проверить фактическое открытие микрофона по устойчивой части имени",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        import sounddevice as sd

        if args.probe:
            opened = open_best_input_stream(
                sd,
                str(args.probe),
                lambda *_args: None,
                target_rate=16000,
            )
            try:
                print(
                    json.dumps(
                        {
                            "event": "probe_ready",
                            "device": opened.device_name,
                            "host_api": opened.host_api,
                            "sample_rate": opened.sample_rate,
                            "candidate_count": opened.candidate_count,
                        },
                        ensure_ascii=False,
                    )
                )
            finally:
                opened.close()
            return 0

        default_input = int(sd.default.device[0]) if sd.default.device[0] is not None else -1
        devices = []
        for index, raw in enumerate(sd.query_devices()):
            info = dict(raw)
            if int(info.get("max_input_channels", 0)) < 1:
                continue
            host = dict(sd.query_hostapis(int(info.get("hostapi", -1)))).get("name", "")
            devices.append(
                {
                    "index": index,
                    "name": str(info.get("name", index)),
                    "host_api": str(host),
                    "sample_rate": int(round(float(info.get("default_samplerate", 0)))),
                    "default": index == default_input,
                }
            )
        print(json.dumps({"event": "devices", "devices": devices}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"event": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
