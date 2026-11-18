"""AI Usage Board - a local-only Windows widget for AI usage quotas."""

__version__ = "1.1.1"
#: Shown to the user: window title, tray tooltip, messages.
APP_NAME = "AI Usage Board"
#: Used for paths, the window class and the single-instance mutex. No spaces,
#: so nothing downstream has to quote it.
APP_SLUG = "AIUsageBoard"
APP_ID = f"jaehoonV.{APP_SLUG}.Widget"
BRIDGE_PORT = 47319
