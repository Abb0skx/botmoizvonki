"""Optional separate process on the SAME host/SQLite volume; not a new queue."""
import signal
import threading
import logging


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Import only for this explicit entry point, never from the standalone ASR API.
    import botmoizvonki as bot
    error = bot.get_transcription_config_error()
    if error:
        raise SystemExit(error)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            refreshed = bot.process_one_transcription_refresh()
            processed = bot.process_one_transcription_job()
        except Exception as exc:
            print("LOCAL TRANSCRIPTION WORKER ERROR:", type(exc).__name__)
            refreshed = processed = False
        if not processed and not refreshed:
            stop.wait(bot.TRANSCRIPTION_POLL_SECONDS)


if __name__ == "__main__":
    main()
