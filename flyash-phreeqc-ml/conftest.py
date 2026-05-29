"""pytest bootstrap.

Living at the project root, this file makes pytest add the root to ``sys.path``
(prepend import mode), so ``import flyash_phreeqc_ml`` works during tests without a
``pip install -e .``. It intentionally contains no fixtures.
"""
