from nyxion_heredity_sdk import BackendError


class UnavailableBackend:
    self_id = 0
    online = False

    def call(self, action: str, params: dict):
        raise BackendError("IM backend is not configured; discovery does not establish a control API.",
                           1503)

    def close(self):
        pass
