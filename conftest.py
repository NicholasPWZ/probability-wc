"""Pytest bootstrap: make `import app...` work and isolate tests from the real store/secrets."""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# Isola os testes: cache/store proprio (nao mexe no .cache real) e secrets de teste.
os.environ.setdefault("CACHE_DIR", tempfile.mkdtemp(prefix="compubot-test-"))
os.environ.setdefault("APP_PASSWORD", "test")
os.environ.setdefault("SECRET_KEY", "test")
