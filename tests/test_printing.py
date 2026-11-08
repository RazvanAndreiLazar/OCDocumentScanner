import asyncio
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from printer_backend import printing
from printer_backend.printer import Printer
from printer_backend.printing import (
    COPIES_MAX, JOB_ABORTED, JOB_CANCELED, JOB_COMPLETED, JOB_PENDING, JOB_PROCESSING, NO_PRINT_SERVICE, JobState,
    check_print_file, convert_for_print, ensure_queue, job_state, print_file_name, printer_capabilities,
    queue_name, resolve_print_uri, submit)

FIXTURES = Path(__file__).parent / 'fixtures' / 'print'
QUEUE = 'ocds-e5a37fdbe391'
JOB = QUEUE + '-12'
ENVY_URI = 'ipp://HPA8B13B03755A.local:631/ipp/print'
ENVY_IP_URI = 'ipp://192.168.0.178/ipp/print'
AIRSCAN = 'airscan:e0:HP ENVY 6400 series [03755A]'
HPAIO = 'hpaio:/net/ENVY_6400_series?ip=192.168.0.178'


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeTools():
    '''Stands in for printing.run_tool: answer(args) returns what the command printed, or raises RuntimeError.'''
    def __init__(self, answer=lambda args: ''):
        self.answer = answer
        self.calls: list[list[str]] = []

    async def __call__(self, *args, timeout, check=True):
        self.calls.append(list(args))
        return self.answer(list(args))

    def called(self, command: str) -> list[list[str]]:
        return [call for call in self.calls if call[0] == command]


class ToolsTestCase(unittest.IsolatedAsyncioTestCase):
    def use(self, answer=lambda args: '') -> FakeTools:
        tools = FakeTools(answer)
        patch = mock.patch.object(printing, 'run_tool', tools)
        patch.start()
        self.addCleanup(patch.stop)
        return tools


class FileNameTest(unittest.TestCase):
    def test_name_is_reduced_to_a_safe_file_name(self):
        for name, stored in (('../../etc/passwd.pdf', 'passwd.pdf'),
                             ('C:\\Users\\me\\Rapport (final).PDF', 'Rapport (final).pdf'),
                             ('a/b\\c.docx', 'c.docx'),
                             ('bad<>name.txt', 'bad__name.txt')):
            with self.subTest(name):
                self.assertEqual(print_file_name(name), stored)

    def test_names_are_never_folders_or_empty(self):
        for name in ('..', '.', '...pdf', '../x.pdf'):
            with self.subTest(name):
                stored = print_file_name(name)
                self.assertNotIn(stored, ('.', '..'))
                self.assertNotIn('/', stored)
        for name in ('', '/'):
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    print_file_name(name)


class MagicBytesTest(unittest.TestCase):
    def write(self, name: str, content: bytes) -> str:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = os.path.join(folder.name, name)
        Path(path).write_bytes(content)
        return path

    def zip_file(self, name: str, entries: dict[str, bytes]) -> str:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = os.path.join(folder.name, name)
        with zipfile.ZipFile(path, 'w') as archive:
            for entry, content in entries.items():
                archive.writestr(entry, content)
        return path

    def test_accepted_files(self):
        cases = [
            ('a.pdf', b'%PDF-1.4\n%....'),
            ('A.PDF', b'%PDF-1.7'),
            ('photo.png', b'\x89PNG\r\n\x1a\n\x00\x00'),
            ('photo.jpg', b'\xff\xd8\xff\xe0JFIF'),
            ('photo.jpeg', b'\xff\xd8\xff\xe1Exif'),
            ('notes.txt', 'Plain text, ünïcode too.'.encode()),
            ('letter.rtf', b'{\\rtf1\\ansi hello}'),
            ('old.doc', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 20),
        ]
        for name, content in cases:
            with self.subTest(name):
                check_print_file(self.write(name, content))
        check_print_file(self.zip_file('a.docx', {'[Content_Types].xml': b'<Types/>', 'word/document.xml': b''}))
        check_print_file(self.zip_file('a.odt', {'mimetype': b'application/vnd.oasis.opendocument.text'}))

    def test_refused_files(self):
        cases = [
            ('a.pdf', b'PK\x03\x04' + b'\x00' * 20),           # a zip named like a PDF
            ('a.pdf', b''),                                    # empty
            ('photo.jpg', b'\x89PNG\r\n\x1a\n'),               # a PNG named like a JPEG
            ('notes.txt', b'\x7fELF\x00\x00binary'),           # binary, with zeros
            ('letter.rtf', b'plain'),
            ('old.doc', b'%PDF-1.4'),
            ('program.exe', b'MZ\x90\x00'),                    # not a type at all
            ('noextension', b'%PDF-1.4'),
        ]
        for name, content in cases:
            with self.subTest(name, content=content[:6]):
                with self.assertRaises(ValueError):
                    check_print_file(self.write(name, content))

    def test_office_zip_needs_its_parts(self):
        with self.subTest('plain zip as docx'):
            with self.assertRaises(ValueError):
                check_print_file(self.zip_file('a.docx', {'readme.txt': b'hi'}))
        with self.subTest('odt with a spreadsheet type'):
            with self.assertRaises(ValueError):
                check_print_file(self.zip_file('a.odt', {'mimetype': b'application/vnd.oasis.opendocument.spreadsheet'}))


class ConversionTest(ToolsTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def test_pdf_and_images_are_printed_as_they_are(self):
        tools = self.use()
        path = os.path.join(self.folder, 'a.pdf')
        self.assertEqual(await_(convert_for_print(path)), path)
        self.assertEqual(tools.calls, [])

    def test_office_documents_are_converted_to_pdf_with_their_own_profile(self):
        path = os.path.join(self.folder, 'letter.docx')
        Path(path).write_bytes(b'x')
        profiles = []

        def answer(args):
            if args[0] == 'soffice':
                profile = args[3].removeprefix('-env:UserInstallation=file://')
                profiles.append(profile)
                os.makedirs(profile)           # what LibreOffice would make
                Path(self.folder, 'letter.pdf').write_bytes(b'%PDF-1.4')
            return ''
        tools = self.use(answer)
        output = await_(convert_for_print(path))
        self.assertEqual(output, os.path.join(self.folder, 'letter.pdf'))
        [soffice] = tools.called('soffice')
        self.assertEqual(soffice[:3], ['soffice', '--headless', '--norestore'])
        self.assertEqual(soffice[-5:-1], ['--convert-to', 'pdf', '--outdir', os.path.dirname(os.path.abspath(path))])
        self.assertEqual(soffice[-1], os.path.abspath(path))
        self.assertFalse(os.path.exists(profiles[0]))       # the profile is removed afterwards

    def test_conversion_that_makes_no_pdf_fails(self):
        path = os.path.join(self.folder, 'broken.odt')
        Path(path).write_bytes(b'x')
        self.use()
        with self.assertRaises(RuntimeError):
            await_(convert_for_print(path))


class ResolveUriTest(ToolsTestCase):
    def answer(self, found: str = fixture('ippfind_envy.txt'), ipptool_ok: bool = True):
        def answer(args):
            if args[0] == 'ippfind':
                return found
            if args[0] == 'ipptool' and not ipptool_ok:
                raise RuntimeError("'ipptool' failed (Err Code: 1)")
            return 'PASS'
        return answer

    def test_airscan_name_is_matched_exactly(self):
        tools = self.use(self.answer())
        self.assertEqual(await_(resolve_print_uri(AIRSCAN)), ENVY_URI)
        self.assertEqual(tools.called('ipptool')[0][-2], ENVY_URI)

    def test_airscan_name_that_is_only_a_prefix_is_not_a_match(self):
        tools = self.use(self.answer())
        with self.assertRaises(RuntimeError):
            await_(resolve_print_uri('airscan:e0:HP ENVY 6400 series'))
        self.assertEqual(tools.called('ipptool'), [])

    def test_airscan_printer_not_on_the_network(self):
        self.use(self.answer(found=''))
        with self.assertRaises(RuntimeError) as context:
            await_(resolve_print_uri(AIRSCAN))
        self.assertEqual(str(context.exception), NO_PRINT_SERVICE)

    def test_hpaio_and_hp_addresses_are_the_ip(self):
        for device in (HPAIO, 'hp:/net/ENVY_6400_series?ip=192.168.0.178'):
            with self.subTest(device):
                self.use(self.answer())
                self.assertEqual(await_(resolve_print_uri(device)), ENVY_IP_URI)

    def test_escl_address_is_the_host(self):
        self.use(self.answer())
        self.assertEqual(await_(resolve_print_uri('escl:http://192.168.0.5:8080')), 'ipp://192.168.0.5/ipp/print')

    def test_printer_that_does_not_answer_at_its_address(self):
        self.use(self.answer(ipptool_ok=False))
        with self.assertRaises(RuntimeError) as context:
            await_(resolve_print_uri(HPAIO))
        self.assertEqual(str(context.exception), NO_PRINT_SERVICE)

    def test_unknown_kind_of_device(self):
        self.use(self.answer())
        with self.assertRaises(RuntimeError):
            await_(resolve_print_uri('epson2:net:192.168.0.5'))


class QueueTest(ToolsTestCase):
    def test_queue_name_is_stable_and_cups_safe(self):
        name = queue_name(AIRSCAN)
        self.assertRegex(name, r'^ocds-[0-9a-f]{12}$')
        self.assertEqual(name, queue_name(AIRSCAN))
        self.assertNotEqual(name, queue_name(HPAIO))

    def test_queue_that_already_prints_to_the_address_is_left_alone(self):
        tools = self.use(lambda args: {'lpstat': fixture('lpstat_v_envy.txt'),
                                       'ipptool': fixture('ipp_queue_envy.txt')}.get(args[0], ''))
        self.assertEqual(await_(ensure_queue(AIRSCAN, ENVY_URI)), (QUEUE, ENVY_URI))
        self.assertEqual(tools.called('lpadmin'), [])

    def test_missing_queue_is_made_driverless(self):
        tools = self.use(lambda args: '' if args[0] == 'lpstat' else '')
        queue, uri = await_(ensure_queue(AIRSCAN, ENVY_URI))
        self.assertEqual(queue, QUEUE)
        self.assertEqual(tools.called('lpadmin'), [['lpadmin', '-p', QUEUE, '-E', '-v', ENVY_URI, '-m', 'everywhere']])

    def test_unknown_address_is_looked_up_first(self):
        tools = self.use(lambda args: fixture('ippfind_envy.txt') if args[0] == 'ippfind' else '')
        self.assertEqual(await_(ensure_queue(AIRSCAN, None)), (QUEUE, ENVY_URI))
        self.assertEqual(len(tools.called('lpadmin')), 1)

    def test_changed_address_is_found_again_once(self):
        state = {'lpadmin': 0}
        hpaio_queue = queue_name(HPAIO)

        def answer(args):
            if args[0] == 'lpstat':
                return f'device for {hpaio_queue}: ipp://192.168.0.49/ipp/print\n'     # an older address
            if args[0] == 'lpadmin':
                state['lpadmin'] += 1
                if state['lpadmin'] == 1:
                    raise RuntimeError("'lpadmin' failed")
            return 'PASS'
        tools = self.use(answer)
        self.assertEqual(await_(ensure_queue(HPAIO, 'ipp://192.168.0.50/ipp/print')), (hpaio_queue, ENVY_IP_URI))
        adds = [call for call in tools.called('lpadmin') if call[1] == '-p']
        self.assertEqual(len(adds), 2)
        self.assertEqual(adds[-1][:3], ['lpadmin', '-p', hpaio_queue])
        self.assertEqual(adds[-1][5], ENVY_IP_URI)

    def test_queue_left_without_ppd_is_made_again(self):
        # what CUPS leaves when lpadmin -m everywhere failed: the address is set, but it is a raw queue
        tools = self.use(lambda args: {'lpstat': fixture('lpstat_v_envy.txt'),
                                       'ipptool': fixture('ipp_queue_no_ppd.txt')}.get(args[0], ''))
        await_(ensure_queue(AIRSCAN, ENVY_URI))
        self.assertEqual(tools.called('lpadmin'), [['lpadmin', '-p', QUEUE, '-E', '-v', ENVY_URI, '-m', 'everywhere']])

    def test_failed_lpadmin_removes_the_half_made_queue(self):
        def answer(args):
            if args[:2] == ['lpadmin', '-p']:
                raise RuntimeError("'lpadmin' failed\nlpadmin: Unable to connect")
            return 'PASS' if args[0] == 'ipptool' else ''
        tools = self.use(answer)
        with self.assertRaises(RuntimeError):
            await_(ensure_queue(HPAIO, ENVY_IP_URI))
        self.assertIn(['lpadmin', '-x', queue_name(HPAIO)], tools.called('lpadmin'))

    def test_printer_that_is_gone_is_an_error(self):
        def answer(args):
            if args[0] == 'lpadmin':
                raise RuntimeError("'lpadmin' failed")
            if args[0] == 'ipptool':
                return 'PASS'
            return ''
        self.use(answer)
        with self.assertRaises(RuntimeError):
            await_(ensure_queue(HPAIO, ENVY_IP_URI))


class SubmitTest(ToolsTestCase):
    def test_lp_gets_every_argument_as_a_list_with_the_path_after_dashes(self):
        tools = self.use(lambda args: fixture('lp_accepted.txt'))
        job = await_(submit(QUEUE, '/app/data/prints/x/a.pdf', 'a.pdf', 2, 'two-sided-long-edge', 'monochrome'))
        self.assertEqual(job, JOB)
        self.assertEqual(tools.calls, [[
            'lp', '-d', QUEUE, '-t', 'a.pdf', '-n', '2', '-o', 'sides=two-sided-long-edge',
            '-o', 'print-color-mode=monochrome', '--', '/app/data/prints/x/a.pdf']])

    def test_values_outside_the_allowlists_are_refused_before_lp(self):
        cases = [
            (QUEUE, 0, 'one-sided', 'color'),
            (QUEUE, COPIES_MAX + 1, 'one-sided', 'color'),
            (QUEUE, True, 'one-sided', 'color'),
            (QUEUE, '2', 'one-sided', 'color'),
            (QUEUE, 1, 'two-sided', 'color'),
            (QUEUE, 1, 'one-sided', 'auto'),
            ('-d ocds-x', 1, 'one-sided', 'color'),
            ('ocds-e5a37fdbe391; rm', 1, 'one-sided', 'color'),
        ]
        for queue, copies, sides, color in cases:
            with self.subTest(queue=queue, copies=copies, sides=sides, color=color):
                tools = self.use(lambda args: fixture('lp_accepted.txt'))
                with self.assertRaises(ValueError):
                    await_(submit(queue, 'a.pdf', 'a.pdf', copies, sides, color))
                self.assertEqual(tools.calls, [])

    def test_title_control_characters_are_dropped(self):
        tools = self.use(lambda args: fixture('lp_accepted.txt'))
        await_(submit(QUEUE, 'a.pdf', 'bad\ntitle\x00', 1, 'one-sided', 'color'))
        self.assertEqual(tools.calls[0][4], 'badtitle')

    def test_lp_without_a_job_id_is_an_error(self):
        self.use(lambda args: 'something else')
        with self.assertRaises(RuntimeError):
            await_(submit(QUEUE, 'a.pdf', 'a.pdf', 1, 'one-sided', 'color'))


class CapabilitiesTest(ToolsTestCase):
    '''
    The fixtures are what CUPS answered (ipptool, as a non-root user, through the socket) for a driverless
    queue of the real ENVY, and of an ippeveprinter without duplexer or colour.
    '''
    def test_cups_is_asked_through_its_socket(self):
        tools = self.use(lambda args: fixture('ipp_queue_envy.txt'))
        with mock.patch.dict(os.environ, {'CUPS_SERVER': '/run/cups/cups.sock'}):
            await_(printer_capabilities(QUEUE))
        self.assertEqual(tools.calls, [['ipptool', '-tv', f'ipp://%2Frun%2Fcups%2Fcups.sock/printers/{QUEUE}',
                                        'get-printer-attributes.test']])

    def test_envy_offers_its_sides_and_colours(self):
        self.use(lambda args: fixture('ipp_queue_envy.txt'))
        capabilities = await_(printer_capabilities(QUEUE))
        self.assertEqual(capabilities.sides, ['one-sided', 'two-sided-long-edge', 'two-sided-short-edge'])
        self.assertEqual(capabilities.sides_default, 'one-sided')
        self.assertEqual(capabilities.color_modes, ['color', 'monochrome'])
        self.assertEqual(capabilities.color_default, 'color')
        self.assertEqual(capabilities.copies_max, COPIES_MAX)      # CUPS says 1-9999; the app keeps its own limit

    def test_simplex_mono_printer_offers_one_value_each(self):
        self.use(lambda args: fixture('ipp_queue_mono_simplex.txt'))
        capabilities = await_(printer_capabilities(QUEUE))
        self.assertEqual(capabilities.sides, ['one-sided'])
        self.assertEqual(capabilities.color_modes, ['monochrome'])
        self.assertEqual(capabilities.color_default, 'monochrome')

    def test_smaller_copies_limit_of_the_queue_is_kept(self):
        self.use(lambda args: fixture('ipp_queue_envy.txt').replace('= 1-9999', '= 1-10'))
        self.assertEqual(await_(printer_capabilities(QUEUE)).copies_max, 10)

    def test_values_the_app_cannot_ask_for_are_left_out(self):
        self.use(lambda args: fixture('ipp_queue_envy.txt').replace(
            '= monochrome,color', '= auto,auto-monochrome,process-monochrome,color'))
        self.assertEqual(await_(printer_capabilities(QUEUE)).color_modes, ['color'])

    def test_cups_without_an_answer_is_an_error(self):
        self.use(lambda args: 'ipptool: Unable to connect to "/run/cups/cups.sock"')
        with self.assertRaises(RuntimeError):
            await_(printer_capabilities(QUEUE))


class JobStateTest(ToolsTestCase):
    '''The fixtures are ipptool answers captured from a real cupsd printing to ippeveprinter.'''
    def test_ipptool_asks_cups_through_its_socket_for_that_job(self):
        tools = self.use(lambda args: fixture('ipp_job_pending.txt'))
        with mock.patch.dict(os.environ, {'CUPS_SERVER': '/run/cups/cups.sock'}):
            await_(job_state(JOB))
        self.assertEqual(tools.calls, [['ipptool', '-tv', '-d', 'job_id=12',
                                        f'ipp://%2Frun%2Fcups%2Fcups.sock/printers/{QUEUE}', printing.JOB_STATE_TEST]])
        self.assertTrue(os.path.isfile(printing.JOB_STATE_TEST))

    def test_pending_job(self):
        self.use(lambda args: fixture('ipp_job_pending.txt'))
        self.assertEqual(await_(job_state(JOB)), JobState(JOB_PENDING, ''))

    def test_pending_job_says_why_it_waits(self):
        self.use(lambda args: fixture('ipp_job_waiting_offline.txt'))
        state = await_(job_state(JOB))
        self.assertEqual(state.state, JOB_PENDING)
        self.assertIn('The printer may not exist or is unavailable', state.reason)
        self.assertIn('printer-stopped', state.reason)

    def test_job_being_printed(self):
        self.use(lambda args: fixture('ipp_job_processing.txt'))
        self.assertEqual(await_(job_state(JOB)), JobState(JOB_PROCESSING, 'Printing.'))

    def test_finished_job(self):
        self.use(lambda args: fixture('ipp_job_completed.txt'))
        self.assertEqual(await_(job_state(JOB)), JobState(JOB_COMPLETED, ''))

    def test_cancelled_job_is_not_mistaken_for_a_printed_one(self):
        self.use(lambda args: fixture('ipp_job_canceled.txt'))
        self.assertEqual(await_(job_state(JOB)).state, JOB_CANCELED)

    def test_aborted_job(self):
        self.use(lambda args: fixture('ipp_job_completed.txt').replace('= completed', '= aborted')
                 .replace('job-completed-successfully', 'job-aborted-by-system'))
        self.assertEqual(await_(job_state(JOB)), JobState(JOB_ABORTED, 'job-aborted-by-system'))

    def test_job_cups_no_longer_knows_counts_as_completed(self):
        self.use(lambda args: fixture('ipp_job_not_found.txt'))
        self.assertEqual(await_(job_state(JOB)), JobState(JOB_COMPLETED, ''))

    def test_answer_without_a_state_is_an_error(self):
        self.use(lambda args: 'ipptool: Unable to connect to "/run/cups/cups.sock"')
        with self.assertRaises(RuntimeError):
            await_(job_state(JOB))

    def test_job_ids_of_other_queues_are_refused(self):
        tools = self.use()
        for job_id in ('ENVY_6400-3', QUEUE + '-x', QUEUE):
            with self.subTest(job_id=job_id), self.assertRaises(ValueError):
                await_(job_state(job_id))
        self.assertEqual(tools.calls, [])

    def test_cancel_runs_cancel_with_the_job_id(self):
        tools = self.use()
        await_(printing.cancel(JOB))
        self.assertEqual(tools.calls, [['cancel', JOB]])


class PrinterUriTest(unittest.TestCase):
    def test_printer_saved_before_printing_has_no_address(self):
        data = {'device': AIRSCAN, 'source': '', 'options': {}}
        self.assertIsNone(Printer.from_dict(data).print_uri)

    def test_address_is_saved(self):
        printer = Printer(AIRSCAN)
        printer.print_uri = ENVY_URI
        self.assertEqual(Printer.from_dict(printer.to_dict()).print_uri, ENVY_URI)


class CleanPrintsFolderTest(unittest.IsolatedAsyncioTestCase):
    async def test_everything_in_the_folder_is_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, 'job', 'nested'))
            Path(folder, 'job', 'a.pdf').write_bytes(b'x')
            Path(folder, 'loose.pdf').write_bytes(b'x')
            with mock.patch.object(printing, 'PRINT_FOLDER_DEFAULT', folder):
                await printing.clean_prints_folder()
            self.assertEqual(os.listdir(folder), [])


def await_(awaitable):
    '''Runs a coroutine in the test's loop (the tests of one class share no loop, so each call makes its own).'''
    return asyncio.run(awaitable)


if __name__ == '__main__':
    unittest.main()
