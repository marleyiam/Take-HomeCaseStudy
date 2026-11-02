import argparse
import logging
import os

import uvicorn

from novamart_assistant import config

from .store import TicketStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

parser = argparse.ArgumentParser(prog="python -m ticket_desk", description="Ticket desk: inbox worker + review UI.")
parser.add_argument("--no-samples", action="store_true",
                    help="start with an empty desk instead of loading the sample tickets from data/tickets/samples/")
args = parser.parse_args()

if not args.no_samples:
    # Only an empty desk is seeded, so restarting doesn't draft (and pay for) the same samples twice.
    tickets_root = config.ROOT / "data" / "tickets"
    copied = TicketStore(tickets_root).seed_from(tickets_root / "samples")
    logging.info("Samples: %s", f"loaded {copied} sample tickets into the inbox" if copied
                 else "desk already has tickets, samples not loaded again")

uvicorn.run("ticket_desk.server:app", host="127.0.0.1", port=int(os.getenv("TICKET_DESK_PORT", "8080")))
