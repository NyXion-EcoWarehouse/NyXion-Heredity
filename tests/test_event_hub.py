import math
import threading
import unittest

from heredity.event_hub import (
    EventGapError,
    EventHub,
    EventHubClosed,
    MAX_EVENT_BYTES,
    MAX_EVENT_DEPTH,
)


def event(self_id=10000, **fields):
    return {"self_id": self_id, "post_type": "message", **fields}


class EventHubTests(unittest.TestCase):
    def test_account_validation(self):
        for value in [0, -1, True, "10000", 1.0]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                EventHub(value)
        hub = EventHub(10000)
        for value in [event(10001), event(True), {"post_type": "message"}, []]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                hub.publish(value, 1)
        self.assertEqual(hub.latest_sequence, 0)
        self.assertEqual(hub.read(0), [])

    def test_capacity_validation(self):
        for value in [0, -1, True, 1.5, "2"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                EventHub(10000, capacity=value)

    def test_account_binding_and_capacity_are_read_only(self):
        hub = EventHub(10000, capacity=2)
        self.assertEqual(hub.self_id, 10000)
        self.assertEqual(hub.capacity, 2)
        with self.assertRaises(AttributeError):
            hub.self_id = 10001
        with self.assertRaises(AttributeError):
            hub.capacity = 3

    def test_publish_and_read_are_independent_copies(self):
        hub = EventHub(10000)
        original = event(message=[{"type": "text", "data": {"text": "original"}}])
        hub.publish(original, 1)
        original["message"][0]["data"]["text"] = "changed by publisher"
        result = hub.read(0)
        self.assertEqual(result[0]["event"]["message"][0]["data"]["text"], "original")
        result[0]["event"]["message"][0]["data"]["text"] = "changed by reader"
        self.assertEqual(
            hub.read(0)[0]["event"]["message"][0]["data"]["text"], "original"
        )

    def test_replay_limits_and_independent_cursors(self):
        hub = EventHub(10000)
        for sequence in [1, 2, 3]:
            hub.publish(event(message_id=sequence), sequence)
        self.assertEqual([item["sequence"] for item in hub.read(0, limit=2)], [1, 2])
        self.assertEqual([item["sequence"] for item in hub.read(1)], [2, 3])
        self.assertEqual([item["sequence"] for item in hub.read(0)], [1, 2, 3])
        self.assertEqual(hub.read(3), [])
        self.assertEqual(hub.latest_sequence, 3)

    def test_inter_account_sequence_gaps_are_valid(self):
        hub = EventHub(10000)
        for sequence in [3, 7, 21]:
            hub.publish(event(), sequence)
        self.assertEqual([item["sequence"] for item in hub.read(3)], [7, 21])
        self.assertEqual([item["sequence"] for item in hub.read(0)], [3, 7, 21])
        with self.assertRaises(EventGapError) as raised:
            hub.read(4)
        self.assertEqual(raised.exception.oldest_sequence, 3)
        self.assertEqual(raised.exception.latest_sequence, 21)

    def test_eviction_has_visible_gap(self):
        hub = EventHub(10000, capacity=2)
        for sequence in [1, 4, 9]:
            hub.publish(event(), sequence)
        for cursor in [0, 1, 2]:
            with self.subTest(cursor=cursor), self.assertRaises(EventGapError) as raised:
                hub.read(cursor)
            self.assertEqual(raised.exception.oldest_sequence, 4)
            self.assertEqual(raised.exception.latest_sequence, 9)
        self.assertEqual([item["sequence"] for item in hub.read(4)], [9])
        self.assertEqual(hub.read(9), [])

    def test_unknown_and_future_cursors(self):
        hub = EventHub(10000)
        with self.assertRaises(ValueError):
            hub.read(1)
        hub.publish(event(), 8)
        with self.assertRaises(EventGapError):
            hub.read(7)
        with self.assertRaises(ValueError) as raised:
            hub.read(9)
        self.assertNotIsInstance(raised.exception, EventGapError)

    def test_sequence_monotonicity(self):
        hub = EventHub(10000)
        hub.publish(event(), 5)
        for sequence in [5, 4, 0, -1, True, 6.0, "6"]:
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                hub.publish(event(), sequence)
        self.assertEqual(hub.latest_sequence, 5)
        hub.publish(event(), 100)
        self.assertEqual(hub.latest_sequence, 100)

    def test_read_argument_validation(self):
        hub = EventHub(10000)
        invalid = [
            {"after": -1}, {"after": True}, {"after": "0"},
            {"after": 0, "timeout": -1}, {"after": 0, "timeout": math.inf},
            {"after": 0, "timeout": math.nan}, {"after": 0, "timeout": True},
            {"after": 0, "timeout": 10**1000},
            {"after": 0, "limit": 0}, {"after": 0, "limit": True},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                hub.read(**arguments)

    def test_wait_is_released_by_publish(self):
        hub = EventHub(10000)
        entered = threading.Event()
        result = []
        failures = []

        def read():
            entered.set()
            try:
                result.extend(hub.read(0, timeout=2))
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=read)
        worker.start()
        self.assertTrue(entered.wait(1))
        hub.publish(event(), 1)
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual([item["sequence"] for item in result], [1])

    def test_close_wakes_waiters_and_preserves_unread_data(self):
        hub = EventHub(10000)
        entered = threading.Event()
        result = []

        def read():
            entered.set()
            result.append(hub.read(0, timeout=30))

        worker = threading.Thread(target=read)
        worker.start()
        self.assertTrue(entered.wait(1))
        hub.close()
        hub.close()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [[]])
        self.assertTrue(hub.closed)
        with self.assertRaises(EventHubClosed):
            hub.publish(event(), 1)

        retained = EventHub(10000)
        retained.publish(event(), 1)
        retained.close()
        self.assertEqual([item["sequence"] for item in retained.read(0)], [1])
        self.assertEqual(retained.read(1, timeout=30), [])

    def test_finite_json_types_only(self):
        hub = EventHub(10000)
        invalid = [
            math.nan, math.inf, -math.inf, b"bytes", {1, 2}, (1, 2),
            {"non_string_key": {1: "value"}}, object(), "\ud800",
        ]
        for value in invalid:
            with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
                hub.publish(event(value=value), 1)
        circular = event()
        circular["cycle"] = circular
        with self.assertRaises(ValueError):
            hub.publish(circular, 1)
        hub.publish(event(value={"finite": 1.5, "none": None, "bool": False}), 1)
        self.assertEqual(hub.latest_sequence, 1)

    def test_nesting_limit(self):
        hub = EventHub(10000)
        nested = {}
        current = nested
        for _ in range(MAX_EVENT_DEPTH + 1):
            current["next"] = {}
            current = current["next"]
        with self.assertRaises(ValueError):
            hub.publish(event(value=nested), 1)
        valid = event(value={"level": [1, 2, 3]})
        hub.publish(valid, 1)
        self.assertEqual(hub.read(0)[0]["event"], valid)

    def test_serialized_size_limit(self):
        hub = EventHub(10000)
        for payload in ["x" * MAX_EVENT_BYTES, "\u4e00" * (MAX_EVENT_BYTES // 2)]:
            with self.subTest(unicode=payload[0] != "x"), self.assertRaises(ValueError):
                hub.publish(event(value=payload), 1)
        hub.publish(event(value="x" * (MAX_EVENT_BYTES - 100)), 1)
        self.assertEqual(hub.latest_sequence, 1)

    def test_concurrent_publish_and_read(self):
        count = 80
        hub = EventHub(10000, capacity=count)
        publish_lock = threading.Lock()
        next_sequence = 0
        reader_results = []
        failures = []
        ready = threading.Barrier(4)

        def producer():
            nonlocal next_sequence
            try:
                ready.wait(timeout=3)
                for _ in range(count // 2):
                    # Store commits and publication ordering are owned by runtime.
                    with publish_lock:
                        next_sequence += 2
                        hub.publish(event(message_id=next_sequence), next_sequence)
            except Exception as exc:
                failures.append(exc)

        def reader():
            cursor = 0
            records = []
            try:
                ready.wait(timeout=3)
                while len(records) < count:
                    batch = hub.read(cursor, timeout=3, limit=7)
                    if not batch:
                        raise AssertionError("Reader timed out before all events arrived.")
                    records.extend(batch)
                    cursor = batch[-1]["sequence"]
                reader_results.append(records)
            except Exception as exc:
                failures.append(exc)

        workers = [
            threading.Thread(target=producer),
            threading.Thread(target=producer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(reader_results), 2)
        expected = list(range(2, count * 2 + 1, 2))
        for records in reader_results:
            self.assertEqual([item["sequence"] for item in records], expected)


if __name__ == "__main__":
    unittest.main()
