from .base import ClinicStore, StoreError, clinic_tz
from .local import LocalStore


def make_store(settings) -> ClinicStore:
    if settings.store_backend == "google":
        from .google import GoogleStore

        return GoogleStore(settings.clinic, settings.google)
    return LocalStore(settings.clinic, settings.data_dir / "store.json")


__all__ = ["ClinicStore", "StoreError", "LocalStore", "clinic_tz", "make_store"]
