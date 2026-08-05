"""Read-only FastAPI service over Postgres (ARCHITECTURE section 1).

Deliberately does **not** re-export the FastAPI instance. Binding the name `app` on this
package would shadow the `jme.api.app` submodule, so `import jme.api.app as m` would hand
back the FastAPI object instead of the module. Import from `jme.api.app` directly.
"""

from __future__ import annotations

from jme.api.app import create_app, get_session

__all__ = ["create_app", "get_session"]
