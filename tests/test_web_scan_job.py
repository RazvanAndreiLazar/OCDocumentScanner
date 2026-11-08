import asyncio
import unittest
from unittest import mock

from printer_backend.printer import Printer
from printer_backend.scanning import Done, FLIP, NEXT_PAGE, NeedsInput, PageScanned, Progress
from web_frontend import scan_job
from web_frontend.scan_job import ScanJob, ScanManager


class FakeStorage():
    def __init__(self, failures=0):
        self.failures = failures
        self.saved = []

    async def save(self, files, name):
        if self.failures:
            self.failures -= 1
            raise RuntimeError('disk full')
        self.saved.append((files, name))
        return [f'{name}.pdf']

    def __str__(self):
        return 'fake'


def scripted(*events, replies=None, then=None):
    '''An async generator like Printer.scan: yields the events, collecting the answers it gets.'''
    async def generator():
        for event in events:
            reply = yield event
            if replies is not None:
                replies.append(reply)
        if then is not None:
            raise then
    return generator()


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


class ScanJobTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clean = mock.patch.object(Printer, 'clean_scans_folder', mock.AsyncMock())
        self.clean = clean.start()
        self.addCleanup(clean.stop)
        self.storage = FakeStorage()

    def job(self, *events, **kwargs):
        job = ScanJob(scripted(*events, **kwargs), 'invoice', self.storage, 'dev')
        job.start()
        self.addAsyncCleanup(self.stop, job)
        return job

    @staticmethod
    async def stop(job):
        job.cancel()    # a test may leave a question open
        await job.wait()

    async def test_scan_is_saved_and_the_scans_folder_cleaned(self):
        job = self.job(Progress('Progress: 50.0%\r'), PageScanned('a.png', 1), Done(['a.pdf']))
        await job.wait()
        self.assertEqual(job.state, scan_job.SAVED)
        self.assertEqual(job.pages, 1)
        self.assertEqual(job.saved, ['invoice.pdf'])
        self.assertEqual(self.storage.saved, [(['a.pdf'], 'invoice')])
        self.clean.assert_awaited_once()

    async def test_progress_is_parsed_and_the_log_skips_percentages(self):
        job = self.job(Progress('Scanning page 1\nProgress: 12.5%\rProgress: 40.0%\r'), NeedsInput(FLIP))
        await until(lambda: job.state == scan_job.WAITING)
        self.assertEqual(job.percent, 40.0)
        self.assertEqual(job.log, 'Scanning page 1')

    async def test_next_page_answers(self):
        replies = []
        job = self.job(PageScanned('1.png', 1), NeedsInput(NEXT_PAGE), PageScanned('2.png', 2),
                       NeedsInput(NEXT_PAGE), Done(['a.pdf']), replies=replies)
        for answer in (True, False):
            await until(lambda: job.state == scan_job.WAITING)
            self.assertEqual(job.question, NEXT_PAGE)
            asked = job.prompt_id
            self.assertTrue(job.answer(asked, answer))
            await until(lambda: job.prompt_id != asked or not job.active)
        await job.wait()
        self.assertEqual([r for r in replies if r is not None], [True, False])
        self.assertEqual(job.state, scan_job.SAVED)

    async def test_flip_question(self):
        job = self.job(NeedsInput(FLIP), Done(['a.pdf']))
        await until(lambda: job.state == scan_job.WAITING)
        self.assertEqual(job.question, FLIP)
        job.answer(job.prompt_id, True)
        await job.wait()
        self.assertEqual(job.state, scan_job.SAVED)

    async def test_double_and_stale_answers_are_ignored(self):
        job = self.job(NeedsInput(NEXT_PAGE), NeedsInput(NEXT_PAGE), Done(['a.pdf']))
        await until(lambda: job.state == scan_job.WAITING)
        first = job.prompt_id
        self.assertTrue(job.answer(first, True))
        self.assertFalse(job.answer(first, False))      # second tap on the same question
        await until(lambda: job.state == scan_job.WAITING and job.prompt_id != first)
        self.assertFalse(job.answer(first, False))      # late tap must not answer the next question
        self.assertTrue(job.answer(job.prompt_id, False))
        await job.wait()

    async def test_answer_without_question(self):
        job = self.job(Progress('x'), Done(['a.pdf']))
        self.assertFalse(job.answer(0, True))

    async def test_cancel_while_waiting_closes_the_scan(self):
        closed = []

        async def generator():
            try:
                yield NeedsInput(NEXT_PAGE)
            finally:
                closed.append(True)
        job = ScanJob(generator(), 'invoice', self.storage, 'dev')
        job.start()
        await until(lambda: job.state == scan_job.WAITING)
        job.cancel()
        await job.wait()
        self.assertEqual(job.state, scan_job.CANCELLED)
        self.assertEqual(closed, [True])
        self.assertEqual(self.storage.saved, [])

    async def test_cancel_while_running(self):
        started = asyncio.Event()

        async def generator():
            started.set()
            await asyncio.sleep(60)
            yield Done(['a.pdf'])
        job = ScanJob(generator(), 'invoice', self.storage, 'dev')
        job.start()
        await started.wait()
        job.cancel()
        await job.wait()
        self.assertEqual(job.state, scan_job.CANCELLED)

    async def test_cancel_before_the_scan_started(self):
        job = ScanJob(scripted(Done(['a.pdf'])), 'invoice', self.storage, 'dev')
        job.start()
        job.cancel()
        await job.wait()
        self.assertEqual(job.state, scan_job.CANCELLED)
        self.assertEqual(self.storage.saved, [])

    async def test_scan_error(self):
        job = self.job(PageScanned('1.png', 1), then=RuntimeError('scanimage failed (7)'))
        await job.wait()
        self.assertEqual(job.state, scan_job.FAILED)
        self.assertIn('scanimage failed (7)', job.error)

    async def test_no_files_is_a_failure(self):
        job = self.job(Progress('x'))
        await job.wait()
        self.assertEqual(job.state, scan_job.FAILED)

    async def test_unexpected_error_still_ends_the_job(self):
        job = self.job(then=KeyError('bug'))
        with self.assertLogs(level='ERROR'):
            await job.wait()
        self.assertEqual(job.state, scan_job.FAILED)

    async def test_failed_save_keeps_the_files_and_can_be_retried(self):
        self.storage.failures = 1
        job = self.job(Done(['a.pdf']))
        await job.wait()
        self.assertEqual(job.state, scan_job.SAVE_FAILED)
        self.assertIn('disk full', job.error)
        self.clean.assert_not_awaited()

        self.assertTrue(job.retry_save())
        await job.wait()
        self.assertEqual(job.state, scan_job.SAVED)
        self.assertIsNone(job.error)
        self.assertEqual(self.storage.saved, [(['a.pdf'], 'invoice')])
        self.clean.assert_awaited_once()

    async def test_retry_only_after_a_failed_save(self):
        job = self.job(Done(['a.pdf']))
        await job.wait()
        self.assertFalse(job.retry_save())


class ScanManagerTest(unittest.IsolatedAsyncioTestCase):
    def new_job(self):
        async def generator():
            yield NeedsInput(NEXT_PAGE)
            yield Done(['a.pdf'])
        return ScanJob(generator(), 'invoice', FakeStorage(), 'dev')

    async def test_second_scan_is_refused_while_one_is_active(self):
        manager = ScanManager()
        first = manager.start(self.new_job())
        await until(lambda: first.state == scan_job.WAITING)
        self.assertTrue(manager.busy)
        with self.assertRaisesRegex(RuntimeError, 'already running'):
            manager.start(self.new_job())
        self.assertIs(manager.job, first)
        await manager.shutdown()

    async def test_finished_job_stays_until_the_next_one(self):
        manager = ScanManager()
        with mock.patch.object(Printer, 'clean_scans_folder', mock.AsyncMock()):
            first = manager.start(self.new_job())
            await until(lambda: first.state == scan_job.WAITING)
            first.answer(first.prompt_id, True)
            await first.wait()
            self.assertFalse(manager.busy)
            self.assertIs(manager.job, first)
            second = manager.start(self.new_job())
            self.assertIs(manager.job, second)
            await manager.shutdown()

    async def test_shutdown_without_a_job(self):
        await ScanManager().shutdown()
