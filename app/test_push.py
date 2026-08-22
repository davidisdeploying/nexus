"""
Focused stdlib test for _send_one's webpush() timeout
(FLEET-WORKER2-BUILD-20260721-panel-push-timeout), and for topic_for_tag's
Web Push Topic header repair (FLEET-AUTO-BUILD-20260802-panel-webpush-topic-repair).

Production hit a ReadTimeout to web.push.apple.com with no timeout bound on
the pywebpush call. This pins an explicit 10s timeout at the call site with
no real network request (webpush is mocked).

Separately, the weekly self-test (tag "nexus-selftest") was delivered fine,
but the conformance recovery event — whose tag falls back to an event_key
containing colons, e.g. "conformance-check:memory:recovery:20260802T2100Z" —
got HTTP 400 from every subscription because raw colons are not valid in a
Web Push Topic header (1-32 URL-safe base64 chars per RFC 8030 §5.4).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from .push import _send_one, topic_for_tag


class SendOneTimeoutTests(unittest.TestCase):
    def test_webpush_called_with_10s_timeout(self):
        sub = {"endpoint": "https://push.example.com/v1/abcd", "p256dh": "p", "auth": "a"}
        payload = {"notification": {"tag": "test-tag"}}

        with patch("app.push.webpush") as mock_webpush, \
             patch("app.push.notify_store.mark_subscription_sent") as mock_mark_sent:
            _send_one(sub, payload, prio=3)

        mock_webpush.assert_called_once()
        self.assertEqual(mock_webpush.call_args.kwargs["timeout"], 10)
        mock_mark_sent.assert_called_once_with(sub["endpoint"])


class TopicForTagTests(unittest.TestCase):
    def test_nexus_selftest_tag_passes_through_unchanged(self):
        self.assertEqual(topic_for_tag("nexus-selftest"), "nexus-selftest")

    def test_colon_bearing_conformance_tag_becomes_valid_topic(self):
        tag = "conformance-check:memory:recovery:20260802T2100Z"
        topic = topic_for_tag(tag)
        self.assertLessEqual(len(topic), 32)
        self.assertGreaterEqual(len(topic), 1)
        self.assertRegex(topic, r"^[A-Za-z0-9_-]+$")

    def test_colon_bearing_run_key_becomes_valid_topic(self):
        tag = "conformance-check:charlie:alarm:20260802T2059Z"
        topic = topic_for_tag(tag)
        self.assertLessEqual(len(topic), 32)
        self.assertRegex(topic, r"^[A-Za-z0-9_-]+$")

    def test_same_input_is_stable(self):
        tag = "conformance-check:memory:recovery:20260802T2100Z"
        self.assertEqual(topic_for_tag(tag), topic_for_tag(tag))

    def test_distinct_invalid_inputs_differ(self):
        a = topic_for_tag("conformance-check:memory:recovery:20260802T2100Z")
        b = topic_for_tag("conformance-check:memory:alarm:20260802T2100Z")
        self.assertNotEqual(a, b)

    def test_empty_tag_yields_valid_topic(self):
        topic = topic_for_tag("")
        self.assertRegex(topic, r"^[A-Za-z0-9_-]{1,32}$")

    def test_overlength_tag_yields_valid_topic(self):
        topic = topic_for_tag("x" * 200)
        self.assertRegex(topic, r"^[A-Za-z0-9_-]{1,32}$")

    def test_already_valid_32_char_tag_passes_through(self):
        tag = "a" * 32
        self.assertEqual(topic_for_tag(tag), tag)


class SendOneTopicHeaderTests(unittest.TestCase):
    def test_send_one_passes_transformed_topic_to_webpush(self):
        sub = {"endpoint": "https://push.example.com/v1/abcd", "p256dh": "p", "auth": "a"}
        raw_tag = "conformance-check:memory:recovery:20260802T2100Z"
        payload = {"notification": {"tag": raw_tag}}

        with patch("app.push.webpush") as mock_webpush, \
             patch("app.push.notify_store.mark_subscription_sent") as mock_mark_sent:
            result = _send_one(sub, payload, prio=3)

        self.assertTrue(result)
        sent_topic = mock_webpush.call_args.kwargs["headers"]["Topic"]
        self.assertNotEqual(sent_topic, raw_tag[:32])
        self.assertRegex(sent_topic, r"^[A-Za-z0-9_-]{1,32}$")
        mock_mark_sent.assert_called_once_with(sub["endpoint"])

    def test_send_one_400_still_uses_transformed_topic_and_bumps_failure(self):
        from pywebpush import WebPushException

        sub = {"endpoint": "https://push.example.com/v1/abcd", "p256dh": "p", "auth": "a"}
        payload = {"notification": {"tag": "conformance-check:memory:alarm:20260802T2100Z"}}

        exc = WebPushException("boom")
        exc.response = type("R", (), {"status_code": 400})()

        with patch("app.push.webpush", side_effect=exc) as mock_webpush, \
             patch("app.push.notify_store.bump_subscription_failure") as mock_bump:
            result = _send_one(sub, payload, prio=3)

        self.assertFalse(result)
        sent_topic = mock_webpush.call_args.kwargs["headers"]["Topic"]
        self.assertRegex(sent_topic, r"^[A-Za-z0-9_-]{1,32}$")
        mock_bump.assert_called_once_with(sub["endpoint"])


if __name__ == "__main__":
    unittest.main()
