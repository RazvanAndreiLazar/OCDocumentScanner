import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import aclosing
import logging
import re

from printer_backend.printer import Printer
from printer_backend.scanning import Done, NeedsInput, PageScanned, Progress
from printer_backend.storage import ScanStorage

# states of a job
RUNNING = 'running'             # scanimage / img2pdf are working
WAITING = 'waiting'             # the scan asked a question (see ScanJob.question)
SAVING = 'saving'
SAVED = 'saved'
SAVE_FAILED = 'save_failed'     # the files are still in the scans folder, saving can be tried again
FAILED = 'failed'
CANCELLED = 'cancelled'
ACTIVE = (RUNNING, WAITING, SAVING)

PERCENT = re.compile(r'(\d+(?:\.\d+)?)%')
LOG_LINES = 30


class ScanJob():
    '''
    One scan in the background: the web version of CLI_APP.run_scan. It drives the events of the scan
    and saves the result; requests only read its state and send answers with answer(), cancel()
    and retry_save().
    '''
    def __init__(self, scan: AsyncGenerator, name: str, storage: ScanStorage, device: str):
        self.scan = scan
        self.name = name
        self.storage = storage
        self.device = device

        self.state = RUNNING
        self.question: str | None = None    # the NeedsInput kind while WAITING
        self.prompt_id = 0                  # tells the questions apart, so a late tap can't answer the next one
        self.pages = 0
        self.percent: float | None = None
        self.files: list[str] = []          # what the scan made, until it is saved
        self.saved: list[str] = []          # the names they got in the storage
        self.error: str | None = None
        self._log: deque[str] = deque(maxlen=LOG_LINES)
        self._reply: asyncio.Future | None = None
        self._task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    @property
    def log(self) -> str:
        return '\n'.join(self._log)

    def start(self):
        self._run(self._scan_and_save)

    def _run(self, work):
        self._task = asyncio.create_task(self._guard(work))
        # a task that is cancelled before its first step never runs _guard, so this can't be done there
        self._task.add_done_callback(self._ended)

    def _ended(self, task: asyncio.Task):
        if task.cancelled():
            self.state = CANCELLED

    async def wait(self):
        '''Returns when the job is not working on its own anymore (used by the tests).'''
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    def answer(self, prompt_id: int, reply: bool) -> bool:
        '''Answers the current question. Returns False, doing nothing, if that question isn't open anymore.'''
        if self.state != WAITING or prompt_id != self.prompt_id or self._reply is None or self._reply.done():
            return False
        self._reply.set_result(reply)
        self.state = RUNNING    # now, not when the scan wakes up: the page that answers shows it
        self.question = None
        return True

    def cancel(self):
        '''Stops the scan like Ctrl-C does in the CLI. Only a scan that is running or waiting can be stopped.'''
        if self.state in (RUNNING, WAITING) and self._task is not None:
            self._task.cancel()

    def retry_save(self) -> bool:
        if self.state != SAVE_FAILED:
            return False
        self.state = SAVING
        self._run(self._save)
        return True

    async def _guard(self, work):
        try:
            await work()
        except (RuntimeError, ValueError) as error:
            self._fail(error)
        except Exception as error:      # a bug must not leave the page waiting for a job that is gone
            logging.exception('Scan job crashed')
            self._fail(error)

    def _fail(self, error: Exception):
        self.state = FAILED
        self.error = str(error)

    async def _scan_and_save(self):
        files = None
        async with aclosing(self.scan) as events:     # leaving early stops the running scanimage
            try:
                event = await anext(events)
                while True:
                    if isinstance(event, Done):
                        files = event.files
                    event = await events.asend(await self._handle(event))
            except StopAsyncIteration:
                pass
        if not files:
            raise RuntimeError('The scan made no files')
        self.files = files
        await self._save()

    async def _handle(self, event) -> bool | None:
        '''Records an event of the scan, and returns the answer to send back if it asks for one.'''
        match event:
            case Progress(text):
                self._note_progress(text)
            case PageScanned():
                self.pages += 1
                self.percent = None
            case NeedsInput(kind):
                self.question = kind
                self.prompt_id += 1
                self._reply = asyncio.get_running_loop().create_future()
                self.state = WAITING
                try:
                    return await self._reply
                finally:
                    self.state = RUNNING
                    self.question = None
        return None

    def _note_progress(self, text: str):
        if percents := PERCENT.findall(text):
            self.percent = float(percents[-1])
        for line in text.replace('\r', '\n').splitlines():
            if line.strip() and not line.lstrip().startswith('Progress:'):
                self._log.append(line.rstrip())

    async def _save(self):
        self.state = SAVING
        try:
            self.saved = await self.storage.save(self.files, self.name)
        except RuntimeError as error:
            self.state = SAVE_FAILED
            self.error = f"Saving failed: {error}. The scan stays on the server until the next scan"
            return
        self.error = None
        await Printer.clean_scans_folder()
        self.state = SAVED


class ScanManager():
    '''Holds the one scan the server runs at a time: every scan uses (and empties) the same scans folder.'''
    def __init__(self):
        self.job: ScanJob | None = None     # the running one, or the last one until the next starts

    @property
    def busy(self) -> bool:
        return self.job is not None and self.job.active

    def start(self, job: ScanJob) -> ScanJob:
        if self.busy:
            raise RuntimeError('A scan is already running')
        self.job = job
        job.start()
        return job

    async def shutdown(self):
        if self.job is not None:
            self.job.cancel()
            await self.job.wait()
