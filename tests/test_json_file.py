import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from printer_backend import json_file


class WriteJsonTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, 'data', 'file.json')

    def test_data_is_flushed_to_disk_before_it_replaces_the_file(self):
        events = []
        real_replace = os.replace
        with mock.patch.object(json_file.os, 'fsync', lambda fd: events.append('fsync')), \
             mock.patch.object(json_file.os, 'replace',
                               lambda a, b: (events.append('replace'), real_replace(a, b))):
            json_file.write_json(self.path, {'a': 1})
        # the file, then the move, then the folder that holds the move
        self.assertEqual(events, ['fsync', 'replace', 'fsync'])
        self.assertEqual(json_file.read_json(self.path, None), {'a': 1})
        self.assertFalse(os.path.exists(f'{self.path}.tmp'))

    def test_missing_file_gives_the_default(self):
        self.assertEqual(json_file.read_json(self.path, 'default'), 'default')
        self.assertFalse(os.path.exists(f'{self.path}.broken'))

    def test_older_broken_file_is_replaced(self):
        os.makedirs(os.path.dirname(self.path))
        Path(f'{self.path}.broken').write_text('old')
        Path(self.path).write_text('{')
        with self.assertLogs('printer_backend.json_file', 'WARNING'):
            self.assertEqual(json_file.read_json(self.path, 'default'), 'default')
        self.assertEqual(Path(f'{self.path}.broken').read_text(), '{')

    def test_bytes_that_are_not_text_are_set_aside(self):
        os.makedirs(os.path.dirname(self.path))
        Path(self.path).write_bytes(b'\xff\xfe\x00')
        with self.assertLogs('printer_backend.json_file', 'WARNING'):
            self.assertEqual(json_file.read_json(self.path, 'default'), 'default')


if __name__ == '__main__':
    unittest.main()
