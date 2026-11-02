import logging
import os

import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
uvicorn.run("ticket_desk.server:app", host="127.0.0.1", port=int(os.getenv("TICKET_DESK_PORT", "8080")))
