import math
import sys
import unittest
from array import array
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from benchmark_audio_full_duplex import (  # noqa: E402
    analyze_pcm_frames,
    frame_rms,
    suppression_db,
)


def pcm_frame(value: int, samples: int = 160) -> bytes:
    return array("h", [value] * samples).tobytes()


class AudioFullDuplexBenchmarkTests(unittest.TestCase):
    def test_frame_rms_uses_pcm_amplitude(self):
        self.assertAlmostEqual(frame_rms(pcm_frame(1200)), 1200.0)

    def test_analysis_separates_noise_floor_from_activity(self):
        result = analyze_pcm_frames(
            [pcm_frame(20), pcm_frame(30), pcm_frame(40)],
            [pcm_frame(20), pcm_frame(250), pcm_frame(500), pcm_frame(40)],
        )
        self.assertEqual(result["active_frame_count"], 2)
        self.assertEqual(result["active_frame_fraction"], 0.5)
        self.assertGreater(result["measurement_rms_p95"], 400)

    def test_suppression_db_reports_energy_ratio(self):
        self.assertAlmostEqual(suppression_db(1000.0, 100.0), 20.0)
        self.assertIsNone(suppression_db(0.0, 100.0))
        self.assertTrue(math.isfinite(suppression_db(100.0, 200.0)))


if __name__ == "__main__":
    unittest.main()
