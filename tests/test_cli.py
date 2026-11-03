import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from main_cli import CLI_APP
from printer_backend import printer as printer_module
from printer_backend.options import parse_help
from printer_backend.printer import Printer
from printer_backend.printer_repository import PrinterRepository
from printer_backend.settings_repository import SettingsRepository
from printer_backend.storage import LocalFolderStorage
from printer_backend.scanning import Done, FLIP, NEXT_PAGE, NeedsInput, PageScanned, Progress

FIXTURES = Path(__file__).parent / 'fixtures'


def make_printer(device='dev'):
    printer = Printer(device)
    printer.options = {
        'Flatbed': parse_help((FIXTURES / 'envy_6400_airscan_flatbed.txt').read_text()),
        'ADF': parse_help((FIXTURES / 'envy_6400_airscan_adf.txt').read_text()),
    }
    printer.select_source('Flatbed')
    return printer


class FakePrinter():
    def __init__(self, source='Flatbed'):
        self.device = 'dev'
        self.source = source
        self.calls = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        return 'scan'


class CliTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, 'printers.json')
        self.repository = PrinterRepository(self.path)
        # nothing here may touch the real ./scans, data/ or ~/Documents
        self.scans = os.path.join(folder.name, 'scans')
        os.makedirs(self.scans)
        scans = mock.patch.object(printer_module, 'SCAN_FOLDER_DEFAULT', self.scans)
        scans.start()
        self.addCleanup(scans.stop)
        self.saved = os.path.join(folder.name, 'saved')
        self.settings_path = os.path.join(folder.name, 'settings.json')
        self.settings = SettingsRepository(self.settings_path)
        self.settings.save_location = LocalFolderStorage(self.saved)
        self.app = CLI_APP(self.repository, self.settings)
        self.app.current_printer = FakePrinter()
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def typing(self, *answers):
        patch = mock.patch('builtins.input', side_effect=list(answers))
        patch.start()
        self.addCleanup(patch.stop)


class ChooseScanTest(CliTestCase):
    def test_enter_goes_back_from_an_unknown_source(self):
        self.app.current_printer = FakePrinter('Tray 2')
        self.typing('')
        self.assertIsNone(self.app.choose_scan())
        self.assertEqual(self.app.current_printer.calls, [])

    def test_flatbed_leaves_the_method_to_the_printer(self):
        self.typing('', 'y')
        self.assertEqual(self.app.choose_scan(), 'scan')
        self.assertEqual(self.app.current_printer.calls,
                         [{'one_at_a_time': None, 'combine': True, 'output_format': 'pdf', 'two_sided': False}])

    def test_feeder_asks_for_both_sides(self):
        self.app.current_printer = FakePrinter('ADF')
        self.typing('pdf', 'n', 'y')
        self.app.choose_scan()
        self.assertEqual(self.app.current_printer.calls,
                         [{'one_at_a_time': None, 'combine': False, 'output_format': 'pdf', 'two_sided': True}])

    def test_duplex_does_not_ask_for_both_sides(self):
        self.app.current_printer = FakePrinter('ADF Duplex')
        self.typing('', '')
        self.app.choose_scan()
        self.assertEqual(self.app.current_printer.calls,
                         [{'one_at_a_time': None, 'combine': True, 'output_format': 'pdf', 'two_sided': False}])

    def test_known_sources_start_with_the_format(self):
        for source in ('Flatbed', 'ADF', 'ADF Duplex'):
            with self.subTest(source):
                self.app.current_printer = FakePrinter(source)
                # nothing else is asked first: png is not combined, and only the feeder asks for both sides
                self.typing('png', 'n')
                self.assertEqual(self.app.choose_scan(), 'scan')
                self.assertEqual(self.app.current_printer.calls[0]['output_format'], 'png')
        self.assertNotIn('one page at a time', self.output.getvalue())
        self.assertNotIn('start scanning', self.output.getvalue())
        self.assertIn(f"saving in '{self.saved}'", self.output.getvalue())

    def test_unknown_source_chooses_the_method(self):
        self.app.current_printer = FakePrinter('Tray 2')
        self.typing('0', '', '', '1', '', '')
        self.app.choose_scan()
        self.app.choose_scan()
        self.assertEqual(self.app.current_printer.calls,
                         [{'one_at_a_time': True, 'combine': False, 'output_format': 'pdf', 'two_sided': False},
                          {'one_at_a_time': False, 'combine': True, 'output_format': 'pdf', 'two_sided': False}])
        self.assertIn('one page at a time', self.output.getvalue())

    def test_other_formats_are_not_combined(self):
        self.app.current_printer = FakePrinter('ADF')
        self.typing('png', '')
        self.app.choose_scan()
        self.assertEqual(self.app.current_printer.calls,
                         [{'one_at_a_time': None, 'combine': False, 'output_format': 'png', 'two_sided': False}])

    def test_bad_answers_are_asked_again(self):
        self.app.current_printer = FakePrinter('Tray 2')
        self.typing('7', '0', 'bmp', 'jpeg')
        self.app.choose_scan()
        self.assertEqual(self.app.current_printer.calls[0]['output_format'], 'jpeg')
        self.assertIn('Invalid choice', self.output.getvalue())
        self.assertIn('not supported', self.output.getvalue())


class SelectPrinterTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.app.current_printer = None

    def availability(self, available):
        patch = mock.patch.object(Printer, 'is_available', mock.AsyncMock(return_value=available))
        patch.start()
        self.addCleanup(patch.stop)

    async def test_selects_a_saved_printer(self):
        self.repository.add_printer(make_printer('one'))
        self.repository.add_printer(make_printer('two'))
        self.availability(True)
        self.typing('x', '7', '1')
        await self.app.select_printer()
        self.assertEqual(self.app.current_printer.device, 'two')
        self.assertIn('not numeric', self.output.getvalue())
        self.assertIn('out of range', self.output.getvalue())
        self.assertNotIn('Warning', self.output.getvalue())

    async def test_unavailable_printer_warns_and_is_still_selected(self):
        self.repository.add_printer(make_printer('one'))
        self.availability(False)
        self.typing('0')
        await self.app.select_printer()
        self.assertEqual(self.app.current_printer.device, 'one')
        self.assertIn("Warning: 'one' is not available", self.output.getvalue())

    async def test_enter_selects_nothing(self):
        self.repository.add_printer(make_printer('one'))
        self.typing('')
        await self.app.select_printer()
        self.assertIsNone(self.app.current_printer)

    async def test_without_saved_printers_offers_to_add_one(self):
        with mock.patch.object(self.app, 'add_printer', mock.AsyncMock()) as add:
            await self.app.select_printer()
        add.assert_awaited_once()
        self.assertIsNone(self.app.current_printer)


class AddPrinterTest(CliTestCase):
    def discovering(self, *devices, extra=()):
        lines = [f"device `{device}' is a HP ENVY scanner\n" for device in devices] + list(extra)
        patch = mock.patch.object(Printer, 'discover_printers', mock.AsyncMock(return_value=lines))
        patch.start()
        self.addCleanup(patch.stop)

    def loading_options(self):
        async def load_options(printer):
            printer.options = make_printer().options
            printer.select_source('Flatbed')

        patch = mock.patch.object(Printer, 'load_options', load_options)
        patch.start()
        self.addCleanup(patch.stop)

    async def test_saved_printers_are_not_offered(self):
        self.repository.add_printer(make_printer('saved'))
        self.discovering('saved', 'new one', 'new two')
        self.loading_options()
        self.typing('1')
        await self.app.add_printer()
        self.assertEqual([printer.device for printer in PrinterRepository(self.path).get_all_printers()],
                         ['saved', 'new two'])
        self.assertNotIn("`saved'", self.output.getvalue())

    async def test_other_lines_are_not_offered(self):
        self.discovering(extra=['No scanners were identified.\n'])
        await self.app.add_printer()
        self.assertIn('No new printers found', self.output.getvalue())

    async def test_nothing_new(self):
        self.repository.add_printer(make_printer('saved'))
        self.discovering('saved')
        await self.app.add_printer()
        self.assertIn('No new printers found', self.output.getvalue())

    async def test_enter_adds_nothing(self):
        self.discovering('new')
        self.typing('')
        await self.app.add_printer()
        self.assertEqual(self.repository.get_all_printers(), [])

    async def test_failed_discovery_is_shown(self):
        with mock.patch.object(Printer, 'discover_printers', mock.AsyncMock(side_effect=RuntimeError('no sane'))):
            await self.app.add_printer()
        self.assertIn('no sane', self.output.getvalue())

    async def test_printer_that_cant_be_read_is_not_saved(self):
        self.discovering('new')
        self.typing('0')
        with mock.patch.object(Printer, 'load_options', mock.AsyncMock(side_effect=RuntimeError('open failed'))):
            await self.app.add_printer()
        self.assertIn('open failed', self.output.getvalue())
        self.assertEqual(self.repository.get_all_printers(), [])


class ConfigurePrinterTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.app.current_printer = make_printer()
        self.repository.add_printer(self.app.current_printer)

    def test_changes_are_saved(self):
        self.typing('resolution 600', 'source ADF', 'mode Gray', '')
        self.app.configure_printer()
        saved = PrinterRepository(self.path).get_printer('dev')
        self.assertEqual(saved.source, 'ADF')
        self.assertEqual(saved.options['Flatbed']['resolution'].value, '600')
        self.assertEqual(saved.options['ADF']['mode'].value, 'Gray')

    def test_refused_values_are_shown_and_not_saved(self):
        self.typing('resolution 5', 'source Tray', '')
        self.app.configure_printer()
        saved = PrinterRepository(self.path).get_printer('dev')
        self.assertEqual(saved.source, 'Flatbed')
        self.assertEqual(saved.current_options['resolution'].value, '300')
        self.assertIn("'5' is not valid", self.output.getvalue())


class RunTest(CliTestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch('main_cli.init', mock.AsyncMock())
        self.init = patch.start()
        self.addCleanup(patch.stop)
        self.app.current_printer = make_printer('one')
        self.repository.add_printer(self.app.current_printer)
        self.repository.add_printer(make_printer('two'))
        available = mock.patch.object(Printer, 'is_available', mock.AsyncMock(return_value=True))
        available.start()
        self.addCleanup(available.stop)

    async def test_runs_init_and_enter_quits_the_selection(self):
        self.app.current_printer = None
        self.typing('')
        await self.app.run()
        self.init.assert_awaited_once()
        self.assertIsNone(self.app.current_printer)

    async def test_enter_quits_the_menu(self):
        self.typing('')
        await self.app.run()
        self.assertEqual(self.app.current_printer.device, 'one')

    async def test_invalid_choice_asks_again(self):
        self.typing('9', '')
        await self.app.run()
        self.assertIn('Invalid choice', self.output.getvalue())

    async def test_add_new_printer(self):
        with mock.patch.object(self.app, 'add_printer', mock.AsyncMock()) as add:
            self.typing('1', '')
            await self.app.run()
        add.assert_awaited_once()
        self.assertEqual(self.app.current_printer.device, 'one')

    async def test_change_printer(self):
        self.typing('2', '1', '')
        await self.app.run()
        self.assertEqual(self.app.current_printer.device, 'two')

    async def test_configure(self):
        self.typing('3', 'mode Gray', '', '')
        await self.app.run()
        self.assertEqual(PrinterRepository(self.path).get_printer('one').current_options['mode'].value, 'Gray')

    async def test_scan(self):
        with mock.patch.object(self.app, 'choose_scan', return_value='scan'), \
                mock.patch.object(self.app, 'run_scan', mock.AsyncMock()) as run_scan:
            self.typing('4', 'invoice', '')
            await self.app.run()
        run_scan.assert_awaited_once_with('scan', 'invoice')

    async def test_scan_name_defaults_and_bad_names_are_asked_again(self):
        with mock.patch.object(self.app, 'choose_scan', return_value='scan'), \
                mock.patch.object(self.app, 'run_scan', mock.AsyncMock()) as run_scan:
            self.typing('4', '../invoice', '.', '', '')
            await self.app.run()
        run_scan.assert_awaited_once_with('scan', 'scan')
        self.assertIn("'../invoice' can't be used", self.output.getvalue())

    async def test_unusable_location_stops_before_scanning(self):
        Path(self.saved).write_text('a file, not a folder')
        with mock.patch.object(self.app, 'choose_scan') as choose_scan:
            self.typing('4', '')
            await self.app.run()
        choose_scan.assert_not_called()
        self.assertIn("Can't save in", self.output.getvalue())

    async def test_change_save_location(self):
        new = os.path.join(self.saved, 'new')
        self.typing('5', new, '')
        await self.app.run()
        self.assertEqual(str(SettingsRepository(self.settings_path).save_location), new)
        self.assertTrue(os.path.isdir(new))

    async def test_enter_keeps_the_save_location(self):
        self.typing('5', '', '')
        await self.app.run()
        self.assertEqual(str(SettingsRepository(self.settings_path).save_location), self.saved)

    async def test_unusable_save_location_is_not_kept(self):
        with mock.patch('printer_backend.storage.SCAN_FOLDER_DEFAULT', self.scans):
            self.typing('5', os.path.join(self.scans, 'inside'), '')
            await self.app.run()
        self.assertEqual(str(SettingsRepository(self.settings_path).save_location), self.saved)
        self.assertIn('temporary folder', self.output.getvalue())

    async def test_a_rejected_scan_shows_the_message_and_goes_back(self):
        self.app.current_printer = FakePrinter()
        self.app.current_printer.scan = mock.Mock(side_effect=ValueError('wrong kind of source'))
        self.typing('4', '', '', '')    # rejected, then Enter to quit
        await self.app.run()
        self.assertIn('wrong kind of source', self.output.getvalue())

    async def test_delete_goes_back_to_the_selection(self):
        for key in ('d', 'D'):
            with self.subTest(key):
                self.repository.add_printer(make_printer('extra'))
                self.app.current_printer = self.repository.get_printer('extra')
                self.typing(key, 'y', '')
                await self.app.run()
                self.assertIsNone(self.app.current_printer)
                self.assertEqual([printer.device for printer in PrinterRepository(self.path).get_all_printers()],
                                 ['one', 'two'])

    async def test_five_does_not_delete(self):
        self.typing('5', '', '')    # asks for a new save location, Enter keeps it
        await self.app.run()
        self.assertEqual(len(PrinterRepository(self.path).get_all_printers()), 2)
        self.assertNotIn('Delete', self.output.getvalue())

    async def test_delete_can_be_refused(self):
        self.typing('d', '', '')
        await self.app.run()
        self.assertEqual(self.app.current_printer.device, 'one')
        self.assertEqual(len(PrinterRepository(self.path).get_all_printers()), 2)


class RunScanTest(CliTestCase):
    async def test_shows_the_events_and_answers_them(self):
        replies = []

        async def scan():
            yield Progress('working')
            yield PageScanned('scans/scan001.png', 1)
            replies.append((yield NeedsInput(NEXT_PAGE)))
            replies.append((yield NeedsInput(NEXT_PAGE)))
            yield NeedsInput(FLIP)

        self.typing('', 's', '')
        await self.app.run_scan(scan(), 'scan')
        self.assertEqual(replies, [True, False])
        for text in ('working', 'Page 1 scanned'):
            self.assertIn(text, self.output.getvalue())

    def finished_scan(self, *names):
        '''A scan that leaves these files and a page in the temporary folder.'''
        Path(self.scans, 'scan001.png').write_text('page')
        files = [os.path.join(self.scans, name) for name in names]
        for file in files:
            Path(file).write_text(os.path.basename(file))

        async def scan():
            yield Done(files)
        return scan()

    async def test_only_the_final_files_are_saved_and_the_temporary_folder_is_emptied(self):
        os.makedirs(self.saved)
        await self.app.run_scan(self.finished_scan('test.pdf'), 'invoice')
        self.assertEqual(os.listdir(self.saved), ['invoice.pdf'])
        self.assertEqual(Path(self.saved, 'invoice.pdf').read_text(), 'test.pdf')
        self.assertEqual(os.listdir(self.scans), [])
        self.assertIn(f"Saved in '{self.saved}': invoice.pdf", self.output.getvalue())

    async def test_several_files_are_numbered_in_order(self):
        os.makedirs(self.saved)
        await self.app.run_scan(self.finished_scan('scan001.pdf', 'scan002.pdf'), 'invoice')
        self.assertEqual(Path(self.saved, 'invoice(1).pdf').read_text(), 'scan001.pdf')
        self.assertEqual(Path(self.saved, 'invoice(2).pdf').read_text(), 'scan002.pdf')

    async def test_failed_save_keeps_the_temporary_files(self):
        # the location was removed after the check at the start of the scan
        await self.app.run_scan(self.finished_scan('test.pdf'), 'invoice')
        self.assertIn('Saving failed', self.output.getvalue())
        self.assertEqual(sorted(os.listdir(self.scans)), ['scan001.png', 'test.pdf'])

    async def test_failed_scan_saves_nothing(self):
        async def scan():
            yield Done([os.path.join(self.scans, 'test.pdf')])
            raise RuntimeError('scanimage failed')

        with mock.patch.object(self.app, 'save_scan', mock.AsyncMock()) as save_scan:
            await self.app.run_scan(scan(), 'invoice')
        save_scan.assert_not_called()

    async def test_failures_are_shown(self):
        async def scan():
            yield Progress('working')
            raise RuntimeError('scanimage failed')

        await self.app.run_scan(scan(), 'scan')
        self.assertIn('Scan failed: scanimage failed', self.output.getvalue())

    async def test_stopping_early_closes_the_scan(self):
        closed = []

        async def scan():
            try:
                yield Progress('working')
            finally:
                closed.append(True)

        with mock.patch.object(self.app, 'handle_event', side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            await self.app.run_scan(scan(), 'scan')
        self.assertEqual(closed, [True])


if __name__ == '__main__':
    unittest.main()
