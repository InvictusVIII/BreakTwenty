from app import key_material


def reset_cached_key_material() -> None:
    with key_material._key_material_lock:
        key_material._key_material = None
