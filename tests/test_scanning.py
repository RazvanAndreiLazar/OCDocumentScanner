import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from printer_backend import printer as printer_module
from printer_backend import scanning
from printer_backend.options import parse_help
from printer_backend.printer import Printer
from printer_backend.scanning import Done, NeedsInput, PageScanned, Progress, FLIP, NEXT_PAGE

FIXTURES = Path(__file__).parent / 'fixtures'


def fake_scanimage(output: bytes):
    proc = mock.Mock()
    proc.communicate = mock.AsyncMock(return_value=(output, None))
    return mock.AsyncMock(return_value=proc)


class ParseVersionTest(unittest.TestCase):
    def test_versions(self):
        for text, version in (
            ('scanimage (sane-backends) 1.2.1-debian; backend version 1.2.1', '1.2.1'),
            ('scanimage (sane-backends) 1.3; backend version 1.3.0', '1.3'),
            ('scanimage (sane-backends) 1.10.12', '1.10.12'),
            ('something else', None),
            ('', None),
        ):
            with self.subTest(text):
                self.assertEqual(scanning.parse_scanimage_version(text), version)


class InitTest(unittest.IsolatedAsyncioTestCase):
    async def init_with(self, output: bytes):
        stderr = io.StringIO()
        with mock.patch.object(scanning.subprocess, 'create_subprocess_exec', fake_scanimage(output)), \
                contextlib.redirect_stderr(stderr):
            version = await scanning.init()
        return version, stderr.getvalue()

    async def test_matching_version_is_silent(self):
        version, warning = await self.init_with(
            f'scanimage (sane-backends) {scanning.SCANIMG_VERSION}-debian; backend version 1.2.1'.encode())
        self.assertEqual(version, scanning.SCANIMG_VERSION)
        self.assertEqual(warning, '')

    async def test_other_version_warns(self):
        version, warning = await self.init_with(b'scanimage (sane-backends) 1.4.0; backend version 1.4.0')
        self.assertEqual(version, '1.4.0')
        self.assertIn('1.4.0', warning)
        self.assertIn(scanning.SCANIMG_VERSION, warning)

    async def test_unreadable_output_warns(self):
        version, warning = await self.init_with(b'garbage')
        self.assertIsNone(version)
        self.assertIn('could not read', warning)

    async def test_missing_scanimage(self):
        with mock.patch.object(scanning.subprocess, 'create_subprocess_exec',
                               mock.AsyncMock(side_effect=FileNotFoundError)):
            with self.assertRaisesRegex(RuntimeError, 'sane-utils'):
                await scanning.init()



class FakeProcess():
    def __init__(self, output=b'progress\r', returncode=0, endless=False):
        self.stdout = self
        self.returncode = None
        self.terminated = False
        self._output, self._final, self._endless = output, returncode, endless

    async def read(self, size):
        if self._endless:
            return b'.'
        output, self._output = self._output, b''
        return output

    async def wait(self):
        if self.returncode is None:
            self.returncode = self._final
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15


class FakeSystem():
    '''Stands in for scanimage and img2pdf: records the calls and creates the files they would write.'''
    def __init__(self, pages_per_batch=2, fail_with=None, endless=False):
        self.calls = []
        self.processes = []
        self.pages_per_batch = pages_per_batch
        self.fail_with = fail_with
        self.endless = endless

    @staticmethod
    def write(path):
        Path(path).write_bytes(b'x')

    async def __call__(self, *args, **kwargs):
        self.calls.append(list(args))
        if args[0] == 'scanimage':
            self.scanimage(args)
        else:
            self.write(args[args.index('-o') + 1])
        if self.fail_with and args[0] == self.fail_with:
            process = FakeProcess(b'it broke', returncode=1)
        else:
            process = FakeProcess(endless=self.endless)
        self.processes.append(process)
        return process

    def scanimage(self, args):
        if '-o' in args:
            self.write(args[args.index('-o') + 1])
        batch = next((arg.split('=', 1)[1] for arg in args if arg.startswith('--batch=')), None)
        if batch is None:
            return
        if '%' not in batch:
            return self.write(batch)
        value = lambda name, default: int(next((a.split('=')[1] for a in args if a.startswith(name)), default))
        start, increment = value('--batch-start=', 1), value('--batch-increment=', 1)
        for i in range(self.pages_per_batch):
            self.write(batch % (start + i * increment))

    def of(self, program):
        return [call for call in self.calls if call[0] == program]


async def drive(gen, answer=lambda event: None):
    '''What a front end does: collects the events and answers the NeedsInput ones.'''
    events = []
    event = await anext(gen)
    while True:
        events.append(event)
        try:
            event = await gen.asend(answer(event) if isinstance(event, NeedsInput) else None)
        except StopAsyncIteration:
            return events


class ScanTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = os.path.join(folder.name, 'scans')
        patch = mock.patch.object(printer_module, 'SCAN_FOLDER_DEFAULT', self.folder)
        patch.start()
        self.addCleanup(patch.stop)

        self.printer = Printer('dev')
        self.printer.options = {
            'Flatbed': parse_help((FIXTURES / 'envy_6400_hpaio_flatbed.txt').read_text()),
            'ADF': parse_help((FIXTURES / 'envy_6400_hpaio_adf.txt').read_text()),
        }
        self.printer.select_source('Flatbed')
        self.system = self.fake_system()

    def fake_system(self, **kwargs):
        system = FakeSystem(**kwargs)
        patch = mock.patch.object(scanning.subprocess, 'create_subprocess_exec', system)
        patch.start()
        self.addCleanup(patch.stop)
        return system

    def path(self, name):
        return os.path.join(self.folder, name)

    def files(self):
        return sorted(os.listdir(self.folder))


class FolderTest(ScanTestCase):
    async def test_clean_creates_the_folder(self):
        await Printer.clean_scans_folder()
        self.assertEqual(self.files(), [])

    async def test_clean_empties_the_folder(self):
        os.makedirs(os.path.join(self.folder, 'old', 'deeper'))
        self.system.write(self.path('scan001.png'))
        self.system.write(self.path('old/deeper/x.txt'))
        await Printer.clean_scans_folder()
        self.assertEqual(self.files(), [])

    async def test_a_new_scan_starts_by_cleaning(self):
        os.makedirs(self.folder)
        self.system.write(self.path('scan007.png'))
        self.system.write(self.path('notes.txt'))
        await drive(self.printer.scan_all())
        self.assertEqual(self.files(), ['scan001.png', 'test.pdf'])

    async def test_nothing_is_cleaned_after_the_scan(self):
        await drive(self.printer.scan_all())
        self.assertEqual(self.files(), ['scan001.png', 'test.pdf'])


class ScanArgsTest(ScanTestCase):
    def test_only_the_changed_options_are_sent(self):
        self.assertEqual(self.printer.to_args(), ['--source', 'Flatbed'])
        self.printer.set_option('resolution', 300)
        self.printer.set_option('mode', 'Gray')
        self.assertEqual(self.printer.to_args(),
                         ['--source', 'Flatbed', '--mode', 'Gray', '--resolution', '300'])

    def test_an_option_set_back_to_its_default_is_not_sent(self):
        self.printer.set_option('resolution', 300)
        self.printer.set_option('resolution', 75)
        self.assertEqual(self.printer.to_args(), ['--source', 'Flatbed'])

    def test_each_source_has_its_own_changes(self):
        self.printer.set_option('resolution', 1200)
        self.printer.select_source('ADF')
        self.assertEqual(self.printer.to_args(), ['--source', 'ADF'])

    def test_set_option_keeps_what_the_device_listed(self):
        self.printer.set_option('resolution', 300)
        self.assertEqual(self.printer.current_options['resolution'].default, '75')


class FlatbedTest(ScanTestCase):
    async def test_scan_all_scans_one_page(self):
        self.printer.set_option('resolution', 300)
        events = await drive(self.printer.scan_all())

        page, document = self.path('scan001.png'), self.path('test.pdf')
        self.assertEqual([e for e in events if not isinstance(e, Progress)],
                         [PageScanned(page, 1), Done([document])])
        self.assertEqual(self.system.of('scanimage'),
                         [['scanimage', '-d', 'dev', '--source', 'Flatbed', '--resolution', '300',
                           '--format', 'png', '-p', '-o', page]])
        self.assertEqual(self.system.of('img2pdf'), [['img2pdf', page, '-o', document]])

    async def test_progress_is_yielded_not_printed(self):
        events = await drive(self.printer.scan_all())
        self.assertIn(Progress('progress\r'), events)

    async def test_two_sided_has_no_effect(self):
        events = await drive(self.printer.scan_all(two_sided=True))
        self.assertFalse([e for e in events if isinstance(e, NeedsInput)])
        self.assertEqual(len(self.system.of('scanimage')), 1)


class OneAtATimeTest(ScanTestCase):
    async def test_until_the_user_stops(self):
        answers = iter([True, True, False])
        events = await drive(self.printer.scan_one_at_a_time(), lambda event: next(answers))

        self.assertEqual([e for e in events if isinstance(e, NeedsInput)], [NeedsInput(NEXT_PAGE)] * 3)
        self.assertEqual([e.number for e in events if isinstance(e, PageScanned)], [1, 2, 3])
        # combine=False: a pdf for every page
        self.assertEqual(events[-1], Done([self.path(f'scan00{i}.pdf') for i in (1, 2, 3)]))

    async def test_combined_into_one_document(self):
        answers = iter([True, False])
        events = await drive(self.printer.scan_one_at_a_time(combine=True), lambda event: next(answers))
        self.assertEqual(events[-1], Done([self.path('test.pdf')]))
        self.assertEqual(self.system.of('img2pdf'),
                         [['img2pdf', self.path('scan001.png'), self.path('scan002.png'), '-o', self.path('test.pdf')]])

    async def test_a_caller_that_never_answers_gets_one_page(self):
        events = [event async for event in self.printer.scan_one_at_a_time()]
        self.assertEqual([e.number for e in events if isinstance(e, PageScanned)], [1])

    async def test_count(self):
        events = await drive(self.printer.scan_one_at_a_time(count=2), lambda event: True)
        self.assertEqual([e.number for e in events if isinstance(e, PageScanned)], [1, 2])
        self.assertEqual(len([e for e in events if isinstance(e, NeedsInput)]), 1)

    async def test_other_formats_are_scanned_directly(self):
        events = await drive(self.printer.scan_one_at_a_time(count=1, output_format='jpeg'))
        self.assertEqual(events[-1], Done([self.path('scan001.jpeg')]))
        self.assertFalse(self.system.of('img2pdf'))
        self.assertIn(['--format', 'jpeg'], [call[call.index('--format'):call.index('--format') + 2]
                                             for call in self.system.of('scanimage')])


class FeederTest(ScanTestCase):
    def setUp(self):
        super().setUp()
        self.printer.select_source('ADF')

    async def test_one_sided(self):
        events = await drive(self.printer.scan_all())
        self.assertFalse([e for e in events if isinstance(e, NeedsInput)])
        self.assertEqual(self.system.of('scanimage'),
                         [['scanimage', '-d', 'dev', '--source', 'ADF', '--format', 'png', '-p',
                           f'--batch={self.path("scan%03d.png")}']])
        self.assertEqual(events[-1], Done([self.path('test.pdf')]))

    async def test_two_sided_turns_the_stack_around_in_between(self):
        seen = []
        events = await drive(self.printer.scan_all(two_sided=True),
                             lambda event: seen.append(len(self.system.of('scanimage'))))

        self.assertEqual([e for e in events if isinstance(e, NeedsInput)], [NeedsInput(FLIP)])
        self.assertEqual(seen, [1])     # asked after the fronts, before the backs
        fronts, backs = self.system.of('scanimage')
        self.assertEqual(fronts[-3:], [f'--batch={self.path("scan%03d.png")}', '--batch-start=1', '--batch-increment=2'])
        # 2 sheets: the backs are 4 and 2, the flipped stack feeds the last one first
        self.assertEqual(backs[-3:], [f'--batch={self.path("scan%03d.png")}', '--batch-start=4', '--batch-increment=-2'])
        self.assertEqual([e.number for e in events if isinstance(e, PageScanned)], [1, 2, 3, 4])
        self.assertEqual(self.system.of('img2pdf'),
                         [['img2pdf', *[self.path(f'scan00{i}.png') for i in (1, 2, 3, 4)], '-o', self.path('test.pdf')]])

    async def test_the_options_of_the_source_are_used(self):
        self.printer.set_option('resolution', 300)
        await drive(self.printer.scan_all())
        self.assertEqual(self.system.of('scanimage')[0][3:7], ['--source', 'ADF', '--resolution', '300'])

    async def test_png_pages_are_kept_without_combining(self):
        events = await drive(self.printer.scan_all(combine=False, output_format='png'))
        self.assertEqual(events[-1], Done([self.path('scan001.png'), self.path('scan002.png')]))
        self.assertFalse(self.system.of('img2pdf'))

    async def test_unknown_source_is_tried_like_a_feeder(self):
        self.printer.options['Tray 2'] = self.printer.options['ADF']
        self.printer.select_source('Tray 2')
        events = await drive(self.printer.scan_all())
        self.assertTrue([e for e in events if isinstance(e, Progress) and "don't know the source" in e.text])
        self.assertEqual(events[-1], Done([self.path('test.pdf')]))


class DuplexTest(ScanTestCase):
    def setUp(self):
        super().setUp()
        self.printer.options['ADF Duplex'] = self.printer.options['ADF']
        self.printer.select_source('ADF Duplex')

    async def test_the_whole_document_is_scanned_by_scanimage(self):
        events = await drive(self.printer.scan_all(two_sided=True))
        self.assertFalse([e for e in events if isinstance(e, NeedsInput)])
        self.assertEqual(self.system.of('scanimage'),
                         [['scanimage', '-d', 'dev', '--source', 'ADF Duplex', '--format', 'pdf', '-p',
                           f'--batch={self.path("test.pdf")}']])
        self.assertFalse(self.system.of('img2pdf'))
        self.assertEqual(events[-1], Done([self.path('test.pdf')]))

    async def test_pages_when_not_combined(self):
        events = await drive(self.printer.scan_all(combine=False))
        self.assertEqual(events[-1], Done([self.path('scan001.pdf'), self.path('scan002.pdf')]))


class ProblemsTest(ScanTestCase):
    def test_arguments_are_checked_when_called(self):
        for call in (lambda: self.printer.scan_all(output_format='bmp'),
                     lambda: self.printer.scan_all(combine=True, output_format='png'),
                     lambda: self.printer.scan_one_at_a_time(combine=True, output_format='tiff'),
                     lambda: self.printer.scan_one_at_a_time(count=0),
                     lambda: self.printer.scan_one_at_a_time(count=-1)):
            with self.assertRaises(ValueError):
                call()

    async def test_failing_command(self):
        self.system = self.fake_system(fail_with='scanimage')
        with self.assertRaisesRegex(RuntimeError, 'scanimage failed.*it broke'):
            await drive(self.printer.scan_all())

    async def test_failing_conversion(self):
        self.system = self.fake_system(fail_with='img2pdf')
        with self.assertRaisesRegex(RuntimeError, 'img2pdf failed'):
            await drive(self.printer.scan_all())

    async def test_nothing_scanned(self):
        self.system = self.fake_system(pages_per_batch=0)
        self.printer.select_source('ADF')
        with self.assertRaisesRegex(RuntimeError, 'Nothing was scanned'):
            await drive(self.printer.scan_all())

    async def test_missing_program(self):
        with mock.patch.object(scanning.subprocess, 'create_subprocess_exec',
                               mock.AsyncMock(side_effect=FileNotFoundError)):
            with self.assertRaisesRegex(RuntimeError, 'not found'):
                await drive(self.printer.scan_all())

    async def test_closing_stops_the_running_scan(self):
        self.system = self.fake_system(endless=True)
        gen = self.printer.scan_all()
        self.assertIsInstance(await anext(gen), Progress)
        await gen.aclose()
        self.assertTrue(self.system.processes[0].terminated)

    async def test_closing_while_waiting_for_the_user(self):
        gen = self.printer.scan_one_at_a_time()
        events = []
        async for event in gen:
            events.append(event)
            if isinstance(event, NeedsInput):
                break
        await gen.aclose()
        self.assertEqual(len(self.system.of('scanimage')), 1)


class SourceKindTest(unittest.TestCase):
    def test_kinds(self):
        for source, kind in (('Flatbed', 'flatbed'), ('flatbed', 'flatbed'), ('ADF', 'document-feeder'),
                             ('Automatic Document Feeder', 'document-feeder'), ('Duplex', 'duplex'),
                             ('ADF Duplex', 'duplex'), ('Tray 2', None), ('', None)):
            with self.subTest(source):
                self.assertEqual(scanning.source_kind(source), kind)


if __name__ == '__main__':
    unittest.main()
