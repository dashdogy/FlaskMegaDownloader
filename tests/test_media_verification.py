from __future__ import annotations

import json
import unittest

from media_compiler import parse_mediainfo_json
from models import MediaVerification


class MediaVerificationTests(unittest.TestCase):
    def test_mediainfo_dolby_flags_are_preservation_not_playback_proof(self) -> None:
        raw = json.dumps(
            {
                "media": {
                    "track": [
                        {
                            "@type": "Video",
                            "Format": "HEVC",
                            "HDR_Format": "Dolby Vision, dvhe.07.06, Full Enhancement Layer",
                        },
                        {
                            "@type": "Audio",
                            "Format": "MLP FBA",
                            "Format_Commercial_IfAny": "Dolby TrueHD with Dolby Atmos",
                        },
                    ]
                }
            }
        )

        verification = parse_mediainfo_json(raw)

        self.assertTrue(verification.dolby_vision)
        self.assertTrue(verification.dolby_vision_preserved)
        self.assertEqual(verification.dolby_vision_profile, "7")
        self.assertEqual(verification.dolby_vision_enhancement_layer, "FEL signaled")
        self.assertTrue(verification.dolby_atmos)
        self.assertTrue(verification.dolby_atmos_preserved)
        self.assertTrue(verification.truehd_atmos_preserved)
        self.assertFalse(verification.playback_client_verified)
        self.assertIn("does not prove", verification.playback_verification_note or "")
        self.assertIn("Dolby Vision FEL", verification.playback_verification_note or "")
        self.assertIn("TrueHD Atmos", verification.playback_verification_note or "")

    def test_legacy_verification_payloads_gain_preservation_defaults(self) -> None:
        verification = MediaVerification.from_dict(
            {
                "dolby_vision": True,
                "dolby_atmos": True,
                "video_codec": "HEVC",
                "audio_codec": "TrueHD",
                "verified_at": "2026-05-15T00:00:00Z",
            }
        )

        self.assertTrue(verification.dolby_vision)
        self.assertTrue(verification.dolby_vision_preserved)
        self.assertTrue(verification.dolby_atmos)
        self.assertTrue(verification.dolby_atmos_preserved)
        self.assertFalse(verification.playback_client_verified)

    def test_plain_mediainfo_verification_still_records_codecs(self) -> None:
        raw = json.dumps(
            {
                "media": {
                    "track": [
                        {"@type": "Video", "Format": "AVC"},
                        {"@type": "Audio", "Format": "DTS"},
                    ]
                }
            }
        )

        verification = parse_mediainfo_json(raw)

        self.assertFalse(verification.dolby_vision_preserved)
        self.assertFalse(verification.dolby_atmos_preserved)
        self.assertFalse(verification.playback_client_verified)
        self.assertEqual(verification.video_codec, "AVC")
        self.assertEqual(verification.audio_codec, "DTS")
        self.assertIn("No playback client", verification.playback_verification_note or "")


if __name__ == "__main__":
    unittest.main()
