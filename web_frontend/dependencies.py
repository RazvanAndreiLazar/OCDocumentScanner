from fastapi import Request

from printer_backend.printer_repository import PrinterRepository
from printer_backend.settings_repository import SettingsRepository


def get_repository(request: Request) -> PrinterRepository:
    return request.app.state.repository


def get_settings(request: Request) -> SettingsRepository:
    return request.app.state.settings
