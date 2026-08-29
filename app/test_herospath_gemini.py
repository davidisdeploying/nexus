import json
import tempfile
import unittest
from pathlib import Path

from app import herospath


class GeminiHeroPathTests(unittest.TestCase):
    def test_codex_json_maps_session_command_and_message(self):
        records = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "item.started", "item": {
                "id": "item_1", "type": "command_execution",
                "command": "/bin/bash -lc pwd", "status": "in_progress",
            }},
            {"type": "item.completed", "item": {
                "id": "item_1", "type": "command_execution",
                "command": "/bin/bash -lc pwd",
                "aggregated_output": "/home/david\n",
                "exit_code": 0, "status": "completed",
            }},
            {"type": "item.completed", "item": {
                "id": "item_2", "type": "agent_message",
                "text": "Codex final answer",
            }},
            {"type": "turn.completed", "usage": {"input_tokens": 5}},
        ]
        events = list(herospath.iter_events(json.dumps(row) for row in records))
        self.assertEqual(
            [event["kind"] for event in events],
            ["session", "tool_use", "tool_result", "text"],
        )
        self.assertEqual(events[0]["model"], "codex")
        self.assertIn("pwd", events[1]["input"])
        self.assertEqual(events[2]["text"], "/home/david\n")
        self.assertEqual(events[3]["text"], "Codex final answer")

    def test_stream_json_maps_session_tools_and_result(self):
        records = [
            {"event": "init", "init": {
                "model": "gemini-3.6-flash-high", "cwd": "/home/david",
            }},
            {"event": "step_update", "step_update": {
                "step_index": 2, "state": "ACTIVE", "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {"name": "run_command",
                              "parameters": {"CommandLine": "pwd"}},
            }},
            {"event": "step_update", "step_update": {
                "step_index": 2, "state": "DONE", "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {"name": "run_command", "output": "/home/david\n"},
            }},
            {"event": "step_update", "step_update": {
                "state": "DONE", "step_type": "agent_response",
                "text_delta": "final answer",
            }},
            {"event": "result", "result": {
                "status": "SUCCESS", "response": "final answer",
            }},
        ]
        events = list(herospath.iter_events(json.dumps(row) for row in records))
        self.assertEqual(
            [event["kind"] for event in events],
            ["session", "tool_use", "tool_result", "text", "result"],
        )
        self.assertEqual(events[0]["model"], "gemini-3.6-flash-high")
        self.assertIn("pwd", events[1]["input"])
        self.assertEqual(events[2]["text"], "/home/david\n")

    def test_legacy_text_transcript_is_a_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "FLEET-WORKER1-BUILD-20260728-legacy.txt"
            path.write_text("legacy Gemini answer", encoding="utf-8")
            data = herospath.read_session(path)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["events"][0]["kind"], "result")
        self.assertEqual(data["events"][0]["text"], "legacy Gemini answer")

    def test_resolver_prefers_json_but_accepts_legacy_text(self):
        old_dirs = herospath.TRANSCRIPT_DIRS
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp).resolve()
                herospath.TRANSCRIPT_DIRS = {"worker1": base}
                token = "FLEET-WORKER1-BUILD-20260728-resolve"
                text_path = base / f"{token}.txt"
                text_path.write_text("old", encoding="utf-8")
                self.assertEqual(herospath.resolve_transcript(token), ("worker1", text_path))
                json_path = base / f"{token}.json"
                json_path.write_text(
                    '{"event":"result","result":{"response":"new"}}\n',
                    encoding="utf-8",
                )
                self.assertEqual(herospath.resolve_transcript(token), ("worker1", json_path))
        finally:
            herospath.TRANSCRIPT_DIRS = old_dirs


if __name__ == "__main__":
    unittest.main()
