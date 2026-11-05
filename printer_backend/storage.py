from abc import ABC, abstractmethod
import asyncio
import os
import re
import shutil
from printer_backend.scanning import SCAN_FOLDER_DEFAULT

DEFAULT_SCAN_NAME = 'scan'
DEFAULT_SAVE_FOLDER = '~/Documents/Scans'
SAVE_ROOT_ENV = 'SAVE_ROOT'    # set in Docker: save locations are folders inside this one


def save_root() -> str | None:
    '''The folder every save location has to be in, or None if there is no such limit.'''
    root = os.environ.get(SAVE_ROOT_ENV, '').strip()
    return os.path.abspath(root) if root else None


def is_within(path: str, folder: str) -> bool:
    '''True if path is folder itself or inside it, following symbolic links.'''
    return os.path.commonpath([os.path.realpath(path), os.path.realpath(folder)]) == os.path.realpath(folder)


def check_scan_name(name: str):
    '''Raises ValueError if a scan can't have this name: it may not lead to another folder.'''
    if not name or name in ('.', '..') or any(character in name for character in '/\\\0'):
        raise ValueError(f"'{name}' can't be used as a name (no slashes, and not '.' or '..')")


def pick_file_names(files: list[str], name: str, existing: set[str]) -> list[str]:
    '''
    The names for the files of one scan: <name>.pdf for a single file if that is free, otherwise
    <name>(1).pdf and so on, continuing after the highest number there is so nothing gets overwritten.
    '''
    extension = os.path.splitext(files[0])[1]   # the files of a scan all have the same format
    name = name.removesuffix(extension) or name
    if len(files) == 1 and f'{name}{extension}' not in existing:
        return [f'{name}{extension}']

    numbered = re.compile(re.escape(name) + r'\((\d+)\)' + re.escape(extension))
    numbers = [int(match.group(1)) for match in map(numbered.fullmatch, existing) if match]
    first = max(numbers, default=0) + 1
    return [f'{name}({number}){extension}' for number in range(first, first + len(files))]


class ScanStorage(ABC):
    '''
    A place where finished scans are saved. A new kind of place implements the abstract methods and
    gets added to STORAGE_TYPES. Failures are raised as RuntimeError, whatever the kind of place.
    '''
    @abstractmethod
    async def validate_storage_path(self):
        '''Validates if files can be saved in the chosen location, else raise RuntimeError.'''

    @abstractmethod
    async def existing_names(self) -> set[str]:
        '''The names of the files that are already in the storage location.'''

    @abstractmethod
    async def copy_file_to_target_location(self, local_path: str, file_name: str):
        '''Stores a local file under this name, raising RuntimeError instead of overwriting anything.'''

    @abstractmethod
    def to_dict(self) -> dict:
        '''Plain data for the settings file, with its STORAGE_TYPES key as 'type'. No passwords: the file is plain text.'''

    @classmethod
    @abstractmethod
    def from_dict(cls, data: dict) -> 'ScanStorage':
        ...

    @abstractmethod
    def __str__(self):
        ...

    @property
    def display_path(self) -> str:
        '''What the user sees and types for this location. It is also what comes back in, see LocalFolderStorage.'''
        return str(self)

    async def save(self, files: list[str], name: str) -> list[str]:
        '''Stores the files of one scan under the given name, and returns the file names they got.'''
        file_names = pick_file_names(files, name, await self.existing_names())
        for local_path, file_name in zip(files, file_names):
            await self.copy_file_to_target_location(local_path, file_name)
        return file_names


class LocalFolderStorage(ScanStorage):
    def __init__(self, path: str):
        path = os.path.expanduser(path)
        root = save_root()
        if root is not None and not os.path.isabs(path):
            path = os.path.join(root, path)     # a relative location is appended to the save root
        self.path = os.path.abspath(path)

    async def validate_storage_path(self):
        root = save_root()
        if root is not None and not is_within(self.path, root):
            raise RuntimeError(f"Can't save in '{self.path}': save locations have to be inside '{root}'")
        # every scan empties that folder, which would delete what was saved in there
        if is_within(self.path, SCAN_FOLDER_DEFAULT):
            raise RuntimeError(f"Can't save in '{self.path}': it is inside the temporary folder '{SCAN_FOLDER_DEFAULT}'")
        
        try:
            os.makedirs(self.path, exist_ok=True)
        except OSError as error:
            raise RuntimeError(f"Can't save in '{self.path}': {error}") from error
        if not os.access(self.path, os.W_OK | os.X_OK):
            raise RuntimeError(f"Can't save in '{self.path}': no permission to write there")

    async def existing_names(self) -> set[str]:
        try:
            return set(os.listdir(self.path))
        except OSError as error:
            raise RuntimeError(f"Can't read '{self.path}': {error}") from error

    async def copy_file_to_target_location(self, local_path: str, file_name: str):
        target = os.path.join(self.path, file_name)
        try:
            # 'x': fails if the file exists, so nothing can be overwritten even if it appeared just now
            with open(local_path, 'rb') as source, open(target, 'xb') as destination:
                try:
                    await asyncio.to_thread(shutil.copyfileobj, source, destination)
                except OSError:
                    os.remove(target)
                    raise
        except OSError as error:
            raise RuntimeError(f"Can't save '{file_name}' in '{self.path}': {error}") from error

    @property
    def display_path(self) -> str:
        '''The path without the save root, which is added again to whatever the user enters ('' for the root itself).'''
        root = save_root()
        if root is None or not is_within(self.path, root):
            return self.path
        relative = os.path.relpath(os.path.realpath(self.path), os.path.realpath(root))
        return '' if relative == '.' else relative

    def to_dict(self) -> dict:
        return {'type': 'folder', 'path': self.path}

    @classmethod
    def from_dict(cls, data: dict) -> 'LocalFolderStorage':
        return cls(data['path'])

    def __str__(self):
        return self.path


STORAGE_TYPES: dict[str, type[ScanStorage]] = {'folder': LocalFolderStorage}


def storage_from_dict(data: dict) -> ScanStorage:
    kind = data.get('type')
    if kind not in STORAGE_TYPES:
        raise ValueError(f"Unknown kind of save location '{kind}'")
    return STORAGE_TYPES[kind].from_dict(data)
