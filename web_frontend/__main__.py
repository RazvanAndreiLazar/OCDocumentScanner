import os

import uvicorn

if __name__ == '__main__':
    # one worker: the saved printers and the running scan live in this process's memory.
    # Run it from the repository root: ./data is relative to it.
    uvicorn.run('web_frontend.app:create_app', factory=True, workers=1,
                host=os.environ.get('WEB_HOST', '0.0.0.0'), port=int(os.environ.get('WEB_PORT', '8000')))
