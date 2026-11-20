"""HesabYar application layer — use cases shared by REST, MCP and workers.

Application code depends only on the domain (its ports, views and errors).
It never imports infrastructure (Tryton, SQLAlchemy, HTTP clients) or
interface code (FastAPI): see spec §6.3 and the architecture-guard test in
``tests/unit/test_application_services.py``.
"""
