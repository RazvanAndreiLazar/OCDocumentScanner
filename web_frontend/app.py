import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from printer_backend.printer_repository import PrinterRepository
from printer_backend.printing import clean_prints_folder
from printer_backend.scanning import init
from printer_backend.settings_repository import SettingsRepository
from web_frontend.device_ids import device_name
from web_frontend.print_job import PrintManager
from web_frontend.routes import printers, printing, scanning, settings as settings_routes
from web_frontend.scan_job import ScanManager

HERE = Path(__file__).parent
SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')


def allowed_hosts() -> list[str]:
    '''Host names the server answers to, from WEB_ALLOWED_HOSTS (comma separated). Everything by default.'''
    return [host.strip() for host in os.environ.get('WEB_ALLOWED_HOSTS', '*').split(',') if host.strip()]


def create_app(repository: PrinterRepository | None = None, settings: SettingsRepository | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await init()
        except RuntimeError as error:   # scanimage missing: the pages still open, scanning reports it
            print(f'Warning: {error}')
        await clean_prints_folder()         # the uploads of a previous run are not needed anymore
        yield
        await app.state.scans.shutdown()    # stops the running scanimage
        await app.state.prints.shutdown()   # stops following the print jobs (CUPS keeps printing them)

    app = FastAPI(lifespan=lifespan)
    app.state.repository = PrinterRepository() if repository is None else repository
    app.state.settings = SettingsRepository() if settings is None else settings
    app.state.scans = ScanManager()
    app.state.prints = PrintManager()
    # every page shows a banner while a scan is going on, wherever the user navigated to, and has the saved colours
    app.state.templates = Jinja2Templates(directory=HERE / 'templates', context_processors=[
        lambda request: {'scan_job': request.app.state.scans.job, 'theme': request.app.state.settings.theme}])
    app.state.templates.env.filters['device_name'] = device_name
    app.mount('/static', StaticFiles(directory=HERE / 'static'), name='static')

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())

    @app.middleware('http')
    async def refuse_cross_site(request: Request, call_next):
        '''Pages on other sites can make the browser send us a POST. Browsers say where it came from.'''
        if request.method not in SAFE_METHODS:
            if request.headers.get('sec-fetch-site', 'same-origin') not in ('same-origin', 'none'):
                return PlainTextResponse('Cross-site request refused', status_code=403)
            origin = request.headers.get('origin')
            if origin and urlsplit(origin).netloc != request.headers.get('host'):
                return PlainTextResponse('Cross-site request refused', status_code=403)
        return await call_next(request)

    # the backend reports a refused value as ValueError and a failed command or folder as RuntimeError,
    # so a route that lets them through shows them inline instead of failing
    @app.exception_handler(ValueError)
    @app.exception_handler(RuntimeError)
    async def show_error(request: Request, error: Exception):
        return app.state.templates.TemplateResponse(request, '_message.html', {'message': str(error)},
                                                    status_code=422)

    app.include_router(printers.router)
    app.include_router(scanning.router)
    app.include_router(printing.router)
    app.include_router(settings_routes.router)
    return app
