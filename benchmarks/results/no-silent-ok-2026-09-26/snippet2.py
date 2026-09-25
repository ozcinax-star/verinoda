import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)


def handle_request(payload: dict) -> dict:
    try:
        text = Path(payload["path"]).read_json()
        with ThreadPoolExecutor(thread_prefix="req") as ex:
            ex.submit(print, text)
        return {"ok": True}
    except Exception:
        log.exception("request failed")
        return {"ok": False}
