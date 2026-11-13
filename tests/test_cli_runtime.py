import ast
import http.client
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CLIRuntimeTests(unittest.TestCase):
    def start_runtime(self, state_dir):
        environment = dict(os.environ)
        environment.update(HEREDITY_ACCESS_TOKEN="fixture-consumer",
                           HEREDITY_PROVIDER_TOKEN="fixture-provider",
                           HEREDITY_EVENT_SECRET="fixture-signing")
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "run.py"), "serve", "--demo", "--sse",
             "--port", "0", "--state-dir", str(state_dir)],
            cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.addCleanup(self.stop_runtime, child)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True)
        reader.start()
        try:
            line = lines.get(timeout=8).strip()
        except queue.Empty:
            self.fail("CLI did not report its listener in time.")
        self.assertIn("backend=DEMO", line)
        address = ast.literal_eval(line.split(" on ", 1)[1].split("; backend=", 1)[0])
        return child, address

    @staticmethod
    def stop_runtime(child):
        if child.poll() is None:
            child.terminate()
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)

    def request(self, address, method, path, body=None, token="fixture-consumer"):
        connection = http.client.HTTPConnection(*address, timeout=4)
        headers = {"Authorization": f"Bearer {token}"}
        if token == "fixture-provider":
            _, runtime = self.request(address, "GET", "/get_runtime_info")
            headers["X-Heredity-Session-Epoch"] = runtime["data"]["session_epoch"]
        encoded = None
        if body is not None:
            encoded = json.dumps(body)
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_real_cli_persists_messages_and_ids_across_restart(self):
        with tempfile.TemporaryDirectory() as state_dir:
            first, address = self.start_runtime(state_dir)
            try:
                _, sent = self.request(address, "POST", "/send_private_msg",
                                       {"user_id": 10001, "message": "hello[CQ:at,target=10002]"})
                message_id = sent["data"]["message_id"]
                event = {"time": 1700000000, "self_id": 10000, "post_type": "message",
                         "message_type": "private", "message_id": -500,
                         "user_id": 10001, "message": "incoming fixture"}
                status, received = self.request(address, "POST", "/_heredity/events",
                                                event, token="fixture-provider")
                self.assertEqual(status, 200)
                self.assertFalse(received["data"]["duplicate"])
                connection = http.client.HTTPConnection(*address, timeout=4)
                stream = None
                try:
                    connection.request("GET", "/_heredity/events?after=0",
                                       headers={"Authorization": "Bearer fixture-consumer"})
                    stream = connection.getresponse()
                    self.assertEqual(stream.status, 200)
                    found = False
                    for _ in range(20):
                        line = stream.readline()
                        if line.startswith(b"data: "):
                            payload = json.loads(line[6:])
                            if payload.get("message_id") == -500:
                                found = True
                                break
                    self.assertTrue(found)
                finally:
                    if stream is not None:
                        stream.close()
                    connection.close()
            finally:
                self.stop_runtime(first)

            second, address = self.start_runtime(state_dir)
            try:
                _, stored = self.request(address, "GET", f"/get_msg?message_id={message_id}")
                self.assertEqual(stored["data"]["message"][0]["data"]["text"], "hello")
                _, history = self.request(
                    address, "GET", "/get_local_history?message_type=private&user_id=10001&limit=5")
                self.assertEqual(len(history["data"]["messages"]), 2)
                _, repeated = self.request(address, "POST", "/_heredity/events",
                                           event, token="fixture-provider")
                self.assertTrue(repeated["data"]["duplicate"])
                _, next_send = self.request(address, "POST", "/send_private_msg",
                                            {"user_id": 10001, "message": "after restart"})
                self.assertGreater(next_send["data"]["message_id"], message_id)
            finally:
                self.stop_runtime(second)


if __name__ == "__main__":
    unittest.main()
