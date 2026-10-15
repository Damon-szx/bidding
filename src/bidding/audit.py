"""Idempotently backfill the configured database's unified request audit table."""
import os

from bidding.config import DATA
from bidding.storage import Store


def main():
    store = Store(DATA, os.getenv('DATABASE_URL') or None)
    try:
        print({'database_driver': store.engine.dialect.name, 'processed': store.backfill_audit()})
    finally:
        store.engine.dispose()


if __name__ == '__main__':
    main()
