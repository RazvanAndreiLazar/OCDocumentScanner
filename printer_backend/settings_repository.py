from printer_backend.json_file import read_json, write_json
from printer_backend.storage import DEFAULT_SAVE_FOLDER, LocalFolderStorage, ScanStorage, storage_from_dict

SETTINGS_FILE_DEFAULT = './data/settings.json'

class SettingsRepository():
    '''The settings that don't belong to one printer, kept in a json file that is rewritten on every change.'''
    def __init__(self, path: str = SETTINGS_FILE_DEFAULT):
        self.path = path
        self.settings: dict = read_json(path, {})
        location = self.settings.get('save_location')
        self._save_location = LocalFolderStorage(DEFAULT_SAVE_FOLDER) if location is None else storage_from_dict(location)

    @property
    def save_location(self) -> ScanStorage:
        return self._save_location

    @save_location.setter
    def save_location(self, location: ScanStorage):
        self.settings['save_location'] = location.to_dict()
        write_json(self.path, self.settings)
        self._save_location = location
