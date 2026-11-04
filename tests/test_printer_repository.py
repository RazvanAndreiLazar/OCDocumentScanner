import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from printer_backend.options import parse_help
from printer_backend.printer import NO_SOURCE, Printer
from printer_backend.printer_repository import PrinterRepository

FIXTURES = Path(__file__).parent / 'fixtures'


def make_printer(device='dev'):
    '''A Printer with the real per-source options of the ENVY 6400.'''
    printer = Printer(device)
    printer.options = {
        'Flatbed': parse_help((FIXTURES / 'envy_6400_airscan_flatbed.txt').read_text()),
        'ADF': parse_help((FIXTURES / 'envy_6400_airscan_adf.txt').read_text()),
    }
    printer.select_source('Flatbed')
    return printer


class SerializationTest(unittest.TestCase):
    def round_trip(self, printer):
        return Printer.from_dict(json.loads(json.dumps(printer.to_dict())))

    def test_keeps_everything(self):
        printer = make_printer()
        printer.select_source('ADF')
        printer.set_option('mode', 'Gray')
        loaded = self.round_trip(printer)
        self.assertEqual(loaded.device, 'dev')
        self.assertEqual(loaded.source, 'ADF')
        self.assertEqual(loaded.options, printer.options)

    def test_changed_options_still_reach_the_scan(self):
        printer = make_printer()
        printer.set_option('mode', 'Gray')
        self.assertEqual(self.round_trip(printer).to_args(), printer.to_args())
        self.assertIn('Gray', printer.to_args())

    def test_device_without_source(self):
        printer = Printer('dev')
        printer.options = {NO_SOURCE: parse_help('    --mode Gray|Color [Color]\n')}
        loaded = self.round_trip(printer)
        self.assertEqual(loaded.source, NO_SOURCE)
        self.assertEqual(loaded.current_options['mode'].value, 'Color')


class SetOptionsTest(unittest.TestCase):
    def test_changes_nothing_if_one_is_refused(self):
        printer = make_printer()
        with self.assertRaises(ValueError):
            printer.set_options({'mode': 'Gray', 'resolution': 'nope'})
        self.assertEqual(printer.current_options['mode'].value, 'Color')

    def test_other_source_is_not_selected(self):
        printer = make_printer()
        printer.set_options({'mode': 'Gray'}, source='ADF')
        self.assertEqual(printer.source, 'Flatbed')
        self.assertEqual(printer.options['ADF']['mode'].value, 'Gray')

    def test_values_are_checked_against_that_source(self):
        # the flatbed goes up to 1200 dpi, the feeder only to 300
        printer = make_printer()
        printer.set_options({'resolution': 1200})
        with self.assertRaises(ValueError):
            printer.set_options({'resolution': 1200}, source='ADF')

    def test_unknown_source(self):
        with self.assertRaises(ValueError):
            make_printer().set_options({'mode': 'Gray'}, source='Tray 2')


class PrinterRepositoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, 'printers.json')
        self.repository = PrinterRepository(self.path)

    def reopened(self):
        return PrinterRepository(self.path)

    async def test_find_new_printers_skips_saved_ones_and_non_devices(self):
        self.repository.add_printer(make_printer('saved'))
        lines = ["device `saved' is a HP ENVY\n", "device `new one' is a HP ENVY  \n", "No scanners were identified\n"]
        with mock.patch.object(Printer, 'discover_printers', mock.AsyncMock(return_value=lines)):
            found = await self.repository.find_new_printers()
        self.assertEqual(found, {'new one': "device `new one' is a HP ENVY"})

    async def test_find_new_printers_passes_a_failed_discovery_on(self):
        with mock.patch.object(Printer, 'discover_printers', mock.AsyncMock(side_effect=RuntimeError('no sane'))):
            with self.assertRaisesRegex(RuntimeError, 'no sane'):
                await self.repository.find_new_printers()

    def test_starts_empty_without_a_file(self):
        self.assertEqual(self.repository.get_all_printers(), [])
        self.assertFalse(os.path.exists(self.path))

    def test_add_is_saved(self):
        self.repository.add_printer(make_printer())
        self.assertEqual(self.reopened().get_printer('dev').options, make_printer().options)

    def test_add_creates_the_folder(self):
        path = os.path.join(os.path.dirname(self.path), 'data', 'printers.json')
        PrinterRepository(path).add_printer(make_printer())
        self.assertEqual(PrinterRepository(path).get_printer('dev').options, make_printer().options)

    def test_add_twice_is_refused(self):
        self.repository.add_printer(make_printer())
        with self.assertRaises(ValueError):
            self.repository.add_printer(make_printer())

    def test_update_is_saved(self):
        self.repository.add_printer(make_printer())
        printer = make_printer()
        printer.select_source('ADF')
        self.repository.update_printer(printer)
        self.assertEqual(self.reopened().get_printer('dev').source, 'ADF')

    def test_update_options_is_saved(self):
        self.repository.add_printer(make_printer())
        self.repository.update_options('dev', {'mode': 'Gray', 'resolution': 600})
        self.repository.update_options('dev', {'resolution': 150}, source='ADF')
        loaded = self.reopened().get_printer('dev')
        self.assertEqual(loaded.source, 'Flatbed')
        self.assertEqual(loaded.options['Flatbed']['mode'].value, 'Gray')
        self.assertEqual(loaded.options['Flatbed']['resolution'].value, '600')
        self.assertEqual(loaded.options['ADF']['mode'].value, 'Color')
        self.assertEqual(loaded.options['ADF']['resolution'].value, '150')

    def test_refused_options_leave_the_file_alone(self):
        self.repository.add_printer(make_printer())
        saved = Path(self.path).read_text()
        with self.assertRaises(ValueError):
            self.repository.update_options('dev', {'mode': 'Gray', 'resolution': 'nope'})
        self.assertEqual(Path(self.path).read_text(), saved)
        self.assertEqual(self.repository.get_printer('dev').options, make_printer().options)

    def test_remove_is_saved(self):
        self.repository.add_printer(make_printer('one'))
        self.repository.add_printer(make_printer('two'))
        self.repository.remove_printer('one')
        self.assertEqual([printer.device for printer in self.reopened().get_all_printers()], ['two'])

    def test_unknown_device_is_refused(self):
        for call in (lambda: self.repository.get_printer('dev'),
                     lambda: self.repository.update_printer(make_printer()),
                     lambda: self.repository.update_options('dev', {'mode': 'Gray'}),
                     lambda: self.repository.remove_printer('dev')):
            with self.assertRaises(ValueError):
                call()
        self.assertFalse(os.path.exists(self.path))

    def test_get_and_update_dont_run_scanimage(self):
        self.repository.add_printer(make_printer())
        with mock.patch('asyncio.create_subprocess_exec') as run:
            self.repository.update_options('dev', {'mode': 'Gray'})
            self.repository.update_printer(self.repository.get_printer('dev'))
        run.assert_not_called()

    def test_broken_file_is_not_overwritten(self):
        Path(self.path).write_text('{not json')
        with self.assertRaises(json.JSONDecodeError):
            self.reopened()
        self.assertEqual(Path(self.path).read_text(), '{not json')

    async def test_available_printers(self):
        for device in ('on', 'off', 'also on'):
            self.repository.add_printer(make_printer(device))

        async def respond(printer):
            return printer.device != 'off'

        with mock.patch.object(Printer, 'is_available', respond):
            available = await self.repository.get_available_printers()
        self.assertEqual([printer.device for printer in available], ['on', 'also on'])


if __name__ == '__main__':
    unittest.main()
