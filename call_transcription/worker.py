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

    def refresh_cards():
        # Telegram editing must not wait behind a CPU-heavy audio job.
        # The existing SQLite lease serializes edits across processes.
        while not stop.is_set():
            try:
                bot.process_one_transcription_refresh()
            except Exception as exc:
                logging.warning("transcript_card_refresh_failed error=%s", type(exc).__name__)
            stop.wait(2)

    refresher = threading.Thread(target=refresh_cards, name="transcript-cards", daemon=True)
    refresher.start()
    while not stop.is_set():
        try:
            processed = bot.process_one_transcription_job()
        except Exception as exc:
            print("LOCAL TRANSCRIPTION WORKER ERROR:", type(exc).__name__)
            processed = False
        if not processed:
            stop.wait(bot.TRANSCRIPTION_POLL_SECONDS)
    refresher.join(timeout=35)


if __name__ == "__main__":
    main()
