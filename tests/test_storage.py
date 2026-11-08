import io
import json
import os
import tempfile
import unittest
import zipfile
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

    async def test_open_file_reads_what_was_saved(self):
        await self.storage.validate_storage_path()
        await self.storage.save([self.scan_file(text='scanned')], 'scan')
        with await self.storage.open_file('scan.pdf') as file:
            self.assertEqual(file.read(), b'scanned')

    async def test_open_missing_file_is_a_runtime_error(self):
        with self.assertRaisesRegex(RuntimeError, "Can't read 'gone.pdf'"):
            await self.storage.open_file('gone.pdf')

    async def test_delete_removes_only_that_file_and_a_second_time_is_fine(self):
        await self.storage.validate_storage_path()
        await self.storage.save([self.scan_file(), self.scan_file('b.pdf')], 'scan')
        await self.storage.delete_file('scan(1).pdf')
        await self.storage.delete_file('scan(1).pdf')
        self.assertEqual(os.listdir(self.path), ['scan(2).pdf'])

    async def test_failed_delete_is_a_runtime_error(self):
        with mock.patch.object(storage_module.os, 'remove', side_effect=PermissionError('locked')), \
                self.assertRaisesRegex(RuntimeError, 'locked'):
            await self.storage.delete_file('scan.pdf')

    async def test_zip_has_the_files_and_leaves_them_in_place(self):
        await self.storage.validate_storage_path()
        names = await self.storage.save([self.scan_file(text='one'), self.scan_file('b.pdf', 'two')], 'scan')
        with await self.storage.open_as_zip(names) as archive, zipfile.ZipFile(archive) as zip_file:
            self.assertEqual({name: zip_file.read(name) for name in zip_file.namelist()},
                             {'scan(1).pdf': b'one', 'scan(2).pdf': b'two'})
        self.assertEqual(sorted(os.listdir(self.path)), names)

    async def test_zip_of_a_missing_file_is_a_runtime_error(self):
        with self.assertRaisesRegex(RuntimeError, "Can't read"):
            await self.storage.open_as_zip(['gone.pdf'])


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

    async def open_file(self, file_name):
        return io.BytesIO(self.files[file_name].encode())

    async def delete_file(self, file_name):
        self.files.pop(file_name, None)

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

    async def test_inherits_the_zip(self):
        storage = FakeCloudStorage('https://cloud')
        await storage.save(['a.pdf', 'b.pdf'], 'scan')
        with await storage.open_as_zip(['scan(1).pdf', 'scan(2).pdf']) as archive, zipfile.ZipFile(archive) as zip_file:
            self.assertEqual(zip_file.read('scan(2).pdf'), b'b.pdf')

    def test_round_trips_through_the_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'settings.json')
            SettingsRepository(path).save_location = FakeCloudStorage('https://cloud')
            location = SettingsRepository(path).save_location
        self.assertIsInstance(location, FakeCloudStorage)
        self.assertEqual(location.url, 'https://cloud')


class SaveRootTest(unittest.IsolatedAsyncioTestCase):
    '''With SAVE_ROOT set (Docker), save locations are folders inside that root.'''
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = os.path.join(folder.name, 'storage')
        os.makedirs(self.root)
        self.scans = os.path.join(folder.name, 'scans')
        patch = mock.patch.dict(os.environ, {'SAVE_ROOT': self.root})
        patch.start()
        self.addCleanup(patch.stop)
        scans = mock.patch.object(storage_module, 'SCAN_FOLDER_DEFAULT', self.scans)
        scans.start()
        self.addCleanup(scans.stop)

    def test_relative_location_is_appended_to_the_root(self):
        self.assertEqual(LocalFolderStorage('Invoices/2026').path, os.path.join(self.root, 'Invoices', '2026'))

    def test_absolute_location_inside_the_root_is_kept(self):
        self.assertEqual(LocalFolderStorage(os.path.join(self.root, 'a')).path, os.path.join(self.root, 'a'))

    def test_default_location_is_the_root(self):
        self.assertEqual(str(SettingsRepository(os.path.join(self.root, 'data.json')).save_location), self.root)

    def test_display_path_leaves_out_the_root(self):
        self.assertEqual(LocalFolderStorage('Invoices/2026').display_path, os.path.join('Invoices', '2026'))
        self.assertEqual(LocalFolderStorage(os.path.join(self.root, 'a')).display_path, 'a')
        self.assertEqual(LocalFolderStorage('').display_path, '')
        self.assertEqual(LocalFolderStorage('/elsewhere').display_path, '/elsewhere')   # a leftover from before the root

    def test_display_path_is_the_whole_path_without_a_root(self):
        os.environ.pop('SAVE_ROOT')
        self.assertEqual(LocalFolderStorage('/some/folder').display_path, '/some/folder')

    async def test_locations_outside_the_root_are_refused(self):
        for path in ('/elsewhere', os.path.join(self.root, '..', 'escape'), '~/Documents', '../escape'):
            with self.subTest(path), self.assertRaisesRegex(RuntimeError, 'inside'):
                await LocalFolderStorage(path).validate_storage_path()

    async def test_root_and_folders_inside_it_are_accepted(self):
        await LocalFolderStorage('').validate_storage_path()
        await LocalFolderStorage('Invoices').validate_storage_path()
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'Invoices')))

    async def test_without_a_root_any_folder_is_accepted(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SAVE_ROOT')
            folder = os.path.join(os.path.dirname(self.root), 'anywhere')
            self.assertEqual(LocalFolderStorage(folder).path, folder)
            await LocalFolderStorage(folder).validate_storage_path()


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

    def assert_set_aside(self, content):
        os.makedirs(os.path.dirname(self.path))
        Path(self.path).write_text(content)
        with self.assertLogs('printer_backend.json_file', 'WARNING'):
            settings = SettingsRepository(self.path)
        self.assertEqual(str(settings.save_location), os.path.expanduser(DEFAULT_SAVE_FOLDER))
        self.assertEqual(settings.theme, 'light')
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(Path(f'{self.path}.broken').read_text(), content)
        settings.theme = 'dark'     # and it works again
        self.assertEqual(SettingsRepository(self.path).theme, 'dark')

    def test_unknown_kind_sets_the_file_aside(self):
        self.assert_set_aside('{"save_location": {"type": "ftp"}}')

    def test_broken_file_is_set_aside(self):
        self.assert_set_aside('{"theme": "da')

    def test_file_of_the_wrong_kind_is_set_aside(self):
        self.assert_set_aside('["theme"]')

    def test_storage_from_dict(self):
        self.assertEqual(storage_from_dict({'type': 'folder', 'path': '/a'}).path, '/a')
        with self.assertRaises(ValueError):
            storage_from_dict({})


if __name__ == '__main__':
    unittest.main()
