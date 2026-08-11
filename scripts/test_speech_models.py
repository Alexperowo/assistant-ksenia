from __future__ import annotations

import argparse
import json
import re
import tempfile
import warnings
import wave
from pathlib import Path


ROUNDTRIP_LANDMARKS = (
    "ксения",
    "александр",
    "августа",
    "первый",
    "второй",
    "третий",
    "горизонт",
)


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"[a-zа-яё0-9]+", str(text).casefold().replace("ё", "е")))


def evaluate_roundtrip(
    synthesized_text: str,
    recognized: str,
    *,
    chunk_count: int,
    audio_duration_seconds: float,
    language_probability: float,
) -> dict[str, object]:
    """Evaluate the beginning, middle and end instead of accepting a short tail."""
    expected = tuple(word.replace("ё", "е") for word in ROUNDTRIP_LANDMARKS)
    recognized_words = _word_set(recognized)
    matched = [word for word in expected if word in recognized_words]
    missing = [word for word in expected if word not in recognized_words]
    minimum_chunks = 2 if len(synthesized_text) > 260 else 1
    minimum_duration = max(2.0, len(synthesized_text) * 0.035)
    digits_removed = re.search(r"\d", synthesized_text) is None
    result = {
        "ok": (
            not missing
            and digits_removed
            and chunk_count >= minimum_chunks
            and audio_duration_seconds >= minimum_duration
            and language_probability >= 0.80
        ),
        "matched_words": matched,
        "missing_words": missing,
        "landmark_recall": round(len(matched) / len(expected), 3),
        "digits_removed_before_tts": digits_removed,
        "minimum_chunk_count": minimum_chunks,
        "minimum_audio_duration_seconds": round(minimum_duration, 2),
    }
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Проверка TTS и STT без микрофона")
    parser.add_argument("--whisper-model", type=Path)
    parser.add_argument("--tts-model", default="v5_ru")
    parser.add_argument("--speaker", default="xenia")
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--compute-type", default="int8_float16")
    parser.add_argument(
        "--text",
        default=(
            "Ксения слушай. Александр проверяет длинную русскую речь. Сегодня 10 августа 2026 года. "
            "Первый фрагмент должен прозвучать полностью и разборчиво. Второй фрагмент проверяет, "
            "что голосовой движок не теряет середину большого ответа. Третий фрагмент подтверждает, "
            "что очередь воспроизведения сохраняет правильный порядок. Последнее контрольное слово: горизонт."
        ),
    )
    return parser.parse_args()


def write_wav(path: Path, audio, sample_rate: int, torch) -> None:
    values = audio.detach().cpu().clamp(-1, 1)
    values = (values * 32767).to(dtype=torch.int16).numpy()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(values.tobytes())


def main() -> int:
    args = parse_args()
    warnings.filterwarnings("ignore", message="TypedStorage is deprecated.*")
    if args.whisper_model is None:
        from butler.config import load_settings

        settings = load_settings()
        voice = settings.raw.get("voice", {})
        args.whisper_model = Path(str(voice.get("stt_model", "")))
        args.tts_model = str(voice.get("model", args.tts_model))
        args.speaker = str(voice.get("speaker", args.speaker))
        args.sample_rate = int(voice.get("sample_rate", args.sample_rate))
        args.device = str(voice.get("stt_device", args.device))
        args.compute_type = str(voice.get("stt_compute_type", args.compute_type))
    if not (args.whisper_model / "model.bin").is_file():
        raise FileNotFoundError(f"Не найдена модель Whisper: {args.whisper_model}")
    import torch
    from silero import silero_tts
    from stt_service import load_whisper
    from voice_worker import split_tts_text
    from butler.speech_text import normalize_for_speech

    torch.set_num_threads(4)
    tts, _example = silero_tts(language="ru", speaker=args.tts_model)
    tts.to(torch.device("cpu"))
    synthesized_text = normalize_for_speech(args.text)
    chunks = split_tts_text(synthesized_text)
    rendered = []
    pause = torch.zeros(round(args.sample_rate * 0.11), dtype=torch.float32)
    for index, chunk in enumerate(chunks):
        if index:
            rendered.append(pause)
        rendered.append(
            tts.apply_tts(
                text=chunk, speaker=args.speaker, sample_rate=args.sample_rate
            ).detach().cpu().flatten()
        )
    audio = torch.cat(rendered)
    with tempfile.TemporaryDirectory(prefix="butler-speech-") as directory:
        wav_path = Path(directory) / "sample.wav"
        write_wav(wav_path, audio, args.sample_rate, torch)
        args.model = args.whisper_model
        whisper, stt_device, stt_compute_type = load_whisper(args)
        segments, info = whisper.transcribe(
            str(wav_path),
            language="ru",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        recognized = " ".join(segment.text.strip() for segment in segments).strip()
    audio_duration_seconds = round(audio.numel() / args.sample_rate, 2)
    quality = evaluate_roundtrip(
        synthesized_text,
        recognized,
        chunk_count=len(chunks),
        audio_duration_seconds=audio_duration_seconds,
        language_probability=float(info.language_probability),
    )
    result = {
        **quality,
        "spoken": args.text,
        "synthesized_text": synthesized_text,
        "recognized": recognized,
        "chunk_count": len(chunks),
        "audio_duration_seconds": audio_duration_seconds,
        "language_probability": float(info.language_probability),
        "stt_device": stt_device,
        "stt_compute_type": stt_compute_type,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
