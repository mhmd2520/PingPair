"""GUI pages — one widget per top-level tab in the main window."""

from .about_view import AboutView
from .analysis_view import AnalysisView
from .config_view import ConfigView
from .help_view import HelpView
from .ping_view import PingView
from .report_view import ReportView
from .script_view import ScriptView
from .setup_view import SetupView

__all__ = [
    "AboutView",
    "AnalysisView",
    "ConfigView",
    "HelpView",
    "PingView",
    "ReportView",
    "ScriptView",
    "SetupView",
]
