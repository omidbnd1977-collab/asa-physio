"""نقطه‌ی ورود WSGI: `gunicorn -b 0.0.0.0:8080 wsgi:application`"""
from api.app import create_app

application = create_app()
