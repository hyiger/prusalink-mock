"""Mock PrusaLink server that answers the way the Prusa Buddy firmware does."""

from .api import MockPrinter
from .state import ScenarioError, list_scenarios

__version__ = "0.1.0"
__all__ = ["MockPrinter", "ScenarioError", "list_scenarios", "__version__"]
