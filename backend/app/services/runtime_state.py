import os
import re
import shutil
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.connection_auth_storage import (
    ACTIVE_SLOT,
    QUARANTINE_SLOT,
    delete_connection_artifacts,
    describe_connection_artifact_sync,
    load_connection_artifact_async,
    load_connection_artifact_sync,
    move_connection_artifacts_to_slot,
    provider_has_connection_artifacts_sync,
    save_connection_artifact_async,
    save_connection_artifact_sync,
)
from app.services.visible_auth_attempt_storage import (
    load_visible_auth_attempt_artifact_async,
    load_visible_auth_attempt_artifact_sync,
    save_visible_auth_attempt_artifact_async,
    save_visible_auth_attempt_artifact_sync,
)
from app.runtime_paths import DATA_DIR, DESKTOP_AUTH_DIR

DATA_ROOT = str(DATA_DIR)
DESKTOP_AUTH_ROOT = str(DESKTOP_AUTH_DIR)
USER_RUNTIME_ROOT = os.path.join(DATA_ROOT, "users")
DESKTOP_AUTH_USER_ROOT = os.path.join(DESKTOP_AUTH_ROOT, "users")
RUNTIME_QUARANTINE_DIR = "reauth_quarantine"
VISIBLE_AUTH_STAGING_DIR = "visible_auth"
VISIBLE_AUTH_ATTEMPT_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")

_runtime_state: dict[tuple[str, int], Any] = {}
ARTIFACT_PATH_RESOLVERS = {
    "cookies": lambda user_id, provider: get_provider_cookie_path(user_id, provider),
    "storage_state": lambda user_id, provider: get_provider_storage_state_path(user_id, provider),
    "session": lambda user_id, provider: get_provider_session_path(user_id, provider),
}
ARTIFACT_FILENAME_TO_KIND = {
    "cookies.json": "cookies",
    "storage_state.json": "storage_state",
    "session.json": "session",
}


def get_user_runtime_dir(user_id: int) -> str:
    return os.path.join(USER_RUNTIME_ROOT, str(user_id))


def get_provider_runtime_dir(user_id: int, provider: str) -> str:
    return os.path.join(get_user_runtime_dir(user_id), "providers", provider)


def get_provider_runtime_quarantine_dir(user_id: int, provider: str) -> str:
    return os.path.join(get_user_runtime_dir(user_id), RUNTIME_QUARANTINE_DIR, "providers", provider)


def get_provider_desktop_auth_dir(user_id: int, provider: str) -> str:
    return os.path.join(DESKTOP_AUTH_USER_ROOT, str(user_id), "providers", provider)


def get_provider_desktop_auth_quarantine_dir(user_id: int, provider: str) -> str:
    return os.path.join(DESKTOP_AUTH_USER_ROOT, str(user_id), RUNTIME_QUARANTINE_DIR, "providers", provider)


def normalize_visible_auth_attempt_id(attempt_id: str | None) -> str:
    normalized = VISIBLE_AUTH_ATTEMPT_ID_RE.sub("-", str(attempt_id or "").strip())
    return normalized.strip(".-")


def get_provider_visible_auth_attempt_dir(user_id: int, provider: str, attempt_id: str) -> str:
    normalized_attempt_id = normalize_visible_auth_attempt_id(attempt_id)
    if not normalized_attempt_id:
        raise ValueError("Visible auth attempt ID is required")
    return os.path.join(
        get_provider_runtime_dir(user_id, provider),
        VISIBLE_AUTH_STAGING_DIR,
        normalized_attempt_id,
    )


def get_provider_visible_auth_artifact_path(
    user_id: int,
    provider: str,
    attempt_id: str,
    artifact_kind: str,
) -> str:
    resolver = ARTIFACT_PATH_RESOLVERS.get(artifact_kind)
    if resolver is None:
        raise ValueError(f"Unsupported runtime artifact kind: {artifact_kind}")
    filename = os.path.basename(resolver(user_id, provider))
    return os.path.join(
        get_provider_visible_auth_attempt_dir(user_id, provider, attempt_id),
        filename,
    )


def get_provider_cookie_path(user_id: int, provider: str) -> str:
    return os.path.join(get_provider_runtime_dir(user_id, provider), "cookies.json")


def get_provider_session_path(user_id: int, provider: str) -> str:
    return os.path.join(get_provider_runtime_dir(user_id, provider), "session.json")


def get_provider_storage_state_path(user_id: int, provider: str) -> str:
    return os.path.join(get_provider_runtime_dir(user_id, provider), "storage_state.json")


def get_provider_quarantined_artifact_path(user_id: int, provider: str, artifact_kind: str) -> str:
    resolver = ARTIFACT_PATH_RESOLVERS.get(artifact_kind)
    if resolver is None:
        raise ValueError(f"Unsupported runtime artifact kind: {artifact_kind}")
    filename = os.path.basename(resolver(user_id, provider))
    return os.path.join(get_provider_runtime_quarantine_dir(user_id, provider), filename)


def get_provider_artifact_path(user_id: int, provider: str, artifact_kind: str) -> str:
    resolver = ARTIFACT_PATH_RESOLVERS.get(artifact_kind)
    if resolver is None:
        raise ValueError(f"Unsupported runtime artifact kind: {artifact_kind}")
    return resolver(user_id, provider)


def _runtime_artifact_ref(path: str) -> tuple[int, str, str, str] | None:
    normalized = os.path.normpath(str(path or ""))
    artifact_kind = ARTIFACT_FILENAME_TO_KIND.get(os.path.basename(normalized))
    if artifact_kind is None:
        return None

    try:
        relative = os.path.relpath(normalized, USER_RUNTIME_ROOT)
    except ValueError:
        return None
    if relative.startswith(".."):
        return None

    parts = relative.split(os.sep)
    if len(parts) == 4 and parts[1] == "providers":
        user_part, _, provider, _ = parts
        slot = ACTIVE_SLOT
    elif len(parts) == 5 and parts[1] == RUNTIME_QUARANTINE_DIR and parts[2] == "providers":
        user_part, _, _, provider, _ = parts
        slot = QUARANTINE_SLOT
    else:
        return None

    try:
        user_id = int(user_part)
    except ValueError:
        return None
    return user_id, provider, artifact_kind, slot


def _visible_auth_attempt_artifact_ref(path: str) -> tuple[int, str, str, str] | None:
    normalized = os.path.normpath(str(path or ""))
    filename = os.path.basename(normalized)
    artifact_kind = ARTIFACT_FILENAME_TO_KIND.get(filename)
    if artifact_kind is None:
        return None

    try:
        relative = os.path.relpath(normalized, USER_RUNTIME_ROOT)
    except ValueError:
        return None
    if relative.startswith(".."):
        return None

    parts = relative.split(os.sep)
    if len(parts) != 6 or parts[1] != "providers" or parts[3] != VISIBLE_AUTH_STAGING_DIR:
        return None

    user_part, _, provider, _, attempt_id, _ = parts
    try:
        user_id = int(user_part)
    except ValueError:
        return None
    normalized_attempt_id = normalize_visible_auth_attempt_id(attempt_id)
    if not normalized_attempt_id:
        return None
    return user_id, provider, normalized_attempt_id, artifact_kind


def _remove_file_if_present(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def load_json_artifact(path: str):
    attempt_artifact_ref = _visible_auth_attempt_artifact_ref(path)
    if attempt_artifact_ref is not None:
        user_id, provider, attempt_id, artifact_kind = attempt_artifact_ref
        return load_visible_auth_attempt_artifact_sync(
            user_id,
            provider,
            attempt_id,
            artifact_kind,
        )

    artifact_ref = _runtime_artifact_ref(path)
    if artifact_ref is not None:
        user_id, provider, artifact_kind, slot = artifact_ref
        return load_connection_artifact_sync(
            user_id,
            provider,
            artifact_kind,
            slot=slot,
        )

    raise ValueError(f"Unsupported runtime artifact path: {path}")


async def load_json_artifact_async(path: str):
    attempt_artifact_ref = _visible_auth_attempt_artifact_ref(path)
    if attempt_artifact_ref is not None:
        user_id, provider, attempt_id, artifact_kind = attempt_artifact_ref
        return await load_visible_auth_attempt_artifact_async(
            user_id,
            provider,
            attempt_id,
            artifact_kind,
        )

    artifact_ref = _runtime_artifact_ref(path)
    if artifact_ref is not None:
        user_id, provider, artifact_kind, slot = artifact_ref
        return await load_connection_artifact_async(
            user_id,
            provider,
            artifact_kind,
            slot=slot,
        )

    raise ValueError(f"Unsupported runtime artifact path: {path}")


def save_json_artifact(path: str, payload) -> None:
    attempt_artifact_ref = _visible_auth_attempt_artifact_ref(path)
    if attempt_artifact_ref is not None:
        user_id, provider, attempt_id, artifact_kind = attempt_artifact_ref
        if save_visible_auth_attempt_artifact_sync(
            user_id,
            provider,
            attempt_id,
            artifact_kind,
            payload,
        ):
            _remove_file_if_present(path)
            return
        raise RuntimeError("Failed to save visible auth attempt artifact")

    artifact_ref = _runtime_artifact_ref(path)
    if artifact_ref is not None:
        user_id, provider, artifact_kind, slot = artifact_ref
        if save_connection_artifact_sync(
            user_id,
            provider,
            artifact_kind,
            payload,
            slot=slot,
        ):
            _remove_file_if_present(path)
            return
        raise RuntimeError("Failed to save provider runtime artifact")
    raise ValueError(f"Unsupported runtime artifact path: {path}")


async def save_json_artifact_async(path: str, payload) -> None:
    attempt_artifact_ref = _visible_auth_attempt_artifact_ref(path)
    if attempt_artifact_ref is not None:
        user_id, provider, attempt_id, artifact_kind = attempt_artifact_ref
        saved = await save_visible_auth_attempt_artifact_async(
            user_id,
            provider,
            attempt_id,
            artifact_kind,
            payload,
        )
        if saved:
            _remove_file_if_present(path)
            return
        raise RuntimeError("Failed to save visible auth attempt artifact")

    artifact_ref = _runtime_artifact_ref(path)
    if artifact_ref is not None:
        user_id, provider, artifact_kind, slot = artifact_ref
        saved = await save_connection_artifact_async(
            user_id,
            provider,
            artifact_kind,
            payload,
            slot=slot,
        )
        if saved:
            _remove_file_if_present(path)
            return
        raise RuntimeError("Failed to save provider runtime artifact")
    raise ValueError(f"Unsupported runtime artifact path: {path}")


async def clear_provider_runtime_artifacts_async(
    db: AsyncSession,
    user_id: int,
    provider: str,
    artifact_kinds: tuple[str, ...] | list[str] | None = None,
    *,
    institution_id: int | None = None,
) -> None:
    kinds = tuple(ARTIFACT_PATH_RESOLVERS.keys()) if artifact_kinds is None else tuple(artifact_kinds)
    if not kinds:
        return
    await delete_connection_artifacts(
        db,
        user_id,
        provider,
        kinds,
        slots=(ACTIVE_SLOT, QUARANTINE_SLOT),
        institution_id=institution_id,
    )
    if institution_id is not None:
        return
    for artifact_kind in kinds:
        _remove_file_if_present(get_provider_artifact_path(user_id, provider, artifact_kind))
        _remove_file_if_present(get_provider_quarantined_artifact_path(user_id, provider, artifact_kind))


def remove_provider_runtime_dir(user_id: int, provider: str) -> None:
    shutil.rmtree(get_provider_runtime_dir(user_id, provider), ignore_errors=True)


def remove_provider_runtime_quarantine_dir(user_id: int, provider: str) -> None:
    shutil.rmtree(get_provider_runtime_quarantine_dir(user_id, provider), ignore_errors=True)


def remove_provider_desktop_auth_dir(user_id: int, provider: str) -> None:
    shutil.rmtree(get_provider_desktop_auth_dir(user_id, provider), ignore_errors=True)


def remove_provider_desktop_auth_quarantine_dir(user_id: int, provider: str) -> None:
    shutil.rmtree(get_provider_desktop_auth_quarantine_dir(user_id, provider), ignore_errors=True)


def _move_dir_to_quarantine(src: str, dst: str) -> None:
    if not os.path.isdir(src):
        return
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)


async def quarantine_provider_runtime_dir_async(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
) -> None:
    await move_connection_artifacts_to_slot(
        db,
        user_id,
        provider,
        tuple(ARTIFACT_PATH_RESOLVERS.keys()),
        source_slot=ACTIVE_SLOT,
        target_slot=QUARANTINE_SLOT,
        institution_id=institution_id,
    )
    if institution_id is not None:
        return
    _move_dir_to_quarantine(
        get_provider_runtime_dir(user_id, provider),
        get_provider_runtime_quarantine_dir(user_id, provider),
    )


def quarantine_provider_desktop_auth_dir(user_id: int, provider: str) -> None:
    _move_dir_to_quarantine(
        get_provider_desktop_auth_dir(user_id, provider),
        get_provider_desktop_auth_quarantine_dir(user_id, provider),
    )


async def clear_provider_reauth_quarantine_async(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    include_desktop_auth: bool = True,
    institution_id: int | None = None,
) -> None:
    await delete_connection_artifacts(
        db,
        user_id,
        provider,
        tuple(ARTIFACT_PATH_RESOLVERS.keys()),
        slots=(QUARANTINE_SLOT,),
        institution_id=institution_id,
    )
    if institution_id is not None:
        return
    remove_provider_runtime_quarantine_dir(user_id, provider)
    if include_desktop_auth:
        remove_provider_desktop_auth_quarantine_dir(user_id, provider)


def get_runtime_state(namespace: str, user_id: int, default=None):
    return _runtime_state.get((namespace, user_id), default)


def set_runtime_state(namespace: str, user_id: int, value) -> None:
    _runtime_state[(namespace, user_id)] = value


def pop_runtime_state(namespace: str, user_id: int, default=None):
    return _runtime_state.pop((namespace, user_id), default)


def clear_runtime_state_namespaces(user_id: int, namespaces: tuple[str, ...] | list[str]) -> None:
    for namespace in namespaces:
        pop_runtime_state(namespace, user_id, None)


def describe_provider_runtime_state(
    user_id: int,
    provider: str,
    *,
    artifact_kinds: tuple[str, ...] | list[str] = (),
    browser_engine: str = "none",
    silent_reuse_engine: str = "none",
    manual_auth_engine: str = "none",
    manual_auth_mode: str = "api",
    auth_mode: str = "api",
    reuse_mode: str = "none",
    pending_runtime_namespaces: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    runtime_dir = get_provider_runtime_dir(user_id, provider)
    artifacts = {}
    for artifact_kind in artifact_kinds:
        path = get_provider_artifact_path(user_id, provider, artifact_kind)
        db_metadata = describe_connection_artifact_sync(
            user_id,
            provider,
            artifact_kind,
            slot=ACTIVE_SLOT,
        )
        exists = False
        modified_at = None
        size_bytes = None
        storage = None
        if db_metadata is not None:
            exists = True
            size_bytes = db_metadata.get("size_bytes")
            modified_at = db_metadata.get("modified_at")
            storage = "db"
        artifacts[artifact_kind] = {
            "path": path,
            "exists": exists,
            "size_bytes": size_bytes,
            "modified_at": modified_at,
            "storage": storage,
        }

    return {
        "provider": provider,
        "runtime_dir": runtime_dir,
        "runtime_dir_exists": os.path.isdir(runtime_dir)
        or provider_has_connection_artifacts_sync(user_id, provider, slot=ACTIVE_SLOT),
        "browser_engine": browser_engine,
        "silent_reuse_engine": silent_reuse_engine,
        "manual_auth_engine": manual_auth_engine,
        "manual_auth_mode": manual_auth_mode,
        "auth_mode": auth_mode,
        "reuse_mode": reuse_mode,
        "artifact_kinds": list(artifact_kinds),
        "artifacts": artifacts,
        "pending_runtime_namespaces": list(pending_runtime_namespaces),
        "pending_runtime_state_present": {
            namespace: get_runtime_state(namespace, user_id) is not None
            for namespace in pending_runtime_namespaces
        },
    }
