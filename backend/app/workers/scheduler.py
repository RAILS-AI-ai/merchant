from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.db.models import get_session_factory
from app.services.cart_cleanup import cleanup_expired_carts

_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if not get_settings().enable_scheduler:
        return
    if _scheduler is not None:
        return

    def _run():
        factory = get_session_factory()
        db = factory()
        try:
            cleaned = cleanup_expired_carts(db)
            if cleaned:
                print(f"Cron: cleaned {cleaned} expired carts")
        finally:
            db.close()

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_run, "cron", minute="*/15", id="cart_cleanup")
    _scheduler.start()


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
