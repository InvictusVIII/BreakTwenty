from __future__ import annotations

import asyncio
import importlib
import multiprocessing
import pickle
import socket
import struct
import threading
import time
from collections.abc import Mapping
from typing import Any


SDK_PROCESS_START_TIMEOUT_SECONDS = 15.0
SDK_PROCESS_CLOSE_TIMEOUT_SECONDS = 2.0
SDK_PROCESS_TERMINATE_TIMEOUT_SECONDS = 1.0
SDK_PROCESS_KILL_TIMEOUT_SECONDS = 1.0
SDK_PROCESS_POLL_INTERVAL_SECONDS = 0.01
SDK_PROCESS_MAX_CONCURRENT_WORKERS = 8
SDK_PROCESS_MAX_REQUEST_BYTES = 256 * 1024
SDK_PROCESS_MAX_RESPONSE_BYTES = 16 * 1024 * 1024

_FRAME_HEADER = struct.Struct("!I")

_SDK_PROCESS_CAPACITY = threading.BoundedSemaphore(SDK_PROCESS_MAX_CONCURRENT_WORKERS)


class SdkProcessError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        remote_type: str = "",
        remote_module: str = "",
    ) -> None:
        super().__init__(message)
        self.remote_type = remote_type
        self.remote_module = remote_module


class SdkProcessCrashed(SdkProcessError):
    pass


class SdkProcessTimeout(TimeoutError):
    pass


class _SdkProcessTransportError(RuntimeError):
    pass


class _SdkProcessRequestError(SdkProcessError):
    pass


def _resolve_handler(handler_path: str):
    module_name, separator, attribute_name = handler_path.rpartition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("SDK process handler must use 'module:attribute' format")
    module = importlib.import_module(module_name)
    return getattr(module, attribute_name)


def _error_payload(exc: BaseException) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "module": type(exc).__module__,
        "message": str(exc)[:1000],
    }


def _encode_frame(value: Any, *, max_bytes: int, label: str) -> bytes:
    try:
        payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    except BaseException as exc:
        raise _SdkProcessTransportError(
            f"SDK process {label} could not be serialized"
        ) from exc
    if len(payload) > max_bytes:
        raise _SdkProcessTransportError(
            f"SDK process {label} exceeds the {max_bytes}-byte transport limit"
        )
    return _FRAME_HEADER.pack(len(payload)) + payload


def _recv_exact_blocking(control_socket: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = control_socket.recv(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_request_blocking(control_socket: socket.socket) -> dict[str, Any]:
    header = _recv_exact_blocking(control_socket, _FRAME_HEADER.size)
    size = _FRAME_HEADER.unpack(header)[0]
    if size > SDK_PROCESS_MAX_REQUEST_BYTES:
        raise _SdkProcessTransportError(
            "SDK process request exceeds the control-socket transport limit"
        )
    request = pickle.loads(_recv_exact_blocking(control_socket, size))
    if not isinstance(request, dict):
        raise _SdkProcessTransportError("SDK process request is malformed")
    return request


def _decode_init_frame(frame: bytes) -> dict[str, Any]:
    if len(frame) < _FRAME_HEADER.size:
        raise _SdkProcessTransportError("SDK process initialization is malformed")
    size = _FRAME_HEADER.unpack(frame[: _FRAME_HEADER.size])[0]
    payload = frame[_FRAME_HEADER.size :]
    if size != len(payload) or size > SDK_PROCESS_MAX_REQUEST_BYTES:
        raise _SdkProcessTransportError("SDK process initialization is malformed")
    value = pickle.loads(payload)
    if not isinstance(value, dict):
        raise _SdkProcessTransportError("SDK process initialization is malformed")
    return value


def _send_response_blocking(
    control_socket: socket.socket,
    response: Mapping[str, Any],
) -> None:
    control_socket.sendall(
        _encode_frame(
            dict(response),
            max_bytes=SDK_PROCESS_MAX_RESPONSE_BYTES,
            label="response",
        )
    )


def _send_result_blocking(
    control_socket: socket.socket,
    request_id: Any,
    value: Any,
) -> None:
    try:
        _send_response_blocking(
            control_socket,
            {"kind": "result", "id": request_id, "value": value},
        )
    except _SdkProcessTransportError as exc:
        _send_response_blocking(
            control_socket,
            {
                "kind": "error",
                "id": request_id,
                "error": _error_payload(exc),
            },
        )


def _sdk_process_main(
    control_socket: socket.socket,
    handler_path: str,
    init_frame: bytes,
) -> None:
    handler = None
    try:
        handler_type = _resolve_handler(handler_path)
        handler = handler_type(_decode_init_frame(init_frame))
        _send_response_blocking(control_socket, {"kind": "ready"})
        while True:
            request = _recv_request_blocking(control_socket)
            request_id = request.get("id")
            operation = request.get("operation")
            if operation == "__close__":
                current_handler = handler
                handler = None
                if current_handler is not None:
                    current_handler.close()
                _send_response_blocking(
                    control_socket,
                    {"kind": "closed", "id": request_id},
                )
                return
            try:
                value = handler.handle(str(operation), request.get("payload") or {})
            except BaseException as exc:
                _send_response_blocking(
                    control_socket,
                    {
                        "kind": "error",
                        "id": request_id,
                        "error": _error_payload(exc),
                    }
                )
            else:
                _send_result_blocking(control_socket, request_id, value)
    except EOFError:
        return
    except BaseException as exc:
        try:
            _send_response_blocking(
                control_socket,
                {"kind": "startup_error", "error": _error_payload(exc)},
            )
        except BaseException:
            pass
    finally:
        if handler is not None:
            try:
                handler.close()
            except BaseException:
                pass
        control_socket.close()


class SdkProcessWorker:
    """Single-owner RPC process for blocking provider SDK state.

    ``start``/``call``/``close`` must be driven by one event loop. Calls are
    serialized so a provider session and its SDK contexts retain child affinity.
    """

    def __init__(
        self,
        handler_path: str,
        init_payload: Mapping[str, Any] | None = None,
        *,
        start_timeout: float = SDK_PROCESS_START_TIMEOUT_SECONDS,
    ) -> None:
        self._handler_path = handler_path
        self._init_payload = dict(init_payload or {})
        self._start_timeout = max(float(start_timeout), 0.01)
        self._context = multiprocessing.get_context("spawn")
        self._socket: socket.socket | None = None
        self._process = None
        self._last_exitcode: int | None = None
        self._request_id = 0
        self._capacity_acquired = False
        self._closed = False
        self._operation_lock = asyncio.Lock()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def is_alive(self) -> bool:
        return bool(self._process is not None and self._process.is_alive())

    @property
    def exitcode(self) -> int | None:
        if self._process is not None:
            try:
                return self._process.exitcode
            except ValueError:
                pass
        return self._last_exitcode

    async def __aenter__(self) -> SdkProcessWorker:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def start(self) -> None:
        if self._closed:
            raise SdkProcessCrashed("SDK process worker is closed")
        if self._process is not None:
            return
        try:
            init_frame = _encode_frame(
                self._init_payload,
                max_bytes=SDK_PROCESS_MAX_REQUEST_BYTES,
                label="initialization",
            )
        except _SdkProcessTransportError as exc:
            raise SdkProcessError(str(exc)) from exc
        if not _SDK_PROCESS_CAPACITY.acquire(blocking=False):
            raise SdkProcessError("SDK process worker capacity is busy")
        self._capacity_acquired = True
        parent_socket, child_socket = socket.socketpair()
        parent_socket.setblocking(False)
        process = self._context.Process(
            target=_sdk_process_main,
            args=(child_socket, self._handler_path, init_frame),
            name=f"breaktwenty-sdk-{self._handler_path.rsplit(':', 1)[-1]}",
            daemon=True,
        )
        self._socket = parent_socket
        self._process = process
        try:
            # ``spawn`` is intentionally synchronous here. Init payloads are small and
            # bounded, and offloading Process.start() would let cancellation release
            # this worker's capacity before the background thread finishes creating
            # its child.
            process.start()
            child_socket.close()
            response = await self._receive(self._start_timeout, stage="startup")
            if response.get("kind") == "startup_error":
                raise self._remote_error(response)
            if response.get("kind") != "ready":
                raise SdkProcessCrashed("SDK process worker returned an invalid startup response")
        except BaseException:
            child_socket.close()
            await self._terminate_process()
            raise

    async def call(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout: float,
    ) -> Any:
        async with self._operation_lock:
            if self._process is None:
                await self.start()
            if self._closed or self._socket is None:
                raise SdkProcessCrashed("SDK process worker is closed")
            self._request_id += 1
            request_id = self._request_id
            request = {
                "id": request_id,
                "operation": str(operation),
                "payload": dict(payload or {}),
            }
            operation_timeout = max(float(timeout), 0.01)
            deadline = asyncio.get_running_loop().time() + operation_timeout
            try:
                await self._send(
                    request,
                    deadline=deadline,
                    timeout=operation_timeout,
                    stage=operation,
                )
                response = await self._receive_until(
                    deadline,
                    timeout=operation_timeout,
                    stage=operation,
                )
            except _SdkProcessRequestError:
                # Local serialization/size rejection never touched the child and
                # therefore does not invalidate its SDK session.
                raise
            except asyncio.CancelledError:
                await self._terminate_process()
                raise
            except BaseException:
                await self._terminate_process()
                raise
            if response.get("id") != request_id:
                await self._terminate_process()
                raise SdkProcessCrashed("SDK process worker returned an out-of-order response")
            if response.get("kind") == "error":
                raise self._remote_error(response)
            if response.get("kind") != "result":
                await self._terminate_process()
                raise SdkProcessCrashed("SDK process worker returned an invalid response")
            return response.get("value")

    async def close(self) -> None:
        if self._closed and self._process is None:
            return
        self._closed = True
        if self._process is None:
            self._release_capacity()
            return
        cancelled = False
        async with self._operation_lock:
            try:
                if self._process_is_alive(self._process) and self._socket is not None:
                    self._request_id += 1
                    request_id = self._request_id
                    timeout = SDK_PROCESS_CLOSE_TIMEOUT_SECONDS
                    deadline = asyncio.get_running_loop().time() + timeout
                    await self._send(
                        {"id": request_id, "operation": "__close__", "payload": {}},
                        deadline=deadline,
                        timeout=timeout,
                        stage="shutdown",
                    )
                    response = await self._receive_until(
                        deadline,
                        timeout=timeout,
                        stage="shutdown",
                    )
                    if response.get("kind") != "closed" or response.get("id") != request_id:
                        raise SdkProcessCrashed(
                            "SDK process worker did not acknowledge shutdown"
                        )
                    remaining = max(deadline - asyncio.get_running_loop().time(), 0.0)
                    await self._wait_for_exit(remaining)
            except asyncio.CancelledError:
                cancelled = True
                await self._terminate_process()
            except BaseException:
                await self._terminate_process()
            finally:
                if self._process is not None and self._process_is_alive(self._process):
                    await self._terminate_process()
                self._cleanup()
        if cancelled:
            raise asyncio.CancelledError

    async def _send(
        self,
        request: Mapping[str, Any],
        *,
        deadline: float,
        timeout: float,
        stage: str,
    ) -> None:
        if self._socket is None or self._process is None:
            raise SdkProcessCrashed("SDK process worker is unavailable")
        loop = asyncio.get_running_loop()
        try:
            frame = _encode_frame(
                dict(request),
                max_bytes=SDK_PROCESS_MAX_REQUEST_BYTES,
                label="request",
            )
        except _SdkProcessTransportError as exc:
            raise _SdkProcessRequestError(str(exc)) from exc
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise self._timeout(stage, timeout)
        try:
            await asyncio.wait_for(
                loop.sock_sendall(self._socket, frame),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            raise self._timeout(stage, timeout) from exc
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            raise SdkProcessCrashed(
                f"SDK process worker exited during {stage}"
            ) from exc

    async def _receive(self, timeout: float, *, stage: str) -> dict[str, Any]:
        timeout = max(float(timeout), 0.01)
        deadline = asyncio.get_running_loop().time() + timeout
        return await self._receive_until(deadline, timeout=timeout, stage=stage)

    async def _receive_until(
        self,
        deadline: float,
        *,
        timeout: float,
        stage: str,
    ) -> dict[str, Any]:
        header = await self._recv_exact(
            _FRAME_HEADER.size,
            deadline=deadline,
            timeout=timeout,
            stage=stage,
        )
        size = _FRAME_HEADER.unpack(header)[0]
        if size > SDK_PROCESS_MAX_RESPONSE_BYTES:
            raise SdkProcessCrashed(
                "SDK process response exceeds the control-socket transport limit"
            )
        payload = await self._recv_exact(
            size,
            deadline=deadline,
            timeout=timeout,
            stage=stage,
        )
        try:
            response = pickle.loads(payload)
        except BaseException as exc:
            raise SdkProcessCrashed("SDK process worker returned malformed data") from exc
        if asyncio.get_running_loop().time() > deadline:
            raise self._timeout(stage, timeout)
        if not isinstance(response, dict):
            raise SdkProcessCrashed("SDK process worker returned malformed data")
        return response

    async def _recv_exact(
        self,
        size: int,
        *,
        deadline: float,
        timeout: float,
        stage: str,
    ) -> bytes:
        if self._socket is None or self._process is None:
            raise SdkProcessCrashed("SDK process worker is unavailable")
        loop = asyncio.get_running_loop()
        chunks: list[bytes] = []
        remaining_size = size
        while remaining_size:
            remaining_time = deadline - loop.time()
            if remaining_time <= 0:
                raise self._timeout(stage, timeout)
            try:
                chunk = await asyncio.wait_for(
                    loop.sock_recv(self._socket, remaining_size),
                    timeout=remaining_time,
                )
            except asyncio.TimeoutError as exc:
                raise self._timeout(stage, timeout) from exc
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                raise SdkProcessCrashed(
                    f"SDK process worker exited during {stage}"
                ) from exc
            if not chunk:
                exitcode = self._process.exitcode
                raise SdkProcessCrashed(
                    f"SDK process worker exited during {stage} with code {exitcode}"
                )
            chunks.append(chunk)
            remaining_size -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _timeout(stage: str, timeout: float) -> SdkProcessTimeout:
        return SdkProcessTimeout(
            f"SDK process operation {stage} timed out after {timeout:g}s"
        )

    async def _wait_for_exit(self, timeout: float) -> bool:
        if self._process is None:
            return True
        deadline = time.monotonic() + max(timeout, 0.0)
        while self._process.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(SDK_PROCESS_POLL_INTERVAL_SECONDS)
        await asyncio.to_thread(self._process.join, 0)
        return not self._process.is_alive()

    async def _terminate_process(self) -> None:
        # Abnormal termination invalidates all SDK session/context state. Never
        # auto-spawn a replacement behind the connector's back.
        self._closed = True
        process = self._process
        if process is None:
            self._cleanup()
            return
        if self._process_is_alive(process):
            try:
                process.terminate()
            except BaseException:
                pass
            try:
                await self._wait_for_exit(SDK_PROCESS_TERMINATE_TIMEOUT_SECONDS)
            except BaseException:
                pass
        if self._process_is_alive(process):
            try:
                process.kill()
            except BaseException:
                pass
            try:
                await self._wait_for_exit(SDK_PROCESS_KILL_TIMEOUT_SECONDS)
            except BaseException:
                pass
        if self._process_is_alive(process):
            self._retain_unreaped_process()
            return
        self._cleanup()

    @staticmethod
    def _process_is_alive(process) -> bool:
        try:
            return bool(process.is_alive())
        except BaseException:
            return False

    def _remote_error(self, response: Mapping[str, Any]) -> SdkProcessError:
        error = response.get("error")
        details = error if isinstance(error, Mapping) else {}
        return SdkProcessError(
            str(details.get("message") or "SDK process operation failed"),
            remote_type=str(details.get("type") or ""),
            remote_module=str(details.get("module") or ""),
        )

    def _cleanup(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except BaseException:
                pass
            self._socket = None
        if self._process is not None:
            if self._process.pid is not None and self._process_is_alive(self._process):
                return
            if self._process.pid is None:
                try:
                    self._process.close()
                except BaseException:
                    pass
                self._process = None
                self._release_capacity()
                return
            try:
                self._process.join(timeout=0)
            except BaseException:
                pass
            try:
                self._last_exitcode = self._process.exitcode
            except BaseException:
                pass
            try:
                self._process.close()
            except BaseException:
                pass
            self._process = None
        self._release_capacity()

    def _retain_unreaped_process(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except BaseException:
                pass
            self._socket = None

    def _release_capacity(self) -> None:
        if not self._capacity_acquired:
            return
        self._capacity_acquired = False
        _SDK_PROCESS_CAPACITY.release()
