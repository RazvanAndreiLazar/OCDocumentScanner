import html
import io
import os
import re
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient
    from web_frontend.app import create_app
except ImportError:     # the web front end has its own requirements, see web_frontend/requirements.txt
    TestClient = None

from web_frontend.device_ids import device_name, device_to_id, id_to_device
from printer_backend.options import parse_help
from printer_backend.printer import Printer
from printer_backend.printer_repository import PrinterRepository
from printer_backend.printing import COPIES_MAX, JOB_COMPLETED, JOB_PENDING, JobState, PrintCapabilities
from printer_backend.scanning import Done, NEXT_PAGE, NeedsInput, PageScanned
from printer_backend.settings_repository import SettingsRepository
from printer_backend.storage import LocalFolderStorage

FIXTURES = Path(__file__).parent / 'fixtures'
DEVICE = 'hpaio:/net/ENVY_6400_series?ip=192.168.0.178'


class DeviceIdTest(unittest.TestCase):
    def test_round_trip(self):
        for device in (DEVICE, 'airscan:e0:HP ENVY 6400', 'a', 'ü/?#%'):
            with self.subTest(device):
                device_id = device_to_id(device)
                self.assertRegex(device_id, r'^[A-Za-z0-9_-]+$')    # nothing a URL path would split on
                self.assertEqual(id_to_device(device_id), device)

    def test_foreign_id_is_refused(self):
        with self.assertRaises(ValueError):
            id_to_device('%%%')

    def test_name_to_show(self):
        for device, name in ((DEVICE, 'ENVY 6400 series'),
                             ('hpaio:/usb/Deskjet_3700?serial=X1', 'Deskjet 3700'),
                             ('airscan:e0:HP ENVY 6400 [A1]', 'HP ENVY 6400 [A1]'),
                             ('epson2:net:192.168.0.5', 'epson2:net:192.168.0.5')):
            with self.subTest(device):
                self.assertEqual(device_name(device), name)


@unittest.skipIf(TestClient is None, 'fastapi is not installed')
class WebTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.repository = PrinterRepository(str(Path(folder.name) / 'printers.json'))
        self.settings = SettingsRepository(str(Path(folder.name) / 'settings.json'))
        self.settings.save_location = LocalFolderStorage(str(self.folder / 'saved'))

        printer = Printer(DEVICE)
        printer.options = {'Flatbed': parse_help((FIXTURES / 'envy_6400_airscan_flatbed.txt').read_text())}
        printer.select_source('Flatbed')
        self.repository.add_printer(printer)

        # the app empties the prints folder at startup: not the real one, which may hold files being printed
        for patch in (mock.patch('web_frontend.app.init', mock.AsyncMock()),
                      mock.patch('web_frontend.app.clean_prints_folder', mock.AsyncMock())):
            patch.start()
            self.addCleanup(patch.stop)
        # entered: background scans only keep running while the client's event loop does
        self.client = TestClient(create_app(self.repository, self.settings))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)


@unittest.skipIf(TestClient is None, 'fastapi is not installed')
class WebRoutesTest(WebTestCase):
    def test_printer_list_shows_the_saved_printer(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('ENVY 6400 series', response.text)
        self.assertIn(DEVICE, response.text)
        self.assertIn('Scan', response.text)
        self.assertIn('href="/printers/discover"', response.text)

    def test_home_without_printers_can_only_search(self):
        self.repository.remove_printer(DEVICE)
        page = self.client.get('/').text
        self.assertIn('No printers added yet', page)
        self.assertIn('disabled>Scan', page)
        self.assertIn('href="/printers/discover"', page)

    def test_static_files_are_served(self):
        for path in ('app.css', 'app.js', 'vendor/htmx.min.js', 'vendor/archivo/archivo-latin-wght.woff2'):
            with self.subTest(path):
                self.assertEqual(self.client.get(f'/static/{path}').status_code, 200)

    def test_colours_are_only_written_down_in_the_palette(self):
        css = self.client.get('/static/app.css').text
        palette, rest = css.split('/* ---------- 2.')
        self.assertEqual(len(re.findall(r'--color-\w+: #[0-9a-f]{6};', palette)), 8)      # four for each colour scheme
        self.assertEqual(re.findall(r':[^;{}]*#[0-9a-fA-F]{3,8}\b[^;{}]*;', rest), [])
        self.assertNotRegex(rest, r'\b(rgb|rgba|hsl|hsla)\(')

    def test_cross_site_post_is_refused(self):
        for headers in ({'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'http://evil.example'}):
            with self.subTest(headers):
                self.assertEqual(self.client.post('/', headers=headers).status_code, 403)

    def test_same_site_post_gets_through_to_the_route(self):
        # there is no POST route on '/': 405 means the middleware let it pass
        self.assertEqual(self.client.post('/', headers={'Origin': 'http://testserver'}).status_code, 405)

    def test_backend_errors_are_shown_inline(self):
        @self.client.app.get('/boom')
        async def boom():
            raise ValueError('not valid')
        response = self.client.get('/boom')
        self.assertEqual(response.status_code, 422)
        self.assertIn('not valid', response.text)


@unittest.skipIf(TestClient is None, 'fastapi is not installed')
class WebScanTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.id = device_to_id(DEVICE)
        self.script = []            # what the next scan yields
        self.scans = []             # the arguments of the scans that were started

        def scan(printer, **kwargs):
            Printer._check_scan_arguments(kwargs['combine'], kwargs['output_format'])   # refuses like the real one
            self.scans.append((printer, kwargs))

            async def events():
                for event in self.script:
                    yield event
            return events()
        for patch in (mock.patch.object(Printer, 'scan', scan),
                      mock.patch.object(Printer, 'clean_scans_folder', mock.AsyncMock())):
            patch.start()
            self.addCleanup(patch.stop)

        self.document = self.folder / 'a.pdf'
        self.document.write_bytes(b'%PDF')

    def add_printer(self, device, source):
        printer = Printer(device)
        printer.options = {source: parse_help((FIXTURES / 'envy_6400_airscan_flatbed.txt').read_text())}
        printer.select_source(source)
        self.repository.add_printer(printer)
        return device_to_id(device)

    def start(self, **form):
        return self.client.post('/scan', data={'device_id': self.id, **form})

    def wait_for(self, text):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            page = self.client.get('/scan/status').text
            if text in page:
                return page
            time.sleep(0.02)
        self.fail(f"'{text}' never showed up, the page says:\n{page}")

    def test_form_of_a_flatbed(self):
        page = self.client.get(f'/printers/{self.id}').text
        self.assertIn('Start scan', page)
        self.assertNotIn('two_sided', page)
        self.assertNotIn('name="how"', page)

    def test_form_of_a_feeder_offers_both_sides(self):
        page = self.client.get(f'/printers/{self.add_printer("feeder", "ADF")}').text
        self.assertIn('two_sided', page)

    def test_form_of_an_unknown_source_asks_how_to_scan(self):
        page = self.client.get(f'/printers/{self.add_printer("odd", "Platen Thing")}').text
        self.assertIn('name="how"', page)

    def test_scan_runs_and_is_saved(self):
        self.script = [PageScanned('a.png', 1), Done([str(self.document)])]
        response = self.start(name='invoice', output_format='pdf')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['HX-Redirect'], '/scan')
        self.assertIn('invoice.pdf', self.wait_for('Saved'))
        self.assertEqual((self.folder / 'saved' / 'invoice.pdf').read_bytes(), b'%PDF')

        printer, kwargs = self.scans[0]
        self.assertEqual(printer.device, DEVICE)
        self.assertEqual(kwargs, {'one_at_a_time': None, 'combine': False, 'output_format': 'pdf', 'two_sided': False})

    def test_options_of_the_form_reach_the_scan(self):
        self.script = [Done([str(self.document)])]
        device_id = self.add_printer('feeder', 'ADF')
        self.client.post('/scan', data={'device_id': device_id, 'output_format': 'pdf', 'combine': 'on', 'two_sided': 'on'})
        self.wait_for('Saved')
        self.assertEqual(self.scans[0][1], {'one_at_a_time': None, 'combine': True, 'output_format': 'pdf', 'two_sided': True})

    def test_combine_only_counts_for_pdf(self):
        self.script = [Done([str(self.document)])]
        self.start(output_format='png', combine='on')
        self.wait_for('Saved')
        self.assertFalse(self.scans[0][1]['combine'])

    def test_scan_asks_for_the_next_page(self):
        self.script = [PageScanned('1.png', 1), NeedsInput(NEXT_PAGE), Done([str(self.document)])]
        self.start()
        page = self.wait_for('Place the next page')
        self.assertIn('name="prompt_id"', page)

        answer = self.client.post('/scan/answer', data={'prompt_id': 1, 'reply': 'false'})
        self.assertEqual(answer.status_code, 200)
        self.assertNotIn('Place the next page', answer.text)    # the page that answers has moved on
        self.wait_for('Saved')

    def test_answer_to_an_old_question_is_ignored(self):
        self.script = [NeedsInput(NEXT_PAGE), Done([str(self.document)])]
        self.start()
        self.wait_for('Place the next page')
        self.client.post('/scan/answer', data={'prompt_id': 99, 'reply': 'true'})
        self.assertIn('Place the next page', self.client.get('/scan/status').text)

    def test_page_shows_the_running_scan_after_a_reload_and_everywhere(self):
        self.script = [NeedsInput(NEXT_PAGE)]
        self.start()
        self.wait_for('Place the next page')
        self.assertIn('Place the next page', self.client.get('/scan').text)
        self.assertIn('A scan needs you', self.client.get('/').text)

    def test_banner_goes_away_when_the_scan_is_done(self):
        self.assertNotIn('href="/scan"', self.client.get('/').text)
        self.script = [NeedsInput(NEXT_PAGE), Done([str(self.document)])]
        self.start()
        self.wait_for('Place the next page')
        self.assertIn('href="/scan"', self.client.get('/scan/banner').text)

        self.client.post('/scan/answer', data={'prompt_id': 1, 'reply': 'false'})
        self.wait_for('Saved')
        banner = self.client.get('/scan/banner').text
        self.assertNotIn('href="/scan"', banner)
        self.assertIn('hx-trigger="every', banner)      # and it notices the next scan

    def test_scan_page_itself_has_no_banner(self):
        self.script = [NeedsInput(NEXT_PAGE)]
        self.start()
        self.wait_for('Place the next page')
        self.assertNotIn('id="scan-banner"', self.client.get('/scan').text)

    def test_second_scan_goes_to_the_running_one(self):
        self.script = [NeedsInput(NEXT_PAGE)]
        self.start()
        self.wait_for('Place the next page')
        response = self.start()
        self.assertEqual(response.headers['HX-Redirect'], '/scan')
        self.assertEqual(len(self.scans), 1)

    def test_cancel(self):
        self.script = [NeedsInput(NEXT_PAGE)]
        self.start()
        self.wait_for('Place the next page')
        self.assertIn('Cancelled', self.client.post('/scan/cancel').text)
        self.assertNotIn('A scan', self.client.get('/').text)

    def test_refused_scans_show_why_and_start_nothing(self):
        for case, form, message in (
            ('bad name', {'name': '../x'}, "can't be used as a name"),
            ('bad format', {'output_format': 'gif'}, 'not supported'),
            ('unknown printer', {'device_id': device_to_id('nothing')}, 'is not saved'),
            ('broken id', {'device_id': '%%%'}, 'not a printer id'),
        ):
            with self.subTest(case):
                response = self.start(**form) if 'device_id' not in form else self.client.post('/scan', data=form)
                self.assertEqual(response.status_code, 422)
                self.assertIn(message, html.unescape(response.text))
        self.assertEqual(self.scans, [])

    def test_unknown_source_has_to_say_how_to_scan(self):
        device_id = self.add_printer('odd', 'Platen Thing')
        response = self.client.post('/scan', data={'device_id': device_id})
        self.assertEqual(response.status_code, 422)
        self.assertIn('Choose how to scan', response.text)

        self.script = [Done([str(self.document)])]
        self.client.post('/scan', data={'device_id': device_id, 'how': 'one'})
        self.wait_for('Saved')
        self.assertTrue(self.scans[0][1]['one_at_a_time'])

    def test_devices_are_left_alone_while_scanning(self):
        self.script = [NeedsInput(NEXT_PAGE)]
        self.start()
        self.wait_for('Place the next page')
        with mock.patch.object(Printer, 'discover_printers', mock.AsyncMock()) as discover, \
                mock.patch.object(Printer, 'is_available', mock.AsyncMock()) as available:
            self.assertEqual(self.client.post('/printers/discover').status_code, 422)
            self.assertEqual(self.client.post('/printers', data={'device': 'x'}).status_code, 422)
            self.assertIn('>In use<', self.client.get(f'/printers/{self.id}/availability').text)
        discover.assert_not_awaited()
        available.assert_not_awaited()

    def test_failed_scan_shows_the_error(self):
        async def broken():
            raise RuntimeError('scanimage failed (7)')
            yield
        with mock.patch.object(Printer, 'scan', lambda printer, **kwargs: broken()):
            self.start()
            self.assertIn('scanimage failed (7)', self.wait_for('The scan failed'))

    def test_unusable_save_location_is_refused_before_scanning(self):
        blocker = self.folder / 'file'
        blocker.write_text('x')
        self.settings.save_location = LocalFolderStorage(str(blocker / 'inside'))
        response = self.start()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.scans, [])

    def test_scan_saves_in_the_folder_the_form_shows(self):
        # leaving the field by pressing Start also sends the change, and that may arrive later
        self.script = [Done([str(self.document)])]
        target = self.folder / 'typed'
        self.start(name='invoice', path=f' {target} ')
        self.wait_for('Saved')
        self.assertEqual((target / 'invoice.pdf').read_bytes(), b'%PDF')
        self.assertEqual(str(self.settings.save_location), str(target))

    def test_scan_is_refused_if_the_folder_the_form_shows_is_not_usable(self):
        before = str(self.settings.save_location)
        blocker = self.folder / 'file'
        blocker.write_text('x')
        for path in (str(blocker / 'inside'), '', '   '):
            with self.subTest(path):
                self.assertEqual(self.start(path=path).status_code, 422)
        self.assertEqual(self.scans, [])
        self.assertEqual(str(self.settings.save_location), before)

    def test_status_without_a_scan(self):
        self.assertIn('There is no scan', self.client.get('/scan/status').text)
        self.assertEqual(self.client.post('/scan/cancel').status_code, 422)

    def finish_scan(self, *documents, name='invoice'):
        '''Runs a scan to the Saved page and returns the job and that page.'''
        self.script = [Done([str(document) for document in documents])]
        self.start(name=name, output_format='pdf')
        page = self.wait_for('Saved')
        return self.client.app.state.scans.job, page

    def second_document(self):
        document = self.folder / 'b.pdf'
        document.write_bytes(b'%PDF two')
        return document

    def test_saved_page_has_the_download_buttons_above_the_way_back(self):
        job, page = self.finish_scan(self.document)
        self.assertIn(f'data-url="/scan/{job.id}/download"', page)
        self.assertIn(f'data-delete-url="/scan/{job.id}/delete"', page)
        self.assertIn('data-name="invoice.pdf"', page)
        self.assertLess(page.index('Download to this device'), page.index('Download and delete from the server'))
        self.assertLess(page.index('Download and delete from the server'), page.index('Back to printers'))

    def test_download_of_one_file(self):
        job, _ = self.finish_scan(self.document)
        response = self.client.get(f'/scan/{job.id}/download')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'%PDF')
        self.assertEqual(response.headers['content-type'], 'application/pdf')
        self.assertEqual(response.headers['content-disposition'], "attachment; filename*=UTF-8''invoice.pdf")
        self.assertTrue((self.folder / 'saved' / 'invoice.pdf').exists())   # a download alone keeps the file

    def test_download_name_cant_break_out_of_the_header(self):
        job, _ = self.finish_scan(self.document, name='factură "a"; b')
        disposition = self.client.get(f'/scan/{job.id}/download').headers['content-disposition']
        self.assertEqual(disposition, "attachment; filename*=UTF-8''factur%C4%83%20%22a%22%3B%20b.pdf")

    def test_download_of_several_files_is_one_zip(self):
        job, page = self.finish_scan(self.document, self.second_document())
        self.assertIn('data-name="invoice.zip"', page)
        response = self.client.get(f'/scan/{job.id}/download')
        self.assertEqual(response.headers['content-type'], 'application/zip')
        self.assertIn("filename*=UTF-8''invoice.zip", response.headers['content-disposition'])
        with zipfile.ZipFile(io.BytesIO(response.content)) as zip_file:
            self.assertEqual({name: zip_file.read(name) for name in zip_file.namelist()},
                             {'invoice(1).pdf': b'%PDF', 'invoice(2).pdf': b'%PDF two'})

    def test_download_of_a_file_that_is_gone_says_so(self):
        job, _ = self.finish_scan(self.document)
        (self.folder / 'saved' / 'invoice.pdf').unlink()
        response = self.client.get(f'/scan/{job.id}/download')
        self.assertEqual(response.status_code, 422)
        self.assertIn('invoice.pdf', html.unescape(response.text))

    def test_delete_removes_all_the_files_and_the_page_says_so(self):
        job, _ = self.finish_scan(self.document, self.second_document())
        response = self.client.post(f'/scan/{job.id}/delete')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list((self.folder / 'saved').iterdir()), [])
        for page in (response.text, self.client.get('/scan/status').text):
            self.assertIn('deleted from the server', page)
            self.assertNotIn('data-download', page)
            self.assertIn('Back to printers', page)

    def test_after_deleting_there_is_nothing_to_download_and_deleting_again_is_fine(self):
        job, _ = self.finish_scan(self.document)
        self.client.post(f'/scan/{job.id}/delete')
        self.assertEqual(self.client.get(f'/scan/{job.id}/download').status_code, 422)
        self.assertEqual(self.client.post(f'/scan/{job.id}/delete').status_code, 200)

    def test_failed_delete_keeps_what_is_left_listed(self):
        job, _ = self.finish_scan(self.document, self.second_document())
        real_remove = os.remove

        def remove(path):
            if path.endswith('invoice(2).pdf'):
                raise PermissionError('locked')
            real_remove(path)
        with mock.patch('printer_backend.storage.os.remove', remove):
            response = self.client.post(f'/scan/{job.id}/delete')
        self.assertEqual(response.status_code, 422)
        self.assertIn('locked', response.text)
        page = self.client.get('/scan/status').text
        self.assertIn('invoice(2).pdf', page)
        self.assertNotIn('invoice(1).pdf', page)
        self.assertEqual(self.client.get(f'/scan/{job.id}/download').content, b'%PDF two')

    def test_page_of_an_older_scan_cant_touch_the_newer_one(self):
        old, _ = self.finish_scan(self.document)
        new, _ = self.finish_scan(self.document, name='newer')
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(self.client.get(f'/scan/{old.id}/download').status_code, 422)
        self.assertEqual(self.client.post(f'/scan/{old.id}/delete').status_code, 422)
        self.assertTrue((self.folder / 'saved' / 'newer.pdf').exists())
        self.assertTrue((self.folder / 'saved' / 'invoice.pdf').exists())

    def test_nothing_to_download_or_delete_before_the_scan_is_saved(self):
        self.script = [NeedsInput(NEXT_PAGE)]
        self.start()
        page = self.wait_for('Place the next page')
        job = self.client.app.state.scans.job
        self.assertNotIn('data-download', page)
        self.assertEqual(self.client.get(f'/scan/{job.id}/download').status_code, 422)
        self.assertEqual(self.client.post(f'/scan/{job.id}/delete').status_code, 422)

    def test_cross_site_delete_is_refused(self):
        job, _ = self.finish_scan(self.document)
        response = self.client.post(f'/scan/{job.id}/delete', headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(response.status_code, 403)
        self.assertTrue((self.folder / 'saved' / 'invoice.pdf').exists())


@unittest.skipIf(TestClient is None, 'fastapi is not installed')
class WebPrinterTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.id = device_to_id(DEVICE)
        self.two = Printer('two')
        self.two.options = {
            'Flatbed': parse_help((FIXTURES / 'envy_6400_airscan_flatbed.txt').read_text()),
            'ADF': parse_help((FIXTURES / 'envy_6400_airscan_adf.txt').read_text()),
        }
        self.two.select_source('Flatbed')
        self.repository.add_printer(self.two)
        self.two_id = device_to_id('two')

    def saved(self, device):
        return PrinterRepository(self.repository.path).get_printer(device)

    def test_page_shows_the_options_and_the_scan_form(self):
        page = self.client.get(f'/printers/{self.id}').text
        self.assertIn('name="resolution"', page)
        self.assertIn('name="mode"', page)
        self.assertIn('Width', page)                # x, as scanimage names it
        self.assertIn('More options', page)
        self.assertIn('Start scan', page)
        self.assertNotIn('hx-post="/printers/' + self.id + '/source"', page)   # a single source has nothing to switch

    def test_inactive_options_are_not_offered(self):
        self.assertNotIn('jpeg-quality', self.client.get(f'/printers/{self.id}').text)

    def test_unknown_printer(self):
        self.assertEqual(self.client.get(f'/printers/{device_to_id("nothing")}').status_code, 422)

    def test_switching_the_source_swaps_the_options_and_the_scan_form(self):
        page = self.client.get(f'/printers/{self.two_id}').text
        self.assertIn('hx-post="/printers/' + self.two_id + '/source"', page)
        self.assertNotIn('two_sided', page)         # a flatbed

        response = self.client.post(f'/printers/{self.two_id}/source', data={'source': 'ADF'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="printer-panel"', response.text)
        self.assertIn('two_sided', response.text)   # a feeder
        self.assertEqual(self.saved('two').source, 'ADF')

    def test_unknown_source_is_refused(self):
        response = self.client.post(f'/printers/{self.two_id}/source', data={'source': 'Nope'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.saved('two').source, 'Flatbed')

    def test_changed_option_is_saved(self):
        response = self.client.post(f'/printers/{self.id}/options', data={'resolution': '300', 'mode': 'Gray'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Saved', response.text)
        options = self.saved(DEVICE).current_options
        self.assertEqual((options['resolution'].value, options['mode'].value), ('300', 'Gray'))
        self.assertIn('value="300" selected', ' '.join(self.client.get(f'/printers/{self.id}').text.split()))

    def test_refused_value_is_shown_and_nothing_changes(self):
        before = self.saved(DEVICE).current_options['mode'].value
        response = self.client.post(f'/printers/{self.id}/options', data={'resolution': '999', 'mode': 'Gray'})
        self.assertEqual(response.status_code, 422)
        self.assertIn('not valid', response.text)
        self.assertEqual(self.saved(DEVICE).current_options['mode'].value, before)

    def test_empty_fields_are_left_alone(self):
        response = self.client.post(f'/printers/{self.id}/options', data={'resolution': '', 'mode': 'Gray'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.saved(DEVICE).current_options['mode'].value, 'Gray')

    def test_options_go_to_the_source_the_form_showed(self):
        # the page was opened on the flatbed, the printer was switched to the feeder since
        self.client.post(f'/printers/{self.two_id}/source', data={'source': 'ADF'})
        self.client.post(f'/printers/{self.two_id}/options', data={'source_of_form': 'Flatbed', 'resolution': '600'})
        printer = self.saved('two')
        self.assertEqual(printer.options_of('Flatbed')['resolution'].value, '600')
        self.assertNotEqual(printer.options_of('ADF')['resolution'].value, '600')

    def test_availability(self):
        for reachable, text in ((True, '>Available<'), (False, '>Not reachable<')):
            with self.subTest(text), mock.patch.object(Printer, 'is_available', mock.AsyncMock(return_value=reachable)):
                page = self.client.get(f'/printers/{self.id}/availability').text
                self.assertIn(text, page)
                self.assertNotIn('load', page)  # the first check doesn't lead to another

    def test_discovery_offers_only_new_printers(self):
        lines = [f"device `{DEVICE}' is a HP ENVY\n", "device `airscan:e0:New <b>' is a HP ENVY\n"]
        with mock.patch.object(Printer, 'discover_printers', mock.AsyncMock(return_value=lines)):
            page = self.client.post('/printers/discover').text
        self.assertIn('airscan:e0:New', page)
        self.assertIn('hx-post="/printers"', page)
        self.assertNotIn('<b>', page)               # escaped
        self.assertNotIn('192.168.0.178', page)     # already saved

    def test_discovery_that_finds_nothing_says_what_to_do(self):
        with mock.patch.object(Printer, 'discover_printers', mock.AsyncMock(return_value=[])):
            self.assertIn('No new printers found', self.client.post('/printers/discover').text)

    def test_discovery_page_searches_when_it_opens(self):
        page = self.client.get('/printers/discover').text
        self.assertIn('hx-post="/printers/discover"', page)
        self.assertIn('load', page)

    def test_failed_discovery_is_shown(self):
        with mock.patch.object(Printer, 'discover_printers', mock.AsyncMock(side_effect=RuntimeError('no sane'))):
            response = self.client.post('/printers/discover')
        self.assertEqual(response.status_code, 422)
        self.assertIn('no sane', response.text)

    def test_add_printer(self):
        async def load_options(printer):
            printer.options = self.two.options
            printer.select_source('Flatbed')
        with mock.patch.object(Printer, 'load_options', load_options):
            response = self.client.post('/printers', data={'device': 'airscan:e0:New?x=1'})
        self.assertEqual(response.headers['HX-Redirect'], '/')
        self.assertEqual(self.saved('airscan:e0:New?x=1').source, 'Flatbed')
        self.assertEqual(self.active_printer_of_home(), device_to_id('airscan:e0:New?x=1'))     # added to be used

    def test_add_printer_whose_options_cant_be_read(self):
        with mock.patch.object(Printer, 'load_options', mock.AsyncMock(side_effect=RuntimeError('open failed'))):
            response = self.client.post('/printers', data={'device': 'broken'})
        self.assertEqual(response.status_code, 422)
        self.assertIn('open failed', response.text)
        with self.assertRaises(ValueError):
            self.saved('broken')

    def test_adding_a_saved_printer_again_is_refused(self):
        with mock.patch.object(Printer, 'load_options', mock.AsyncMock()):
            self.assertEqual(self.client.post('/printers', data={'device': 'two'}).status_code, 422)

    def test_delete_printer(self):
        response = self.client.post(f'/printers/{self.two_id}/delete')
        self.assertEqual(response.headers['HX-Redirect'], '/')
        with self.assertRaises(ValueError):
            self.saved('two')

    def active_printer_of_home(self):
        '''The printer the closed dropdown shows and the Scan button goes to.'''
        page = self.client.get('/').text
        scan = re.search(r'id="scan-link"[^>]*href="/printers/([^"]+)"', page).group(1)
        first = re.search(r'data-device-id="([^"]+)"', page).group(1)
        current = re.search(r'aria-current="true"[^>]*?data-device-id="([^"]+)"', page, re.DOTALL).group(1)
        self.assertEqual({scan, first}, {current})
        return current

    def test_home_checks_every_printer_in_one_dropdown(self):
        page = self.client.get('/').text
        for device_id in (self.id, self.two_id):
            self.assertIn(f'hx-get="/printers/{device_id}/availability"', page)
            self.assertIn(f'data-scan-url="/printers/{device_id}"', page)
        self.assertEqual(page.count('aria-current="true"'), 1)

    def test_first_printer_is_active_until_one_is_chosen(self):
        self.assertEqual(self.active_printer_of_home(), self.id)

    def test_chosen_printer_stays_active(self):
        response = self.client.post('/printers/active', data={'device_id': self.two_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.active_printer_of_home(), self.two_id)
        self.assertEqual(SettingsRepository(self.settings.path).active_printer, 'two')

    def test_unknown_printer_cant_be_chosen(self):
        response = self.client.post('/printers/active', data={'device_id': device_to_id('nothing')})
        self.assertEqual(response.status_code, 422)
        self.assertIsNone(self.settings.active_printer)

    def test_deleting_the_active_printer_makes_another_one_active(self):
        self.client.post('/printers/active', data={'device_id': self.two_id})
        self.client.post(f'/printers/{self.two_id}/delete')
        self.assertEqual(self.active_printer_of_home(), self.id)


@unittest.skipIf(TestClient is None, 'fastapi is not installed')
class WebSettingsTest(WebTestCase):
    def test_scan_page_shows_the_save_location_and_saves_changes_to_it(self):
        page = self.client.get(f'/printers/{device_to_id(DEVICE)}').text
        self.assertIn(f'value="{self.folder / "saved"}"', page)
        self.assertIn('hx-post="/settings/save-location"', page)

    def test_with_a_save_root_the_field_leaves_it_out_and_input_is_added_to_it(self):
        root = self.folder / 'storage'
        root.mkdir()
        with mock.patch.dict(os.environ, {'SAVE_ROOT': str(root)}):
            self.settings.save_location = LocalFolderStorage('Invoices')
            page = self.client.get(f'/printers/{device_to_id(DEVICE)}').text
            self.assertIn('value="Invoices"', page)
            self.assertNotIn(str(root), page)

            response = self.client.post('/settings/save-location', data={'path': 'Taxes/2026'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('Taxes/2026', response.text)
            self.assertNotIn(str(root), response.text)
            self.assertEqual(str(self.settings.save_location), str(root / 'Taxes' / '2026'))

            # empty means the root itself
            self.assertEqual(self.client.post('/settings/save-location', data={'path': ''}).status_code, 200)
            self.assertEqual(str(self.settings.save_location), str(root))
            self.assertEqual(self.client.post('/settings/save-location', data={'path': '../out'}).status_code, 422)

    def test_there_is_no_settings_page(self):
        self.assertEqual(self.client.get('/settings').status_code, 404)
        self.assertNotIn('href="/settings"', self.client.get('/').text)

    def test_pages_start_light(self):
        self.assertIn('<html lang="en" data-theme="light">', self.client.get('/').text)

    def test_theme_is_changed_and_remembered(self):
        response = self.client.post('/settings/theme', data={'theme': 'dark'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-current="dark"', response.text)
        self.assertIn('Switch to light colours', response.text)
        self.assertEqual(SettingsRepository(self.settings.path).theme, 'dark')
        for path in ('/', '/printers/discover', '/scan', f'/printers/{device_to_id(DEVICE)}'):
            with self.subTest(path):
                page = self.client.get(path).text
                self.assertIn('data-theme="dark"', page)
                self.assertIn('id="theme-toggle"', page)

    def test_toggle_shows_the_saved_theme(self):
        self.settings.theme = 'dark'
        self.assertIn('data-current="dark"', self.client.get('/settings/theme').text)

    def test_unknown_theme_is_refused(self):
        for theme in ('sepia', ''):
            with self.subTest(theme):
                self.assertEqual(self.client.post('/settings/theme', data={'theme': theme}).status_code, 422)
                self.assertEqual(self.settings.theme, 'light')

    def test_theme_and_save_location_dont_overwrite_each_other(self):
        self.settings.theme = 'dark'
        self.client.post('/settings/save-location', data={'path': str(self.folder / 'elsewhere')})
        saved = SettingsRepository(self.settings.path)
        self.assertEqual(saved.theme, 'dark')
        self.assertEqual(str(saved.save_location), str(self.folder / 'elsewhere'))

    def test_empty_location_is_refused(self):
        # it would mean the folder the server runs in
        before = str(self.settings.save_location)
        for path in ('', '   '):
            with self.subTest(path):
                response = self.client.post('/settings/save-location', data={'path': path})
                self.assertEqual(response.status_code, 422)
                self.assertIn('Enter the folder', response.text)
                self.assertEqual(str(self.settings.save_location), before)

    def test_save_location_is_changed_and_remembered(self):
        target = self.folder / 'elsewhere'
        response = self.client.post('/settings/save-location', data={'path': f' {target} '})
        self.assertEqual(response.status_code, 200)
        self.assertIn(str(target), response.text)
        self.assertTrue(target.is_dir())    # made, like the CLI does
        self.assertEqual(str(self.settings.save_location), str(target))
        self.assertEqual(str(SettingsRepository(self.settings.path).save_location), str(target))

    def test_unusable_location_is_refused_and_nothing_changes(self):
        blocker = self.folder / 'file'
        blocker.write_text('x')
        before = str(self.settings.save_location)
        for path in (str(blocker / 'inside'), './data/scans'):
            with self.subTest(path):
                response = self.client.post('/settings/save-location', data={'path': path})
                self.assertEqual(response.status_code, 422)
                self.assertIn("Can't save in", html.unescape(response.text))
                self.assertEqual(str(self.settings.save_location), before)


@unittest.skipIf(TestClient is None, 'fastapi is not installed')
class PrintRoutesTest(WebTestCase):
    """The print page with CUPS replaced by fakes: the backend tests cover what the commands get and say."""
    PDF = b'%PDF-1.4\n%fake page\n'
    QUEUE = 'ocds-e5a37fdbe391'
    JOB = QUEUE + '-12'
    URI = 'ipp://HPA8B13B03755A.local:631/ipp/print'
    ALL_OPTIONS = PrintCapabilities(['one-sided', 'two-sided-long-edge', 'two-sided-short-edge'], 'one-sided',
                                    ['monochrome', 'color'], 'color')
    SIMPLEX_MONO = PrintCapabilities(['one-sided'], 'one-sided', ['monochrome'], 'monochrome')

    def setUp(self):
        super().setUp()
        self.prints = self.folder / 'prints'
        self.capabilities = self.ALL_OPTIONS
        self.state = JOB_COMPLETED
        self.submit = mock.AsyncMock(return_value=self.JOB)
        self.cancel_cups_job = mock.AsyncMock()
        fakes = {
            'printer_backend.printing.PRINT_FOLDER_DEFAULT': str(self.prints),
            'web_frontend.routes.printing.ensure_queue': mock.AsyncMock(return_value=(self.QUEUE, self.URI)),
            'web_frontend.routes.printing.printer_capabilities': mock.AsyncMock(side_effect=self.get_capabilities),
            'web_frontend.print_job.convert_for_print': mock.AsyncMock(side_effect=lambda path: path),
            'web_frontend.print_job.submit': self.submit,
            'web_frontend.print_job.job_state': mock.AsyncMock(side_effect=lambda job: JobState(self.state, '')),
            'web_frontend.print_job.cancel_cups_job': self.cancel_cups_job,
            'web_frontend.print_job.PRINT_POLL_SECONDS': 0.01,
        }
        for target, replacement in fakes.items():
            patch = mock.patch(target, replacement)
            patch.start()
            self.addCleanup(patch.stop)

    async def get_capabilities(self, queue):
        return self.capabilities

    def upload(self, name='a.pdf', content=None, **form):
        data = {'copies': '1', **form}
        return self.client.post('/print', data=data,
                                files={'file': (name, self.PDF if content is None else content, 'application/pdf')})

    def job_folder_contents(self) -> list:
        return sorted(self.prints.rglob('*')) if self.prints.exists() else []

    def wait_until(self, check, description):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.02)
        self.fail(f'never got to: {description}')

    def last_job(self):
        return self.client.app.state.prints.jobs[0]

    def test_page_without_a_printer_says_so(self):
        response = self.client.get('/print')
        self.assertEqual(response.status_code, 200)
        self.assertIn('There is no printer chosen', response.text)

    def test_page_offers_the_printers_own_options(self):
        self.settings.active_printer = DEVICE
        page = self.client.get('/print').text
        self.assertIn('<select name="sides">', page)
        self.assertIn('Two sides, long edge', page)
        self.assertIn('Black and white', page)

        self.capabilities = self.SIMPLEX_MONO
        page = self.client.get('/print').text
        self.assertNotIn('name="sides"', page.replace('<input type="hidden" name="sides"', ''))
        self.assertIn('<input type="hidden" name="sides" value="one-sided">', page)
        self.assertIn('<input type="hidden" name="color_mode" value="monochrome">', page)

    def test_printer_that_does_not_answer_is_shown_on_the_page(self):
        self.settings.active_printer = DEVICE
        with mock.patch('web_frontend.routes.printing.ensure_queue',
                        mock.AsyncMock(side_effect=RuntimeError("Could not find this printer's print service"))):
            page = self.client.get('/print').text
        self.assertIn("Could not find this printer's print service", html.unescape(page))
        self.assertNotIn('type="file"', page)

    def test_upload_without_an_active_printer_is_refused(self):
        response = self.upload()
        self.assertEqual(response.status_code, 422)
        self.assertIn('Choose a printer first', response.text)
        self.assertEqual(self.client.app.state.prints.jobs, [])

    def test_wrong_file_type_is_refused_and_nothing_is_kept(self):
        self.settings.active_printer = DEVICE
        response = self.upload('program.exe', b'MZ\x90\x00')
        self.assertEqual(response.status_code, 422)
        self.assertIn("can't be printed", html.unescape(response.text))
        self.assertEqual(self.job_folder_contents(), [])

    def test_zip_named_like_a_pdf_is_refused(self):
        self.settings.active_printer = DEVICE
        response = self.upload('a.pdf', b'PK\x03\x04' + b'\x00' * 30)
        self.assertEqual(response.status_code, 422)
        self.assertIn('is not a real .pdf', response.text)
        self.assertEqual(self.job_folder_contents(), [])

    def test_file_over_the_limit_is_refused_and_leaves_no_file(self):
        self.settings.active_printer = DEVICE
        with mock.patch('printer_backend.printing.MAX_UPLOAD_BYTES', 1000):
            response = self.upload('big.pdf', self.PDF + b'0' * 2000)
        self.assertEqual(response.status_code, 422)
        self.assertIn('larger than', response.text)
        self.assertEqual(self.job_folder_contents(), [])

    def test_copies_outside_the_range_are_refused(self):
        self.settings.active_printer = DEVICE
        for copies in ('0', str(COPIES_MAX + 1), '2.5', 'many'):
            with self.subTest(copies):
                response = self.upload(copies=copies)
                self.assertEqual(response.status_code, 422)
                self.assertIn('copies', response.text)
        self.assertEqual(self.client.app.state.prints.jobs, [])

    def test_option_the_printer_lacks_is_refused(self):
        self.settings.active_printer = DEVICE
        self.capabilities = self.SIMPLEX_MONO
        response = self.upload(sides='two-sided-long-edge')
        self.assertEqual(response.status_code, 422)
        self.assertIn("can't do 'two-sided-long-edge'", html.unescape(response.text))
        self.assertEqual(self.job_folder_contents(), [])

    def test_printer_that_cannot_be_reached_keeps_no_file(self):
        self.settings.active_printer = DEVICE
        with mock.patch('web_frontend.routes.printing.ensure_queue',
                        mock.AsyncMock(side_effect=RuntimeError("Could not find this printer's print service"))):
            response = self.upload()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.job_folder_contents(), [])

    def test_good_upload_is_printed_and_its_files_are_removed(self):
        self.settings.active_printer = DEVICE
        response = self.upload(copies='2', sides='two-sided-long-edge', color_mode='monochrome')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['HX-Redirect'], '/print')

        self.wait_until(lambda: self.last_job().state == 'completed', 'a completed job')
        self.assertIn('Printed', self.client.get('/print/status').text)
        self.assertEqual(self.job_folder_contents(), [])
        self.assertEqual(self.submit.await_args.args[0], self.QUEUE)
        self.assertEqual(self.submit.await_args.args[3:], (2, 'two-sided-long-edge', 'monochrome'))

    def test_options_left_out_of_the_form_take_the_printers_defaults(self):
        self.settings.active_printer = DEVICE
        self.upload()
        self.wait_until(lambda: self.last_job().state == 'completed', 'a completed job')
        self.assertEqual(self.submit.await_args.args[3:], (1, 'one-sided', 'color'))

    def test_job_that_waits_can_be_cancelled(self):
        self.settings.active_printer = DEVICE
        self.state = JOB_PENDING
        self.upload()
        job = self.last_job()
        self.wait_until(lambda: job.state == 'queued', 'a queued job')
        self.assertIn('Waiting for the printer', self.client.get('/print/status').text)

        response = self.client.post(f'/print/{job.id}/cancel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(job.state, 'canceled')
        self.assertIn('Cancelled', response.text)
        self.cancel_cups_job.assert_awaited_once_with(self.JOB)

    def test_stale_job_id_is_refused(self):
        response = self.client.post('/print/0123456789abcdef/cancel')
        self.assertEqual(response.status_code, 422)
        self.assertIn("isn't listed anymore", html.unescape(response.text))

