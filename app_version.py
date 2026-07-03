"""Single source of truth for the application version.

Imported by app_qt.py (as APP_VERSION) and parsed by BlenderVideoMapper.spec and
CI, so the version lives in exactly one place. CI fails a ``v<X.Y.Z>`` release
tag whose value doesn't match this string.
"""
__version__ = "1.8.34"
# Display name; single source shared by the app + mixins. "PrevizRender" is
# the product name (formerly "Render Mapper Pro"); installer/bundle artifact
# names in BlenderVideoMapper.spec and installer/windows.iss intentionally keep
# the old name so the auto-update chain and install paths stay valid.
APP_NAME = "PrevizRender"
