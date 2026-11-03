import contextlib
import io
import unittest
from unittest import mock

from main_cli import CLI_APP
from printer_backend.scanning import Done, FLIP, NEXT_PAGE, NeedsInput, PageScanned, Progress


class FakePrinter():
    def __init__(self, source='Flatbed'):
        self.source = source
        self.calls = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        return 'scan'


class CliTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = CLI_APP()
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
    def test_enter_quits(self):
        for source in ('Flatbed', 'ADF', 'Tray 2'):
            with self.subTest(source):
                self.app.current_printer = FakePrinter(source)
                self.typing('')
                self.assertIsNone(self.app.choose_scan())
                self.assertEqual(self.app.current_printer.calls, [])

    def test_flatbed_leaves_the_method_to_the_printer(self):
        self.typing('s', '', 'y')
        self.assertEqual(self.app.choose_scan(), 'scan')
        self.assertEqual(self.app.current_printer.calls,
                         [{'one_at_a_time': None, 'combine': True, 'output_format': 'pdf', 'two_sided': False}])

    def test_feeder_asks_for_both_sides(self):
        self.app.current_printer = FakePrinter('ADF')
        self.typing('s', 'pdf', 'n', 'y')
        self.app.choose_scan()
        self.assertEqual(self.app.current_printer.calls,
                         [{'one_at_a_time': None, 'combine': False, 'output_format': 'pdf', 'two_sided': True}])

    def test_duplex_does_not_ask_for_both_sides(self):
        self.app.current_printer = FakePrinter('ADF Duplex')
        self.typing('s', '', '')
        self.app.choose_scan()
        self.assertEqual(self.app.current_printer.calls,
                         [{'one_at_a_time': None, 'combine': True, 'output_format': 'pdf', 'two_sided': False}])

    def test_known_sources_do_not_offer_the_two_options(self):
        for source in ('Flatbed', 'ADF', 'ADF Duplex'):
            with self.subTest(source):
                self.app.current_printer = FakePrinter(source)
                self.typing('0', '1', '')
                self.assertIsNone(self.app.choose_scan())
                self.assertEqual(self.app.current_printer.calls, [])
        self.assertNotIn('one page at a time', self.output.getvalue())

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
        self.typing('s', 'png', '')
        self.app.choose_scan()
        self.assertEqual(self.app.current_printer.calls,
                         [{'one_at_a_time': None, 'combine': False, 'output_format': 'png', 'two_sided': False}])

    def test_bad_answers_are_asked_again(self):
        self.typing('7', 's', 'bmp', 'jpeg')
        self.app.choose_scan()
        self.assertEqual(self.app.current_printer.calls[0]['output_format'], 'jpeg')
        self.assertIn('Invalid choice', self.output.getvalue())
        self.assertIn('not supported', self.output.getvalue())


class RunTest(CliTestCase):
    async def test_a_rejected_scan_shows_the_message_and_goes_back(self):
        async def no_selection():
            pass

        self.app.current_printer.scan = mock.Mock(side_effect=ValueError('wrong kind of source'))
        with mock.patch.object(self.app, 'select_printer', no_selection), \
                mock.patch.object(self.app, 'configure_printer') as configure:
            self.typing('s', '', '', '')    # rejected, then Enter to quit
            await self.app.run()
        self.assertIn('wrong kind of source', self.output.getvalue())
        self.assertEqual(configure.call_count, 2)


class RunScanTest(CliTestCase):
    async def test_shows_the_events_and_answers_them(self):
        replies = []

        async def scan():
            yield Progress('working')
            yield PageScanned('scans/scan001.png', 1)
            replies.append((yield NeedsInput(NEXT_PAGE)))
            replies.append((yield NeedsInput(NEXT_PAGE)))
            yield NeedsInput(FLIP)
            yield Done(['scans/test.pdf'])

        self.typing('', 's', '')
        await self.app.run_scan(scan())
        self.assertEqual(replies, [True, False])
        for text in ('working', "Page 1 saved in 'scans/scan001.png'", 'Done: scans/test.pdf'):
            self.assertIn(text, self.output.getvalue())

    async def test_failures_are_shown(self):
        async def scan():
            yield Progress('working')
            raise RuntimeError('scanimage failed')

        await self.app.run_scan(scan())
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
            await self.app.run_scan(scan())
        self.assertEqual(closed, [True])


if __name__ == '__main__':
    unittest.main()
