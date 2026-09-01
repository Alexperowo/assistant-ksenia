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


def _default_device_index(sd, position: int) -> int:
    try:
        index = int(sd.default.device[position])
    except (AttributeError, IndexError, TypeError, ValueError, sd.PortAudioError):
        return -1
    return index if index >= 0 else -1


def audio_inventory(sd) -> dict[str, list[dict[str, object]]]:
    default_input = _default_device_index(sd, 0)
    default_output = _default_device_index(sd, 1)
    inputs: list[dict[str, object]] = []
    outputs: list[dict[str, object]] = []
    for index, raw in enumerate(sd.query_devices()):
        info = dict(raw)
        try:
            host = dict(sd.query_hostapis(int(info.get("hostapi", -1)))).get(
                "name", ""
            )
        except (TypeError, ValueError, sd.PortAudioError):
            host = ""
        common = {
            "index": index,
            "name": str(info.get("name", index)),
            "host_api": str(host),
            "sample_rate": int(round(float(info.get("default_samplerate", 0)))),
        }
        input_channels = int(info.get("max_input_channels", 0))
        output_channels = int(info.get("max_output_channels", 0))
        if input_channels > 0:
            inputs.append(
                common
                | {
                    "channels": input_channels,
                    "default": index == default_input,
                }
            )
        if output_channels > 0:
            outputs.append(
                common
                | {
                    "channels": output_channels,
                    "default": index == default_output,
                }
            )
    return {"inputs": inputs, "outputs": outputs}


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

        inventory = audio_inventory(sd)
        print(
            json.dumps(
                {
                    "event": "devices",
                    "devices": inventory["inputs"],
                    "inputs": inventory["inputs"],
                    "outputs": inventory["outputs"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"event": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
