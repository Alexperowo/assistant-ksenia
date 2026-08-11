from __future__ import annotations

import json


def main() -> int:
    try:
        import sounddevice as sd

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
