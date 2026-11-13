"""Compatibility import for the public SDK's OneBot value helpers."""

from nyxion_heredity_sdk.onebot import (
    MAX_JSON_BYTES, MAX_JSON_DEPTH, MIN_MESSAGE_ID, MAX_MESSAGE_ID,
    _integer, _json_copy, _unescape, _escape, _validate_segment,
    cq_string, message_id, normalize_message, positive_id,
)
