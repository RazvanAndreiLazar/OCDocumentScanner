from fastapi import APIRouter, Depends, Form, Request

from printer_backend.settings_repository import SettingsRepository
from printer_backend.storage import LocalFolderStorage, save_root
from web_frontend.dependencies import get_settings

router = APIRouter()


async def change_save_location(settings: SettingsRepository, path: str):
    '''Makes path the save location, raising RuntimeError or ValueError, and changing nothing, if scans can't be saved there.'''
    path = path.strip()
    if not path and save_root() is None:    # LocalFolderStorage would take it for the folder the server runs in
        raise ValueError('Enter the folder to save scans in')
    location = LocalFolderStorage(path)
    await location.validate_storage_path()
    if str(location) != str(settings.save_location):
        settings.save_location = location


@router.get('/settings/theme')
async def theme_toggle(request: Request):
    return request.app.state.templates.TemplateResponse(request, 'settings/_theme_toggle.html')


@router.post('/settings/theme')
async def change_theme(request: Request, theme: str = Form(''), settings: SettingsRepository = Depends(get_settings)):
    settings.theme = theme
    return request.app.state.templates.TemplateResponse(request, 'settings/_theme_toggle.html')


@router.post('/settings/save-location')
async def save_location(request: Request, path: str = Form(''), settings: SettingsRepository = Depends(get_settings)):
    await change_save_location(settings, path)
    return request.app.state.templates.TemplateResponse(request, 'settings/_saved_location.html',
                                                        {'save_location': settings.save_location})
