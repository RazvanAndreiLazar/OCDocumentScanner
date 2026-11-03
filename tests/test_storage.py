import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from printer_backend import storage as storage_module
from printer_backend.settings_repository import SettingsRepository
from printer_backend.storage import (
    DEFAULT_SAVE_FOLDER, STORAGE_TYPES, LocalFolderStorage, ScanStorage, check_scan_name, pick_file_names, storage_from_dict)


class CheckScanNameTest(unittest.TestCase):
    def test_refused(self):
        for name in ('', '.', '..', '../x', 'a/b', '/etc/x', 'a\\b', 'a\0b'):
            with self.subTest(name), self.assertRaises(ValueError):
                check_scan_name(name)

    def test_accepted(self):
        for name in ('scan', 'my invoice', '50%', 'v1.2', '..hidden', 'factură'):
            with self.subTest(name):
                check_scan_name(name)


class PickFileNamesTest(unittest.TestCase):
    def test_names(self):
        for count, existing, expected in (
            (1, [], ['scan.pdf']),
            (2, [], ['scan(1).pdf', 'scan(2).pdf']),
            (1, ['scan.pdf'], ['scan(1).pdf']),
            (2, ['scan.pdf', 'scan(1).pdf'], ['scan(2).pdf', 'scan(3).pdf']),
            (1, ['scan(1).pdf'], ['scan.pdf']),
            (1, ['scan.pdf', 'scan(5).pdf'], ['scan(6).pdf']),     # gaps aren't filled, the newest stays the highest
            (2, ['scan(9).pdf'], ['scan(10).pdf', 'scan(11).pdf']),
            (1, ['scan.pdf', 'scan(1000).pdf'], ['scan(1001).pdf']),
            (1, ['scan.jpeg', 'scan(1).jpeg'], ['scan.pdf']),      # another format is another name
            (2, ['scanner(1).pdf', 'scan1.pdf', 'scan001.pdf', 'oldscan(9).pdf', 'scan(x).pdf'], ['scan(1).pdf', 'scan(2).pdf']),
            (1, ['scan.pdf', 'scan2024.pdf'], ['scan(1).pdf']),    # digits without brackets are just part of another name
        ):
            with self.subTest(count=count, existing=existing):
                files = [f'/tmp/scans/page{index}.pdf' for index in range(count)]
                self.assertEqual(pick_file_names(files, 'scan', set(existing)), expected)

    def test_extension_is_not_doubled(self):
        self.assertEqual(pick_file_names(['/tmp/test.pdf'], 'invoice.pdf', set()), ['invoice.pdf'])
        self.assertEqual(pick_file_names(['/tmp/test.pdf'], 'invoice.pdf', {'invoice.pdf'}), ['invoice(1).pdf'])
        self.assertEqual(pick_file_names(['/tmp/test.pdf'], '.pdf', set()), ['.pdf.pdf'])

    def test_names_with_special_characters(self):
        files = ['/tmp/a.pdf', '/tmp/b.pdf']
        self.assertEqual(pick_file_names(files, '50%', {'50%(1).pdf'}), ['50%(2).pdf', '50%(3).pdf'])
        self.assertEqual(pick_file_names(files, 'a+(b', {'a+(b(1).pdf'}), ['a+(b(2).pdf', 'a+(b(3).pdf'])
        self.assertEqual(pick_file_names(files, 'v1.2', {'v1x2(1).pdf'}), ['v1.2(1).pdf', 'v1.2(2).pdf'])

    def test_keeps_the_extension_of_the_files(self):
        self.assertEqual(pick_file_names(['/tmp/scan001.jpeg'], 'scan', set()), ['scan.jpeg'])


class LocalFolderStorageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.scans = os.path.join(self.folder, 'scans')
        os.makedirs(self.scans)
        patch = mock.patch.object(storage_module, 'SCAN_FOLDER_DEFAULT', self.scans)
        patch.start()
        self.addCleanup(patch.stop)
        self.path = os.path.join(self.folder, 'saved', 'deep')
        self.storage = LocalFolderStorage(self.path)

    def scan_file(self, name='test.pdf', text='scanned'):
        path = os.path.join(self.scans, name)
        Path(path).write_text(text)
        return path

    def test_path_is_resolved_when_created(self):
        with mock.patch.dict(os.environ, {'HOME': '/home/someone'}):
            self.assertEqual(LocalFolderStorage('~/Scans').path, '/home/someone/Scans')
        self.assertEqual(LocalFolderStorage('some/folder').path, os.path.abspath('some/folder'))

    async def test_check_creates_the_folder(self):
        await self.storage.validate_storage_path()
        self.assertTrue(os.path.isdir(self.path))

    async def test_check_refuses_a_file(self):
        path = os.path.join(self.folder, 'file')
        Path(path).write_text('')
        with self.assertRaises(RuntimeError):
            await LocalFolderStorage(path).validate_storage_path()

    @unittest.skipIf(os.geteuid() == 0, 'root can write anywhere')
    async def test_check_refuses_a_folder_it_cant_write_to(self):
        path = os.path.join(self.folder, 'locked')
        os.makedirs(path)
        os.chmod(path, 0o500)
        self.addCleanup(os.chmod, path, 0o700)
        with self.assertRaisesRegex(RuntimeError, 'no permission'):
            await LocalFolderStorage(path).validate_storage_path()

    async def test_check_refuses_the_scans_folder_and_what_is_inside(self):
        link = os.path.join(self.folder, 'link')
        os.symlink(self.scans, link)
        for path in (self.scans, os.path.join(self.scans, 'kept'), link, os.path.join(link, 'kept')):
            with self.subTest(path), self.assertRaisesRegex(RuntimeError, 'temporary folder'):
                await LocalFolderStorage(path).validate_storage_path()
        self.assertEqual(os.listdir(self.scans), [])

    async def test_check_accepts_a_parent_and_a_lookalike_of_the_scans_folder(self):
        await LocalFolderStorage(self.folder).validate_storage_path()
        await LocalFolderStorage(f'{self.scans}-kept').validate_storage_path()

    async def test_save_twice_keeps_both(self):
        await self.storage.validate_storage_path()
        self.assertEqual(await self.storage.save([self.scan_file(text='first')], 'scan'), ['scan.pdf'])
        self.assertEqual(await self.storage.save([self.scan_file(text='second')], 'scan'), ['scan(1).pdf'])
        self.assertEqual(Path(self.path, 'scan.pdf').read_text(), 'first')
        self.assertEqual(Path(self.path, 'scan(1).pdf').read_text(), 'second')

    async def test_save_leaves_the_local_files(self):
        await self.storage.validate_storage_path()
        file = self.scan_file()
        await self.storage.save([file], 'scan')
        self.assertTrue(os.path.exists(file))

    async def test_write_never_overwrites(self):
        await self.storage.validate_storage_path()
        Path(self.path, 'scan.pdf').write_text('precious')
        with self.assertRaises(RuntimeError):
            await self.storage.copy_file_to_target_location(self.scan_file(), 'scan.pdf')
        self.assertEqual(Path(self.path, 'scan.pdf').read_text(), 'precious')

    async def test_failed_write_leaves_no_partial_file(self):
        await self.storage.validate_storage_path()
        with mock.patch.object(storage_module.shutil, 'copyfileobj', side_effect=OSError('disk full')), \
                self.assertRaisesRegex(RuntimeError, 'disk full'):
            await self.storage.copy_file_to_target_location(self.scan_file(), 'scan.pdf')
        self.assertEqual(os.listdir(self.path), [])

    async def test_missing_folder_is_a_runtime_error(self):
        with self.assertRaises(RuntimeError):
            await self.storage.save([self.scan_file()], 'scan')


class FakeCloudStorage(ScanStorage):
    '''What a new kind of location has to implement: nothing but the abstract methods.'''
    def __init__(self, url):
        self.url = url
        self.files = {}

    async def validate_storage_path(self):
        pass

    async def existing_names(self):
        return set(self.files)

    async def copy_file_to_target_location(self, local_path, file_name):
        self.files[file_name] = local_path

    def to_dict(self):
        return {'type': 'cloud', 'url': self.url}

    @classmethod
    def from_dict(cls, data):
        return cls(data['url'])

    def __str__(self):
        return self.url


class NewKindOfStorageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patch = mock.patch.dict(STORAGE_TYPES, {'cloud': FakeCloudStorage})
        patch.start()
        self.addCleanup(patch.stop)

    async def test_inherits_the_naming(self):
        storage = FakeCloudStorage('https://cloud')
        self.assertEqual(await storage.save(['a.pdf'], 'scan'), ['scan.pdf'])
        self.assertEqual(await storage.save(['b.pdf', 'c.pdf'], 'scan'), ['scan(1).pdf', 'scan(2).pdf'])
        self.assertEqual(storage.files, {'scan.pdf': 'a.pdf', 'scan(1).pdf': 'b.pdf', 'scan(2).pdf': 'c.pdf'})

    def test_round_trips_through_the_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'settings.json')
            SettingsRepository(path).save_location = FakeCloudStorage('https://cloud')
            location = SettingsRepository(path).save_location
        self.assertIsInstance(location, FakeCloudStorage)
        self.assertEqual(location.url, 'https://cloud')


class SettingsRepositoryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, 'data', 'settings.json')

    def test_default_location_without_a_file(self):
        location = SettingsRepository(self.path).save_location
        self.assertEqual(str(location), os.path.expanduser(DEFAULT_SAVE_FOLDER))
        self.assertFalse(os.path.exists(self.path))

    def test_location_is_saved(self):
        SettingsRepository(self.path).save_location = LocalFolderStorage('/some/folder')
        location = SettingsRepository(self.path).save_location
        self.assertIsInstance(location, LocalFolderStorage)
        self.assertEqual(location.path, '/some/folder')
        self.assertEqual(json.loads(Path(self.path).read_text()),
                         {'save_location': {'type': 'folder', 'path': '/some/folder'}})

    def test_other_settings_are_kept(self):
        os.makedirs(os.path.dirname(self.path))
        Path(self.path).write_text('{"other": 1}')
        SettingsRepository(self.path).save_location = LocalFolderStorage('/some/folder')
        self.assertEqual(json.loads(Path(self.path).read_text())['other'], 1)

    def test_unknown_kind_is_refused_and_the_file_kept(self):
        os.makedirs(os.path.dirname(self.path))
        content = '{"save_location": {"type": "ftp"}}'
        Path(self.path).write_text(content)
        with self.assertRaisesRegex(ValueError, "'ftp'"):
            SettingsRepository(self.path)
        self.assertEqual(Path(self.path).read_text(), content)

    def test_storage_from_dict(self):
        self.assertEqual(storage_from_dict({'type': 'folder', 'path': '/a'}).path, '/a')
        with self.assertRaises(ValueError):
            storage_from_dict({})


if __name__ == '__main__':
    unittest.main()
