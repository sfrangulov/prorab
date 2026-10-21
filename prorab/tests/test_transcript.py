import json
import os
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _defaults  # noqa: E402
import transcript  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
MAIN = os.path.join(FIX, "session_main.jsonl")
SIDE = os.path.join(FIX, "session_sidechain.jsonl")


def _entry(tokens, model):
    return json.dumps({
        "type": "assistant", "isSidechain": False,
        "message": {"model": model, "content": [],
                    "usage": {"input_tokens": tokens, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0, "output_tokens": 0}},
    })


class TestTranscript(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(
            transcript.project_slug("/home/user/projects/demo-app"),
            "-home-user-projects-demo-app",
        )

    def test_last_usage_ignores_sidechain(self):
        self.assertEqual(transcript.last_usage(SIDE), (362873, "claude-opus-5-5"))
        self.assertEqual(transcript.last_usage(MAIN), (362873, "claude-opus-5-5"))

    def test_context_status_opus(self):
        d = _defaults.load()
        st = transcript.context_status(SIDE, d)
        self.assertEqual(st["tokens"], 362873)
        self.assertEqual(st["window"], 1000000)
        self.assertAlmostEqual(st["pct"], 36.3, places=1)
        self.assertFalse(st["soft"])
        self.assertFalse(st["hard"])
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "a.jsonl")
            with open(p, "w") as fh:
                fh.write(_entry(600000, "claude-opus-5-5") + "\n")
            st = transcript.context_status(p, d)
            self.assertTrue(st["soft"])
            self.assertFalse(st["hard"])
            with open(p, "w") as fh:
                fh.write(_entry(130000, "claude-unknown-9") + "\n")
            st = transcript.context_status(p, d)
            self.assertEqual(st["window"], 200000)
            self.assertTrue(st["soft"])
            self.assertFalse(st["hard"])

    def test_last_text_skips_tool_only_entries(self):
        self.assertEqual(transcript.last_text(MAIN), "first part\nsecond part")

    def test_usage_totals(self):
        self.assertEqual(
            transcript.usage_totals(MAIN),
            {"input": 12, "cache_creation": 5694, "cache_read": 356912,
             "output": 1415, "messages": 2},
        )

    def test_usage_totals_dedups_by_message_id(self):
        # three lines share one id (last usage wins), one more id
        self.assertEqual(
            transcript.usage_totals(os.path.join(FIX, "session_multiline.jsonl")),
            {"input": 6, "cache_creation": 20, "cache_read": 200,
             "output": 47, "messages": 2},
        )

    def test_find_transcript_glob_fallback(self):
        with tempfile.TemporaryDirectory() as home:
            d = os.path.join(home, ".claude", "projects", "-other-slug")
            os.makedirs(d)
            p = os.path.join(d, "sess1.jsonl")
            open(p, "w").close()
            self.assertEqual(transcript.find_transcript("/a/b", "sess1", home), p)
            self.assertIsNone(transcript.find_transcript("/a/b", "nope", home))
            d2 = os.path.join(home, ".claude", "projects", "-a-b")
            os.makedirs(d2)
            p2 = os.path.join(d2, "sess1.jsonl")
            open(p2, "w").close()
            self.assertEqual(transcript.find_transcript("/a/b", "sess1", home), p2)


if __name__ == "__main__":
    unittest.main()
