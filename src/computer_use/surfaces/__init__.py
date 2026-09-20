"""Surface abstractions and the isolated Playwright implementation."""

from computer_use.surfaces.base import SurfaceDriver
from computer_use.surfaces.playwright import PlaywrightSurfaceDriver

__all__ = ["PlaywrightSurfaceDriver", "SurfaceDriver"]
