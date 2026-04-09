from pathlib import Path

from pollypm.models import ProviderKind
from pollypm.plugin_host import extension_host_for_root
from pollypm.providers.base import ProviderAdapter


def get_provider(provider: ProviderKind, *, root_dir: Path | None = None) -> ProviderAdapter:
    root = str((root_dir or Path.cwd()).resolve())
    return extension_host_for_root(root).get_provider(provider.value)
