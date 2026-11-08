import asyncio
from contextlib import suppress
import logging
import os
import shutil
from uuid import uuid4

from fastapi import UploadFile

from printer_backend.printing import (
    JOB_ABORTED, JOB_CANCELED, JOB_COMPLETED, JOB_PROCESSING, PRINT_POLL_SECONDS, UPLOAD_CHUNK,
    cancel as cancel_cups_job, convert_for_print, job_state, print_file_name, submit)

# states of a print job
CONVERTING = 'converting'   # LibreOffice is making a PDF of the file
SENDING = 'sending'         # lp is handing the file to CUPS
QUEUED = 'queued'           # CUPS has it; it waits for the printer (also when the printer is offline)
PRINTING = 'printing'
COMPLETED = 'completed'
FAILED = 'failed'
CANCELED = 'canceled'
ACTIVE = (CONVERTING, SENDING, QUEUED, PRINTING)
CANCELLABLE = (CONVERTING, QUEUED, PRINTING)
LABELS = {CONVERTING: 'Preparing the file', SENDING: 'Sending to the printer', QUEUED: 'Waiting for the printer',
          PRINTING: 'Printing', COMPLETED: 'Printed', FAILED: 'Failed', CANCELED: 'Cancelled'}
RECENT_JOBS = 10        # finished jobs that are still listed


async def store_upload(upload: UploadFile, folder: str, limit: int) -> str:
    '''
    Streams an upload into the folder in chunks, and returns the path of the file. Raises ValueError, leaving
    no file behind, if it is bigger than limit bytes or its name can't be used.
    '''
    path = os.path.join(folder, print_file_name(upload.filename or ''))
    os.makedirs(folder, exist_ok=True)
    size = 0
    try:
        with open(path, 'xb') as target:
            while chunk := await upload.read(UPLOAD_CHUNK):
                size += len(chunk)
                if size > limit:
                    raise ValueError(f'The file is larger than {limit // (1024 * 1024)} MiB')
                target.write(chunk)
    except BaseException:
        with suppress(FileNotFoundError):
            os.remove(path)
        raise
    return path


class PrintJob():
    '''
    One file on its way to the printer: the web version of printing.py's steps. It converts the file, hands
    it to CUPS and then follows the CUPS job until the printer is done with it. Requests only read its state
    and cancel it.
    '''
    def __init__(self, name: str, folder: str, path: str, device: str, queue: str,
                 copies: int, sides: str, color_mode: str):
        self.id = uuid4().hex
        self.name = name                # the file name, as the user knows it
        self.folder = folder            # ./data/prints/<id>, removed as soon as CUPS has the file
        self.path = path
        self.device = device
        self.queue = queue
        self.copies = copies
        self.sides = sides
        self.color_mode = color_mode

        self.state = CONVERTING
        self.reason = ''               # what CUPS says while the job waits, e.g. 'printer offline'
        self.error: str | None = None
        self.cups_job: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    @property
    def cancellable(self) -> bool:
        return self.state in CANCELLABLE

    @property
    def label(self) -> str:
        return LABELS[self.state]

    def start(self):
        self._task = asyncio.create_task(self._guard())
        # a task that is cancelled before its first step never runs _guard, so this can't be done there
        self._task.add_done_callback(self._ended)

    def _ended(self, task: asyncio.Task):
        if task.cancelled():
            self.state = CANCELED

    async def wait(self):
        '''Returns when the job is not working on its own anymore.'''
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def cancel(self):
        '''Cancels the job. A file that is still being converted stops at once; one that CUPS has is cancelled there.'''
        if not self.cancellable:
            raise ValueError('This print job can no longer be cancelled')
        if self.state == CONVERTING:
            self._task.cancel()
            await self.wait()
            return
        await cancel_cups_job(self.cups_job)
        self.state = CANCELED
        self._task.cancel()
        await self.wait()

    async def stop(self):
        '''For shutdown: stops following the job. CUPS keeps printing what it has.'''
        if self._task is not None:
            self._task.cancel()
        await self.wait()

    async def _guard(self):
        try:
            await self._print()
        except (RuntimeError, ValueError) as error:
            self._fail(error)
        except asyncio.CancelledError:
            raise
        except Exception as error:      # a bug must not leave the page waiting for a job that is gone
            logging.exception('Print job crashed')
            self._fail(error)
        finally:
            self._remove_files()

    def _fail(self, error: Exception):
        self.state = FAILED
        self.error = str(error)

    def _remove_files(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    async def _print(self):
        self.state = CONVERTING
        pdf = await convert_for_print(self.path)
        self.state = SENDING
        self.cups_job = await submit(self.queue, pdf, self.name, self.copies, self.sides, self.color_mode)
        self._remove_files()            # CUPS has its own copy from now on
        self.state = QUEUED
        while True:
            info = await job_state(self.cups_job)
            self.reason = info.reason
            if info.state == JOB_COMPLETED:
                self.state = COMPLETED
                return
            if info.state == JOB_CANCELED:
                self.state = CANCELED
                return
            if info.state == JOB_ABORTED:
                raise RuntimeError(info.reason or 'The printer stopped the job')
            self.state = PRINTING if info.state == JOB_PROCESSING else QUEUED
            await asyncio.sleep(PRINT_POLL_SECONDS)


class PrintManager():
    '''The print jobs of the server. Several can run at once, CUPS keeps them in order; the newest ones are listed.'''
    def __init__(self):
        self.jobs: list[PrintJob] = []    # newest first

    @property
    def active(self) -> bool:
        return any(job.active for job in self.jobs)

    def start(self, job: PrintJob) -> PrintJob:
        self.jobs.insert(0, job)
        job.start()
        finished = [job for job in self.jobs if not job.active]
        for old in finished[RECENT_JOBS:]:
            self.jobs.remove(old)
        return job

    def get(self, job_id: str) -> PrintJob:
        '''Raises ValueError for a job that isn't listed (anymore), so a page left open can't act on a different one.'''
        for job in self.jobs:
            if job.id == job_id:
                return job
        raise ValueError("This print job isn't listed anymore")

    async def shutdown(self):
        for job in self.jobs:
            await job.stop()
