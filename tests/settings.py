SECRET_KEY = "tests-only"
INSTALLED_APPS = ["django.contrib.staticfiles", "tasks"]
ROOT_URLCONF = "tests.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
}]
DATABASES = {}
LANGUAGE_CODE = "de"
TIME_ZONE = "Europe/Berlin"
USE_I18N = False
USE_TZ = True
STATIC_URL = "static/"
