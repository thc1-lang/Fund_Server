from .factor_registry import RegistryError, load_profile_definition


class ProfileNotConfigured(RegistryError):
    pass


def load_profile(name: str = "core_v1", config_root: str = "config/scoring"):
    try:
        return load_profile_definition(name, config_root)
    except RegistryError as exc:
        if "PROFILE_NOT_CONFIGURED" in str(exc):
            raise ProfileNotConfigured(str(exc)) from exc
        raise
