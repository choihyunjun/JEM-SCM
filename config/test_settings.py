"""Isolated business-logic tests: no production database, ERP, SMTP or file uploads.

The existing material 0045/0046 migrations both add updated_by. These tests
build tables from current models; migration upgrades must be tested separately.
"""
from .settings import *  # noqa: F403,F401

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
        'TEST': {'NAME': ':memory:'},
    },
}
ERP_ENABLED = False
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}
MIGRATION_MODULES = {
    app: None for app in
    ['admin', 'auth', 'contenttypes', 'sessions', 'orders', 'material', 'qms', 'admin_app']
}
