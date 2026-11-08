from printer_backend.json_file import read_json, write_json
from printer_backend.storage import DEFAULT_SAVE_FOLDER, LocalFolderStorage, ScanStorage, save_root, storage_from_dict

SETTINGS_FILE_DEFAULT = './data/settings.json'
THEMES = ('light', 'dark')

class SettingsRepository():
    '''The settings that don't belong to one printer, kept in a json file that is rewritten on every change.'''
    def __init__(self, path: str = SETTINGS_FILE_DEFAULT):
        self.path = path
        # a file that can't be read is set aside by read_json: default settings are better than an app that doesn't start
        self.settings, self._save_location = read_json(path, ({}, self._default_location()), self._parse)

    @staticmethod
    def _default_location() -> ScanStorage:
        return LocalFolderStorage(save_root() or DEFAULT_SAVE_FOLDER)

    @classmethod
    def _parse(cls, data) -> tuple[dict, ScanStorage]:
        if not isinstance(data, dict):
            raise ValueError('the settings are not an object')
        location = data.get('save_location')
        return data, cls._default_location() if location is None else storage_from_dict(location)

    @property
    def save_location(self) -> ScanStorage:
        return self._save_location

    @save_location.setter
    def save_location(self, location: ScanStorage):
        self.settings['save_location'] = location.to_dict()
        write_json(self.path, self.settings)
        self._save_location = location

    @property
    def theme(self) -> str:
        '''The colour scheme of the web front end.'''
        return self.settings.get('theme', THEMES[0])

    @theme.setter
    def theme(self, theme: str):
        if theme not in THEMES:
            raise ValueError(f"'{theme}' is not a colour scheme")
        self.settings['theme'] = theme
        write_json(self.path, self.settings)

    @property
    def active_printer(self) -> str | None:
        '''The device the user chose last. It may have been deleted since: callers check it against the saved printers.'''
        return self.settings.get('active_printer')

    @active_printer.setter
    def active_printer(self, device: str):
        self.settings['active_printer'] = device
        write_json(self.path, self.settings)
