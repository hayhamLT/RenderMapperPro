"""Single source of truth for the application version.

Imported by app_qt.py (as APP_VERSION) and parsed by PrevizRender.spec and
CI, so the version lives in exactly one place. CI fails a ``v<X.Y.Z>`` release
tag whose value doesn't match this string.
"""
__version__ = "1.9.2"
# Display name; single source shared by the app + mixins. "PrevizRender" is
# the product name.
APP_NAME = "PrevizRender"
