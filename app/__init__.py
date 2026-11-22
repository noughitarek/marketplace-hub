import secrets

from flask import Flask, abort, request, session

from .config import Config
from .extensions import db


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)
    db.init_app(app)

    from .routes.api import bp as api_bp
    from .routes.web import bp as web_bp
    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api/v1")

    @app.before_request
    def csrf_protect():
        if request.method == "POST" and not request.path.startswith("/api/"):
            token = session.get("_csrf")
            if not token or token != request.form.get("_csrf"):
                abort(400, "Invalid CSRF token - reload the page and try again.")

    @app.context_processor
    def inject_globals():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        from .services import settings
        try:
            cur, biz = settings.get("currency"), settings.get("business_name")
        except Exception:
            cur, biz = "DA", "Marketplace Hub"
        return {"csrf_token": session["_csrf"], "currency": cur, "business_name": biz}

    @app.template_filter("money")
    def money(v):
        return f"{int(v or 0):,}".replace(",", " ")

    @app.template_filter("localdt")
    def localdt(dt, fmt="%d/%m %H:%M"):
        from datetime import timezone
        from .services import settings
        if not dt:
            return "—"
        return dt.replace(tzinfo=timezone.utc).astimezone(settings.tz()).strftime(fmt)

    with app.app_context():
        import os
        for sub in ("images", "audio", "chat_images"):
            os.makedirs(os.path.join(app.root_path, "static", "uploads", sub), exist_ok=True)
        db.create_all()
        try:
            from sqlalchemy import text
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE message ADD COLUMN IF NOT EXISTS audio_path VARCHAR(255)"))
                conn.execute(text("ALTER TABLE message ADD COLUMN IF NOT EXISTS audio_mime VARCHAR(50)"))
                conn.execute(text("ALTER TABLE message ADD COLUMN IF NOT EXISTS image_path VARCHAR(255)"))
                conn.execute(text("ALTER TABLE message ADD COLUMN IF NOT EXISTS image_mime VARCHAR(50)"))
                conn.commit()
        except Exception:
            pass
        from .services import settings
        settings.ensure_agent_key()

    if app.config["SCHEDULER_ENABLED"]:
        from . import tasks
        tasks.start(app)
    return app
