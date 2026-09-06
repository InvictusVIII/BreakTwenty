import os
import signal
import time


class TestSdkProcessHandler:
    def __init__(self, payload: dict) -> None:
        self._hang_close = bool(payload.get("hang_close"))
        time.sleep(float(payload.get("init_delay") or 0))

    def handle(self, operation: str, payload: dict):
        if operation == "echo":
            return {"pid": os.getpid(), "value": payload.get("value")}
        if operation == "sleep":
            time.sleep(float(payload.get("seconds") or 0))
            return os.getpid()
        if operation == "ignore_terminate_and_sleep":
            if os.name != "nt":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(float(payload.get("seconds") or 0))
            return os.getpid()
        if operation == "ignore_terminate":
            if os.name != "nt":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            return os.getpid()
        if operation == "large_result":
            return b"x" * int(payload.get("bytes") or 0)
        if operation == "error":
            raise ValueError("remote failure")
        if operation == "crash":
            os._exit(73)
        raise ValueError(f"unsupported operation: {operation}")

    def close(self) -> None:
        if self._hang_close:
            time.sleep(30)
