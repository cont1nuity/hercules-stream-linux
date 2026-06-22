"""Build-time feature flags & internal config overrides.

`src/build_overrides.toml` is an optional TOML overlay baked into a build (by
`packaging/build-appimage.sh --set KEY=VALUE`) that OVERRIDES the user's config.toml at runtime.
Resolution order, highest wins:

    src/build_overrides.toml   >   config.toml   >   code defaults

`apply_overrides(cfg)` returns the merged config; `enabled(cfg, name)` resolves a named feature
flag (e.g. the Stream 200 XLR backend, default OFF) the same way. The override file is optional
and read-only — everything degrades to "no overrides, defaults apply" if it is absent or
unparseable, so the daemon never hard-fails on it.
"""
import os
from paths import BUILD_OVERRIDES

try:
    import tomllib as _toml          # stdlib on 3.11+
except ImportError:                  # <3.11 source installs (Ubuntu 22.04, Debian 11, RHEL 9)
    try:
        import tomli as _toml
    except ImportError:
        _toml = None

# Code-level defaults for named feature flags. A flag absent here resolves to False.
FEATURE_DEFAULTS = {
    "stream200": False,   # Stream 200 XLR backend — experimental, not yet hardware-verified
}

_cache = None             # parsed build_overrides.toml (default path), or {} ; None = unread


def load_overrides(path=None):
    """The build-baked override table (a deep dict), or {} when the file is absent/unparseable.
    Result for the default path is cached; pass an explicit `path` to bypass the cache (tests)."""
    global _cache
    default = path is None
    if default:
        if _cache is not None:
            return _cache
        path = BUILD_OVERRIDES
    data = {}
    try:
        if _toml is not None and os.path.exists(path):
            with open(path, "rb") as f:
                data = _toml.load(f) or {}
    except Exception:
        data = {}                          # a malformed override must not crash the daemon
    if default:
        _cache = data
    return data


def _deep_merge(base, over):
    """`base` with `over` layered on top — over wins. Nested tables merge recursively; scalars
    and arrays replace wholesale. Neither input is mutated."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def apply_overrides(cfg):
    """The effective config the daemon runs on: the user's `cfg` with the internal build
    overrides layered on top (overrides win)."""
    ov = load_overrides()
    return _deep_merge(cfg or {}, ov) if ov else dict(cfg or {})


def enabled(cfg, name):
    """Whether feature `name` is on. Resolution: override file > cfg[features][name] >
    FEATURE_DEFAULTS. `cfg` may be raw (pre-merge) or already merged — the override file is
    consulted directly either way, so a build override always wins."""
    ov = load_overrides().get("features", {})
    if isinstance(ov, dict) and name in ov:
        return bool(ov[name])
    feats = cfg.get("features", {}) if isinstance(cfg, dict) else {}
    if isinstance(feats, dict) and name in feats:
        return bool(feats[name])
    return FEATURE_DEFAULTS.get(name, False)


def _selftest():
    global _cache
    # deep merge: nested tables merge, scalar->table replaces
    assert _deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 9}}) == {"a": {"x": 1, "y": 9}}
    assert _deep_merge({"a": 1}, {"a": {"b": 2}}) == {"a": {"b": 2}}
    # flag resolution from config / defaults (no override file in a dev checkout)
    assert enabled({}, "stream200") is False
    assert enabled({"features": {"stream200": True}}, "stream200") is True
    assert enabled({"features": {"stream200": False}}, "stream200") is False
    assert enabled({}, "unknown_flag") is False
    # override file parses and wins over config
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".toml"); os.close(fd)
    try:
        with open(p, "w") as f:
            f.write("[features]\nstream200 = true\n")
        assert load_overrides(p) == {"features": {"stream200": True}}
        merged = _deep_merge({"features": {"stream200": False}, "brightness": 50},
                             load_overrides(p))
        assert merged["features"]["stream200"] is True and merged["brightness"] == 50
    finally:
        os.remove(p)
    saved = _cache
    _cache = {"features": {"stream200": True}}   # simulate a baked override
    try:
        assert enabled({"features": {"stream200": False}}, "stream200") is True  # override wins
    finally:
        _cache = saved
    print("features selftest: OK")


if __name__ == "__main__":
    _selftest()
