"""Caregiver monitoring and indoor map authoring; enrollment stays on the Pi."""

from .web import create_dashboard_app, create_dashboard_blueprint

__all__ = ["create_dashboard_app", "create_dashboard_blueprint"]
