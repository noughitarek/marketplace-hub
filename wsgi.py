import os

from werkzeug.middleware.proxy_fix import ProxyFix

from app import create_app

PREFIX = os.environ.get("URL_PREFIX", "").rstrip("/")


class PrefixMiddleware:
    """Mount the whole app under PREFIX. Works whether or not nginx strips it."""

    def __init__(self, wsgi_app, prefix):
        self.app, self.prefix = wsgi_app, prefix

    def __call__(self, environ, start_response):
        if self.prefix:
            environ["SCRIPT_NAME"] = self.prefix
            path = environ.get("PATH_INFO", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.app(environ, start_response)


app = create_app()
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # no x_prefix here
app.wsgi_app = PrefixMiddleware(app.wsgi_app, PREFIX)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)