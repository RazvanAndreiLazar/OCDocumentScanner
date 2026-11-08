import asyncio
import contextlib
import io
import os
import sys
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
    def __init__(self, pages_per_batch=2, fail_with=None, endless=False, open_failures=0):
        self.open_failures = open_failures
        self.calls = []
        self.processes = []
        self.pages_per_batch = pages_per_batch
        self.fail_with = fail_with
        self.endless = endless

    @staticmethod
    def write(path):
        Path(path).write_text(os.path.basename(path))   # the name it was written with, to follow renames

    async def __call__(self, *args, **kwargs):
        self.calls.append(list(args))
        if args[0] == 'scanimage' and self.open_failures:
            self.open_failures -= 1
            return FakeProcess(b'scanimage: open of device dev failed: Invalid argument', returncode=1)
        no_documents = False
        if args[0] == 'scanimage':
            no_documents = self.scanimage(args)
        else:
            self.write(args[args.index('-o') + 1])
        if self.fail_with and args[0] == self.fail_with:
            process = FakeProcess(b'it broke', returncode=1)
        elif no_documents:
            process = FakeProcess(b'Document feeder out of documents', returncode=7)
        else:
            process = FakeProcess(endless=self.endless)
        self.processes.append(process)
        return process

    def scanimage(self, args):
        '''Returns True if it exits with 'no documents', like the real one: see the n > batch_start_at check in scanimage.c'''
        if '-o' in args:
            self.write(args[args.index('-o') + 1])
        batch = next((arg.split('=', 1)[1] for arg in args if arg.startswith('--batch=')), None)
        if batch is None:
            return False
        if '%' not in batch:
            self.write(batch)
            return False
        value = lambda name, default: int(next((a.split('=')[1] for a in args if a.startswith(name)), default))
        start, increment = value('--batch-start=', 1), value('--batch-increment=', 1)
        for i in range(self.pages_per_batch):
            self.write(batch % (start + i * increment))
        return self.pages_per_batch == 0 or increment < 0

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
        delay = mock.patch.object(printer_module, 'OPEN_RETRY_DELAY', 0)
        delay.start()
        self.addCleanup(delay.stop)

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

    def use_source(self, source):
        self.printer.options.setdefault(source, self.printer.options['ADF'])
        self.printer.select_source(source)

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
        await drive(self.printer.scan_one_at_a_time(count=1, combine=True))
        self.assertEqual(self.files(), ['scan001.png', 'test.pdf'])

    async def test_nothing_is_cleaned_after_the_scan(self):
        await drive(self.printer.scan_one_at_a_time(count=1, combine=True))
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
    async def test_one_page(self):
        self.printer.set_option('resolution', 300)
        events = await drive(self.printer.scan_one_at_a_time(count=1, combine=True))

        page, document = self.path('scan001.png'), self.path('test.pdf')
        self.assertEqual([e for e in events if not isinstance(e, Progress)],
                         [PageScanned(page, 1), Done([document])])
        self.assertEqual(self.system.of('scanimage'),
                         [['scanimage', '-d', 'dev', '--source', 'Flatbed', '--resolution', '300',
                           '--format', 'png', '-p', '-o', page]])
        self.assertEqual(self.system.of('img2pdf'), [['img2pdf', page, '-o', document]])

    async def test_progress_is_yielded_not_printed(self):
        events = await drive(self.printer.scan_one_at_a_time(count=1))
        self.assertIn(Progress('progress\r'), events)


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
        # 2 sheets: the flipped stack feeds the last one first, so the first back is page 4
        self.assertEqual(Path(self.path('scan004.png')).read_text(), 'back001.png')
        self.assertEqual(Path(self.path('scan002.png')).read_text(), 'back002.png')
        self.assertFalse([name for name in self.files() if name.startswith('back')])
        self.assertEqual(backs[-1], f'--batch={self.path("back%03d.png")}')
        self.assertEqual([e.number for e in events if isinstance(e, PageScanned)], [1, 2, 3, 4])
        self.assertEqual(self.system.of('img2pdf'),
                         [['img2pdf', *[self.path(f'scan00{i}.png') for i in (1, 2, 3, 4)], '-o', self.path('test.pdf')]])

    async def test_fewer_backs_than_fronts(self):
        def one_page_left(event):
            self.system.pages_per_batch = 1

        with self.assertRaisesRegex(RuntimeError, '2 front sides but 1 back sides'):
            await drive(self.printer.scan_all(two_sided=True), one_page_left)
        self.assertFalse(self.system.of('img2pdf'))

    async def test_empty_feeder_for_the_backs(self):
        def empty(event):
            self.system.pages_per_batch = 0

        with self.assertRaisesRegex(RuntimeError, r'scanimage failed \(7\)'):
            await drive(self.printer.scan_all(two_sided=True), empty)

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


class SourceGuardTest(ScanTestCase):
    def test_scan_all_rejects_a_flatbed(self):
        with self.assertRaisesRegex(ValueError, "'Flatbed' holds one page at a time"):
            self.printer.scan_all()
        self.assertEqual(self.system.calls, [])

    def test_one_at_a_time_rejects_feeders(self):
        for source in ('ADF', 'ADF Duplex', 'Automatic Document Feeder'):
            with self.subTest(source):
                self.use_source(source)
                with self.assertRaisesRegex(ValueError, 'feeds the whole stack'):
                    self.printer.scan_one_at_a_time()
        self.assertEqual(self.system.calls, [])

    def test_a_rejected_scan_does_not_empty_the_folder(self):
        os.makedirs(self.folder)
        self.system.write(self.path('scan001.png'))
        with self.assertRaises(ValueError):
            self.printer.scan_all()
        self.assertEqual(self.files(), ['scan001.png'])

    async def test_unknown_source_can_use_both(self):
        self.use_source('Tray 2')
        events = await drive(self.printer.scan_one_at_a_time(count=1))
        self.assertEqual(events[-1], Done([self.path('scan001.pdf')]))
        events = await drive(self.printer.scan_all())
        self.assertEqual(events[-1], Done([self.path('test.pdf')]))


class ScanDispatchTest(ScanTestCase):
    async def test_flatbed_scans_one_at_a_time(self):
        answers = iter([True, False])
        events = await drive(self.printer.scan(), lambda event: next(answers))
        self.assertEqual([e.number for e in events if isinstance(e, PageScanned)], [1, 2])
        # combine defaults to what scan_one_at_a_time defaults to
        self.assertEqual(events[-1], Done([self.path('scan001.pdf'), self.path('scan002.pdf')]))

    async def test_flatbed_arguments(self):
        events = await drive(self.printer.scan(count=2, combine=True, two_sided=True), lambda event: True)
        self.assertEqual([e.number for e in events if isinstance(e, PageScanned)], [1, 2])
        self.assertFalse([e for e in events if isinstance(e, NeedsInput) and e.kind == FLIP])
        self.assertEqual(events[-1], Done([self.path('test.pdf')]))

    async def test_feeder_scans_the_stack(self):
        self.use_source('ADF')
        events = await drive(self.printer.scan(two_sided=True))
        self.assertIn(NeedsInput(FLIP), events)
        self.assertEqual(events[-1], Done([self.path('test.pdf')]))

    async def test_duplex_scans_the_stack(self):
        self.use_source('ADF Duplex')
        events = await drive(self.printer.scan())
        self.assertEqual(len(self.system.of('scanimage')), 1)
        self.assertEqual(events[-1], Done([self.path('test.pdf')]))

    def test_unknown_source_has_to_choose(self):
        self.use_source('Tray 2')
        with self.assertRaisesRegex(ValueError, 'choose how to scan'):
            self.printer.scan()

    async def test_unknown_source_chooses(self):
        self.use_source('Tray 2')
        await drive(self.printer.scan(one_at_a_time=True, count=1))
        self.assertIn('-o', self.system.of('scanimage')[-1])
        events = await drive(self.printer.scan(one_at_a_time=False))
        self.assertTrue([arg for arg in self.system.of('scanimage')[-1] if arg.startswith('--batch=')])
        self.assertEqual(events[-1], Done([self.path('test.pdf')]))

    def test_the_choice_cannot_overrule_a_known_source(self):
        with self.assertRaises(ValueError):
            self.printer.scan(one_at_a_time=False)
        self.use_source('ADF')
        with self.assertRaises(ValueError):
            self.printer.scan(one_at_a_time=True)


class ProblemsTest(ScanTestCase):
    def setUp(self):
        super().setUp()
        self.printer.select_source('ADF')

    def test_arguments_are_checked_when_called(self):
        for call in (lambda: self.printer.scan_all(output_format='bmp'),
                     lambda: self.printer.scan_all(combine=True, output_format='png')):
            with self.assertRaisesRegex(ValueError, 'format|combined'):
                call()
        self.printer.select_source('Flatbed')
        for call in (lambda: self.printer.scan_one_at_a_time(combine=True, output_format='tiff'),
                     lambda: self.printer.scan_one_at_a_time(count=0),
                     lambda: self.printer.scan_one_at_a_time(count=-1)):
            with self.assertRaisesRegex(ValueError, 'combined|count'):
                call()

    async def test_failing_command(self):
        self.system = self.fake_system(fail_with='scanimage')
        with self.assertRaisesRegex(RuntimeError, 'scanimage failed.*it broke'):
            await drive(self.printer.scan_all())

    async def test_other_failures_are_not_tried_again(self):
        self.system = self.fake_system(fail_with='scanimage')
        with self.assertRaises(RuntimeError):
            await drive(self.printer.scan_all())
        self.assertEqual(len(self.system.of('scanimage')), 1)

    async def test_device_that_does_not_open_is_tried_again(self):
        self.system = self.fake_system(open_failures=scanning.OPEN_ATTEMPTS - 1)
        events = await drive(self.printer.scan_all())
        self.assertEqual(len(self.system.of('scanimage')), scanning.OPEN_ATTEMPTS)
        self.assertEqual(events[-1], Done([self.path('test.pdf')]))

    async def test_device_that_never_opens(self):
        self.system = self.fake_system(open_failures=scanning.OPEN_ATTEMPTS)
        with self.assertRaisesRegex(RuntimeError, 'open of device'):
            await drive(self.printer.scan_all())
        self.assertEqual(len(self.system.of('scanimage')), scanning.OPEN_ATTEMPTS)

    async def test_backs_survive_a_device_that_does_not_open(self):
        self.printer.select_source('ADF')

        def fail_next_open(event):
            self.system.open_failures = 1

        events = await drive(self.printer.scan_all(two_sided=True), fail_next_open)
        self.assertEqual(events[-1], Done([self.path('test.pdf')]))
        self.assertEqual([e.number for e in events if isinstance(e, PageScanned)], [1, 2, 3, 4])

    async def test_failing_conversion(self):
        self.system = self.fake_system(fail_with='img2pdf')
        with self.assertRaisesRegex(RuntimeError, 'img2pdf failed'):
            await drive(self.printer.scan_all())

    async def test_nothing_scanned(self):
        self.system = self.fake_system(pages_per_batch=0)
        self.printer.select_source('ADF')
        with self.assertRaisesRegex(RuntimeError, r'scanimage failed \(7\)'):
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
        self.printer.select_source('Flatbed')
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



class IsAvailableTest(unittest.IsolatedAsyncioTestCase):
    OPEN_ERROR = b"scanimage: open of device dev failed: Invalid argument\n"

    def setUp(self):
        delay = mock.patch.object(printer_module, 'OPEN_RETRY_DELAY', 0)
        delay.start()
        self.addCleanup(delay.stop)

    def fake_runs(self, *errors):
        '''One scanimage per entry: None responds, anything else fails with that on stderr.'''
        procs = []
        for error in errors:
            proc = mock.Mock(returncode=0 if error is None else 1)
            proc.communicate = mock.AsyncMock(return_value=(b'', error or b''))
            procs.append(proc)
        run = mock.AsyncMock(side_effect=procs)
        patch = mock.patch.object(printer_module.asyncio, 'create_subprocess_exec', run)
        patch.start()
        self.addCleanup(patch.stop)
        return run

    async def test_responds(self):
        run = self.fake_runs(None)
        self.assertTrue(await Printer('dev').is_available())
        self.assertEqual(run.await_count, 1)

    async def test_open_is_tried_again(self):
        run = self.fake_runs(self.OPEN_ERROR, self.OPEN_ERROR, None)
        self.assertTrue(await Printer('dev').is_available())
        self.assertEqual(run.await_count, 3)

    async def test_gives_up_after_all_attempts(self):
        run = self.fake_runs(*[self.OPEN_ERROR] * scanning.OPEN_ATTEMPTS)
        self.assertFalse(await Printer('dev').is_available())
        self.assertEqual(run.await_count, scanning.OPEN_ATTEMPTS)

    async def test_other_errors_are_not_tried_again(self):
        run = self.fake_runs(b'scanimage: something else\n')
        self.assertFalse(await Printer('dev').is_available())
        self.assertEqual(run.await_count, 1)

    async def test_stuck_scanimage_is_killed(self):
        async def never_answers():
            await asyncio.Event().wait()

        proc = mock.Mock(returncode=None, communicate=never_answers, wait=mock.AsyncMock())
        run = mock.AsyncMock(return_value=proc)
        with mock.patch.object(printer_module.asyncio, 'create_subprocess_exec', run), \
             mock.patch.object(printer_module, 'AVAILABLE_TIMEOUT', 0.01):
            self.assertFalse(await Printer('dev').is_available())
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()

    async def test_killed_scanimage_that_doesnt_finish_is_left_behind(self):
        async def never_answers():
            await asyncio.Event().wait()

        proc = mock.Mock(returncode=None, communicate=never_answers, wait=never_answers)
        run = mock.AsyncMock(return_value=proc)
        with mock.patch.object(printer_module.asyncio, 'create_subprocess_exec', run), \
             mock.patch.object(printer_module, 'AVAILABLE_TIMEOUT', 0.01), \
             mock.patch.object(printer_module, 'KILL_WAIT', 0.01):
            self.assertFalse(await asyncio.wait_for(Printer('dev').is_available(), 5))
        proc.kill.assert_called_once()


class StuckProcess():
    '''A scanimage that ignores terminate and never exits, with a pipe that is never closed.'''
    def __init__(self):
        self.returncode = None
        self.calls = []
        self.stdout = self

    def terminate(self):
        self.calls.append('terminate')

    def kill(self):
        self.calls.append('kill')

    async def wait(self):
        await asyncio.Event().wait()

    async def read(self, size):
        await asyncio.Event().wait()


class StopProcessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patch = mock.patch.object(scanning, 'KILL_WAIT', 0.01)
        patch.start()
        self.addCleanup(patch.stop)

    async def test_stuck_process_is_killed_without_waiting_for_ever(self):
        proc = StuckProcess()
        await asyncio.wait_for(scanning.stop_process(proc), 5)
        self.assertEqual(proc.calls, ['kill'])

    async def test_graceful_stop_falls_back_to_kill(self):
        proc = StuckProcess()
        await asyncio.wait_for(scanning.stop_process(proc, graceful=True), 5)
        self.assertEqual(proc.calls, ['terminate', 'kill'])

    async def test_finished_process_is_left_alone(self):
        proc = StuckProcess()
        proc.returncode = 0
        await scanning.stop_process(proc)
        self.assertEqual(proc.calls, [])

    async def test_cancelling_a_scan_that_ignores_terminate_doesnt_hang(self):
        proc = StuckProcess()
        with mock.patch.object(scanning.subprocess, 'create_subprocess_exec', mock.AsyncMock(return_value=proc)):
            events = scanning.run_command('scanimage')
            task = asyncio.create_task(anext(events))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(events.aclose(), 5)
        self.assertEqual(proc.calls, ['terminate', 'kill'])


class DiscoveryTest(unittest.IsolatedAsyncioTestCase):
    '''Printer.discover_printers against a real child process, since the problems are about its pipes.'''
    def fake_scanimage(self, script: str):
        real = asyncio.create_subprocess_exec   # printer_module.asyncio is this same module, patched below

        async def start(*args, **kwargs):
            return await real(sys.executable, '-c', script, **kwargs)
        patch = mock.patch.object(printer_module.asyncio, 'create_subprocess_exec', start)
        patch.start()
        self.addCleanup(patch.stop)

    async def test_lists_devices(self):
        self.fake_scanimage("print(\"device `a' is a X\")")
        self.assertEqual(await Printer.discover_printers(), ["device `a' is a X\n"])

    async def test_a_lot_of_stderr_doesnt_block_it(self):
        self.fake_scanimage("import sys; sys.stderr.write('x' * 1_000_000); print('device')")
        self.assertEqual(await asyncio.wait_for(Printer.discover_printers(), 10), ['device\n'])

    async def test_failure_reports_stderr(self):
        self.fake_scanimage("import sys; sys.stderr.write('no backend'); sys.exit(3)")
        with self.assertRaisesRegex(RuntimeError, r'\(Err Code: 3\)[\s\S]*no backend'):
            await Printer.discover_printers()

    async def test_hanging_scanimage_times_out_and_is_killed(self):
        self.fake_scanimage('import time; time.sleep(60)')
        with mock.patch.object(printer_module, 'DISCOVER_TIMEOUT', 0.2):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                await asyncio.wait_for(Printer.discover_printers(), 10)


class LoadOptionsTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_stuck_scanimage_times_out_and_is_killed(self):
        async def never_answers():
            await asyncio.Event().wait()

        proc = mock.Mock(returncode=None, communicate=never_answers, wait=mock.AsyncMock())
        with mock.patch.object(printer_module.asyncio, 'create_subprocess_exec', mock.AsyncMock(return_value=proc)), \
             mock.patch.object(printer_module, 'LOAD_TIMEOUT', 0.01):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                await Printer('dev').load_options()
        proc.kill.assert_called_once()


if __name__ == '__main__':
    unittest.main()
