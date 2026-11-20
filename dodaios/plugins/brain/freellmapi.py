"""FreeLLMApi — the local unified LLM router as a first-class brain.

FreeLLMApi (FLA) runs on the person's own machine and puts ONE
OpenAI-compatible endpoint in front of every upstream provider and model it is
configured with: ``/v1/chat/completions``, ``/v1/models`` and ``/v1/responses``.
It is the same wire protocol as ``local-openai``, but it deserves its own
provider id for two reasons that are about the person, not the code:

* it has a known default address (``http://127.0.0.1:3001``) instead of the
  "no guessable port, set one first" rule the generic self-hosted slot has to
  follow, so it works the moment it is picked; and
* it takes a REQUIRED key (the router answers 401 without its unified key),
  which the generic keyless slot treats as optional. Giving FLA its own
  credential slot means a missing key is reported as "no key set", not as an
  unreachable server.

Everything else — streaming, tool calls, model discovery, context-window
guard, thinking-prefix handling — is inherited from
:class:`~dodaios.plugins.brain.local_openai.LocalOpenAIBrain`. The base URL is
still overridable per provider card (``[brain.providers.freellmapi].base_url``),
so a router on another machine works without code changes.
"""

from __future__ import annotations

import logging

from dodaios.core import config as cfg

from .local_openai import LocalOpenAIBrain
from .ollama import normalize_server_root

log = logging.getLogger(__name__)

#: Where a default FreeLLMApi install listens.
DEFAULT_BASE_URL = "http://127.0.0.1:3001"

_NO_BASE_URL_HELP = (
    "FreeLLMApi needs its server URL — set it on the provider card (or "
    '[brain.providers."freellmapi"].base_url). The default is '
    f"{DEFAULT_BASE_URL}."
)

_NO_KEY_HELP = (
    "FreeLLMApi needs its unified API key — open the FreeLLMApi dashboard, copy "
    "the key from the Keys page header, and paste it on this card."
)


class FreeLLMAPIBrain(LocalOpenAIBrain):
    """A brain over the person's own FreeLLMApi router."""

    name: str = "freellmapi"
    # The router serves whatever its config holds; this is a budget hint only.
    context_window: int = 32_768
    supports_tools: bool = True

    def __init__(self, model: str | None = None) -> None:
        super().__init__(model=model)
        # The inherited constructor asks the catalog under the generic
        # ``local-openai`` id. Ask under this provider's own id, so a model
        # FLA declares as vision-capable is allowed to receive images.
        if self._model:
            try:
                from dodaios.brain.model_catalog import model_capabilities

                self.supports_vision = (
                    model_capabilities(self.name, self._model)["vision"] is True
                )
            except Exception:  # noqa: BLE001 — a probe must never break construction
                log.debug("freellmapi: vision capability probe failed — staying blind")

    def _resolve_root(self) -> str:
        if self._server_root is None:
            ep = cfg.resolve_provider_endpoint(
                self.name, vendor_default_base_url=DEFAULT_BASE_URL
            )
            if not ep.base_url:
                raise RuntimeError(_NO_BASE_URL_HELP)
            # Same server-root convention as the other local cards: ``/v1`` is
            # appended by the client, a pasted ``…/v1`` is normalized away.
            self._server_root = normalize_server_root(ep.base_url)
            self._credential = ep.credential or cfg.get_secret(
                "freellmapi_api_key", "FREELLMAPI_API_KEY"
            )
            if not self._credential:
                raise RuntimeError(_NO_KEY_HELP)
        return self._server_root


__all__ = ["DEFAULT_BASE_URL", "FreeLLMAPIBrain"]
