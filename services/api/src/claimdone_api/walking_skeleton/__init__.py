"""Lazy, explicitly opt-in exports for the retired walking skeleton."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .portal import HttpPortalPort, PortalPort
    from .router import create_walking_skeleton_router
    from .service import WalkingSkeletonService

__all__ = [
    "HttpPortalPort",
    "PortalPort",
    "WalkingSkeletonService",
    "create_walking_skeleton_router",
]


def __getattr__(name: str) -> Any:
    if name in {"HttpPortalPort", "PortalPort"}:
        from .portal import HttpPortalPort, PortalPort

        return {"HttpPortalPort": HttpPortalPort, "PortalPort": PortalPort}[name]
    if name == "WalkingSkeletonService":
        from .service import WalkingSkeletonService

        return WalkingSkeletonService
    if name == "create_walking_skeleton_router":
        from .router import create_walking_skeleton_router

        return create_walking_skeleton_router
    raise AttributeError(name)
