"""A deliberately isolated, in-memory demonstration. It never connects to a live platform."""

from copy import deepcopy
import threading
import time

from .base import BackendError
from ..messages import message_id, positive_id


def _positive_id(value, name):
    if isinstance(value, bool):
        raise BackendError(f"{name} must be a positive integer.")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise BackendError(f"{name} must be a positive integer.") from None
    if result <= 0 or (isinstance(value, float) and result != value):
        raise BackendError(f"{name} must be a positive integer.")
    return result


class MockBackend:
    self_id = 10000
    online = True

    def __init__(self, *, id_factory=None, self_id=10000):
        self.self_id = positive_id(self_id, "self_id")
        self._lock = threading.Lock()
        self._messages = {}
        self._next_id = 1
        self._id_factory = id_factory

    def call(self, action: str, params: dict):
        with self._lock:
            return deepcopy(self._call(action, params))

    def _call(self, action, params):
        if action == "get_login_info":
            return {"user_id": self.self_id, "nickname": "Heredity Demo"}
        if action == "get_friend_list":
            return [{"user_id": 10001, "nickname": "Demo Friend", "remark": "demo"}]
        if action == "get_group_list":
            return [{"group_id": 20000, "group_name": "Heredity Demo",
                     "member_count": 2, "max_member_count": 2}]
        if action in {"send_private_msg", "send_group_msg", "send_msg"}:
            kind = action
            if action == "send_msg":
                message_type = params.get("message_type")
                if message_type is None:
                    message_type = "group" if "group_id" in params else "private"
                if message_type not in {"group", "private"}:
                    raise BackendError("message_type must be private or group.")
                kind = f"send_{message_type}_msg"
            target_key = "group_id" if kind == "send_group_msg" else "user_id"
            target = _positive_id(params.get(target_key), target_key)
            message = params.get("message")
            if not isinstance(message, (str, list)) or not message:
                raise BackendError("message must be a nonempty string or segment array.")
            if isinstance(message, list):
                if any(not isinstance(segment, dict)
                       or not isinstance(segment.get("type"), str)
                       or not isinstance(segment.get("data"), dict) for segment in message):
                    raise BackendError("Invalid message segment.")
            message_id_value = self._id_factory() if self._id_factory else self._next_id
            self._next_id += 1
            self._messages[message_id_value] = {
                "time": int(time.time()), "message_type": "group" if target_key == "group_id" else "private",
                "message_id": message_id_value, "real_id": message_id_value,
                "sender": {"user_id": self.self_id, "nickname": "Heredity Demo"},
                "message": message, target_key: target,
            }
            return {"message_id": message_id_value}
        if action in {"get_msg", "delete_msg"}:
            message_id_value = message_id(params.get("message_id"))
            if message_id_value not in self._messages:
                raise BackendError("Demo message not found.", 1404)
            if action == "delete_msg":
                del self._messages[message_id_value]
                return None
            return self._messages[message_id_value]
        if action == "can_send_image":
            return {"yes": False}
        if action == "can_send_record":
            return {"yes": False}
        raise BackendError(f"Unsupported action: {action}", 1404)

    def close(self):
        self.online = False
