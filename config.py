"""Configuration loading for LinkWeb.

Precedence, highest first:

1. Real environment variables (e.g. set in the systemd unit).
2. A ``.env`` file next to ``app.py`` (simple ``KEY=VALUE`` lines).
3. A ``config.yaml`` file next to ``app.py``.

No third-party dependency is required: the YAML reader below understands the
small subset this project needs (nested maps, scalars, comments, quoted
strings). If PyYAML happens to be installed it is used instead, so a full
YAML file works too.
"""

import os
import pathlib

BASE_DIR = pathlib.Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
YAML_FILE = BASE_DIR / "config.yaml"

DEFAULTS = {
    "user": "",
    "password": "",
    "db_path": str(BASE_DIR / "links.db"),
    "backup_dir": str(BASE_DIR / "backups"),
    "backup_keep": 10,
    "host": "0.0.0.0",
    "port": 6999,
}


def _parse_env_file(path):
    data = {}
    if not path.is_file():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        data[key.strip()] = value
    return data


def _load_yaml(path):
    """Load config.yaml. Prefers PyYAML, falls back to a minimal parser."""
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}
    except ImportError:
        pass

    # Minimal fallback parser: one level of nesting, scalar values only.
    result = {}
    current = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        key, _, value = line.strip().partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            current = key
            result.setdefault(current, {})
        elif current and indent > 0:
            result[current][key] = value
        else:
            current = None
            result[key] = value
    return result


def _from_yaml(mapping, *keys):
    """Return the first present value among the given key paths."""
    for key in keys:
        section, _, leaf = key.partition(".")
        if leaf:
            sub = mapping.get(section)
            if isinstance(sub, dict) and leaf in sub:
                return sub[leaf]
        elif section in mapping:
            return mapping[section]
    return None


def load_config():
    """Merge the three sources into a single config dict."""
    cfg = dict(DEFAULTS)

    yaml_data = _load_yaml(YAML_FILE)
    env_data = _parse_env_file(ENV_FILE)

    # Layer 3: config.yaml
    yaml_map = [
        ("user", ("auth.user", "user")),
        ("password", ("auth.password", "password")),
        ("db_path", ("database.path", "db_path")),
        ("backup_dir", ("backup.dir", "backup_dir")),
        ("backup_keep", ("backup.keep", "backup_keep")),
        ("host", ("server.host", "host")),
        ("port", ("server.port", "port")),
    ]
    for target, paths in yaml_map:
        value = _from_yaml(yaml_data, *paths)
        if value not in (None, ""):
            cfg[target] = value

    # Layer 2: .env
    env_map = {
        "LINKWEB_USER": "user",
        "LINKWEB_PASSWORD": "password",
        "LINKWEB_DB": "db_path",
        "LINKWEB_BACKUP_DIR": "backup_dir",
        "LINKWEB_BACKUP_KEEP": "backup_keep",
        "LINKWEB_HOST": "host",
        "LINKWEB_PORT": "port",
    }
    for source, target in env_map.items():
        if source in env_data and env_data[source] != "":
            cfg[target] = env_data[source]

    # Layer 1: real environment variables
    for source, target in env_map.items():
        if os.environ.get(source):
            cfg[target] = os.environ[source]

    # Coerce numerics.
    for key in ("backup_keep", "port"):
        try:
            cfg[key] = int(cfg[key])
        except (TypeError, ValueError):
            cfg[key] = DEFAULTS[key]

    return cfg


CONFIG = load_config()
