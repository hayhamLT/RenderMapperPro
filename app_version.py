"""Single source of truth for the application version.

Imported by app_qt.py (as APP_VERSION) and parsed by BlenderVideoMapper.spec and
CI, so the version lives in exactly one place. CI fails a ``v<X.Y.Z>`` release
tag whose value doesn't match this string.
"""
__version__ = "1.8.23"
APP_NAME = "Render Mapper Pro"   # display name; single source shared by the app + mixins
