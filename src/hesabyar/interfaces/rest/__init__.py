"""FastAPI REST interface (spec §6.2, §9.1).

Routes are thin: they validate the HTTP contract, call services, and
return typed responses. No accounting rules live here, and no domain
module imports FastAPI (see tests/interfaces/test_rest_health.py).
"""

