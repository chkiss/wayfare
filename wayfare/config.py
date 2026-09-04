"""Configuration.

Every value comes from the environment or from files outside the repository.
Nothing here contains, defaults to, or logs a secret — the repo is safe to
publish as-is.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _host_timezone() -> str:
    """Imported lazily: timeutil imports schema, which must not import config."""
    from .timeutil import host_timezone

    return host_timezone()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


#: Where a deployed instance keeps its per-user files. Preferred over the
#: in-repo directories when it exists, so the CLI and the running service
#: agree about configuration without anyone having to export environment
#: variables in their shell.
#: Free and vision-free is fine: the model only ever reads text that tesseract
#: or pdftotext already extracted.
DEFAULT_LLM_MODEL = "google/gemma-4-31b-it:free"

USER_CONFIG_DIR = Path.home() / ".config" / "wayfare"
USER_STATE_DIR = Path.home() / ".local" / "state" / "wayfare"


def _preferred(env_var: str, deployed: Path, fallback: Path) -> Path:
    """Environment first, then the deployed location, then the repo."""
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw).expanduser()
    return deployed if deployed.exists() else fallback


def _read_secret(path: Path) -> str | None:
    """Read a single-line secret from a file, if it exists."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


@dataclass
class Config:
    # --- filesystem ------------------------------------------------------
    #: Writable runtime state: uploads, parsed records, the undo log.
    state_dir: Path = field(
        default_factory=lambda: _preferred("WAYFARE_STATE_DIR", USER_STATE_DIR, REPO_ROOT / "var")
    )
    #: Reference data (airport database). Downloaded by deploy/setup.sh.
    data_dir: Path = field(default_factory=lambda: _env_path("WAYFARE_DATA_DIR", REPO_ROOT / "data"))
    #: OAuth client secret + stored user token. Kept OUTSIDE the repo.
    secrets_dir: Path = field(
        default_factory=lambda: _preferred(
            "WAYFARE_SECRETS_DIR", USER_CONFIG_DIR / "secrets", REPO_ROOT / "secrets"
        )
    )

    # --- google calendar -------------------------------------------------
    #: How much of the calendar this instance asks for.
    #:
    #: "full" — the calendar scope. Events can land on your own primary
    #:   calendar, and existing events can be read to spot a booking you
    #:   already have. Google classes this as sensitive, so consent shows the
    #:   unverified-app warning.
    #: "app" — the calendar.app.created scope. Not sensitive, so the warning
    #:   goes away, but the app can only touch calendars it created itself:
    #:   travel appears on its own calendar rather than your primary one, and
    #:   duplicate detection is limited to what this tool has written.
    scope_mode: str = field(
        default_factory=lambda: os.environ.get("WAYFARE_SCOPE_MODE", "full").strip().lower()
    )
    #: Calendar that clean, fully validated events land on, in "full" mode.
    calendar_id: str = field(default_factory=lambda: os.environ.get("WAYFARE_CALENDAR_ID", "primary"))
    #: In "app" mode, the app-owned calendar that promoted events land on.
    target_calendar_name: str = field(
        default_factory=lambda: os.environ.get("WAYFARE_TARGET_CALENDAR", "Travel")
    )
    #: Summary of the quarantine calendar. Created on first use if absent.
    pending_calendar_name: str = field(
        default_factory=lambda: os.environ.get("WAYFARE_PENDING_CALENDAR", "Travel (pending)")
    )
    #: How many models read each document independently.
    #:
    #: A second reading catches the dominant failure of a free model — an
    #: absent value rather than a wrong one — and it is worth having whenever
    #: there is budget for it. On a free tier there is not. Measured over six
    #: documents on the same day: a quorum of two read 12 legs of 22 and a
    #: quorum of one read 17, because the second reading spent the daily
    #: allowance of the only provider still answering and later documents got
    #: nothing at all. A cross-check that costs a whole document is not a
    #: cross-check worth making.
    #:
    #: So one by default, and raise it where the budget is real: a paid tier,
    #: a local endpoint, or a day with two providers answering. Nothing else
    #: about consensus changes — set it to two and every guarantee described in
    #: `extractors/consensus.py` applies again.
    llm_quorum: int = field(
        default_factory=lambda: max(1, int(os.environ.get("WAYFARE_LLM_QUORUM", "1")))
    )
    #: How long a second opinion may keep the first one waiting. Free models
    #: range from seconds to minutes, and a cross-check is worth some delay but
    #: not an unbounded one.
    llm_quorum_grace: float = field(
        default_factory=lambda: float(os.environ.get("WAYFARE_LLM_QUORUM_GRACE", "25"))
    )
    #: Last-resort zone for a record whose own timezone could not be resolved.
    #: Google rejects an event whose time carries no zone at all, so a held
    #: record needs *some* zone to be written to the pending calendar.
    timezone: str = field(
        default_factory=lambda: os.environ.get("WAYFARE_TIMEZONE") or _host_timezone()
    )

    # --- model backend ---------------------------------------------------
    #: OpenAI-compatible base URL for the free model that interprets OCR text.
    llm_base_url: str = field(
        default_factory=lambda: os.environ.get("WAYFARE_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    )
    #: Extra endpoints to read documents with, as
    #: ``name=https://host/v1[,name=...]``. Free tiers rotate and cap
    #: independently, so the provider that is spent today is not the one that
    #: will be spent tomorrow — measured, a whole day's free budget was gone on
    #: one endpoint while another answered in under a second. modelchain ships
    #: Zen, Nous and OpenRouter as known names, so listing a name alone is
    #: enough to turn one on.
    llm_extra_providers: str = field(
        default_factory=lambda: os.environ.get("WAYFARE_LLM_PROVIDERS", "zen")
    )
    #: Models to use on an endpoint whose catalogue does not say what anything
    #: costs, as ``provider=model|model[,provider=...]``. Zen is such a
    #: gateway: it lists everything it sells beside the few it gives away, so
    #: discovery cannot tell them apart and the free ones are named instead.
    #: These are wrong the day the provider changes them, which is why they are
    #: configuration and not code.
    llm_provider_models: str = field(
        #: Only big-pickle by default: measured keyless from this machine,
        #: hy3 answered 401. A model that needs a key nobody has is a slot in
        #: the chain spent on a certain failure.
        default_factory=lambda: os.environ.get(
            "WAYFARE_LLM_PROVIDER_MODELS", "zen=big-pickle"
        )
    )
    #: Generous, because the free models that answer at all are slow: measured,
    #: big-pickle read a one-flight receipt correctly in 63 seconds, and the
    #: old 90-second ceiling turned ordinary variance into "could not be read".
    #: Nobody waits on this in silence any more — the page reports the stage it
    #: has reached, and a second opinion gets a scaled window of its own.
    llm_timeout: float = field(default_factory=lambda: float(os.environ.get("WAYFARE_LLM_TIMEOUT", "180")))
    #: How many other free models to fall back to when the chosen one is busy.
    llm_fallbacks: int = field(
        default_factory=lambda: int(os.environ.get("WAYFARE_LLM_FALLBACKS", "3"))
    )

    # --- optional live flight schedule lookup ----------------------------
    #: Provider for the flight-number schedule check. "none" disables it.
    schedule_provider: str = field(
        default_factory=lambda: os.environ.get("WAYFARE_SCHEDULE_PROVIDER", "none")
    )

    # --- promotion policy ------------------------------------------------
    #: Minimum overall confidence for an event to auto-promote to the real calendar.
    promote_threshold: float = field(
        default_factory=lambda: float(os.environ.get("WAYFARE_PROMOTE_THRESHOLD", "0.85"))
    )
    #: When false, everything is held in pending regardless of confidence.
    auto_promote: bool = field(default_factory=lambda: _env_bool("WAYFARE_AUTO_PROMOTE", True))

    #: Public origin of the microsite, e.g. "https://wayfare.example.com".
    #: Used to build the OAuth redirect URI. Derived from the request when
    #: unset, which is only correct if nginx forwards the original scheme.
    base_url: str = field(default_factory=lambda: os.environ.get("WAYFARE_BASE_URL", "").rstrip("/"))

    # --- external tools --------------------------------------------------
    tesseract_bin: str = field(default_factory=lambda: os.environ.get("WAYFARE_TESSERACT", "tesseract"))
    pdftotext_bin: str = field(default_factory=lambda: os.environ.get("WAYFARE_PDFTOTEXT", "pdftotext"))
    pdftoppm_bin: str = field(default_factory=lambda: os.environ.get("WAYFARE_PDFTOPPM", "pdftoppm"))
    zbarimg_bin: str = field(default_factory=lambda: os.environ.get("WAYFARE_ZBARIMG", "zbarimg"))
    kitinerary_bin: str = field(
        default_factory=lambda: os.environ.get("WAYFARE_KITINERARY", "kitinerary-extractor")
    )

    # --- derived paths ---------------------------------------------------
    @property
    def uploads_dir(self) -> Path:
        return self.state_dir / "uploads"

    @property
    def records_dir(self) -> Path:
        return self.state_dir / "records"

    @property
    def undo_log(self) -> Path:
        return self.state_dir / "undo.jsonl"

    @property
    def airports_csv(self) -> Path:
        return self.data_dir / "airports.csv"

    @property
    def oauth_client_secret(self) -> Path:
        return self.secrets_dir / "client_secret.json"

    @property
    def oauth_token(self) -> Path:
        return self.secrets_dir / "token.json"

    # --- credentials -----------------------------------------------------
    @property
    def llm_model(self) -> str:
        """Model name: environment, then the setup page's choice, then default."""
        return (
            os.environ.get("WAYFARE_LLM_MODEL")
            or _read_secret(self.secrets_dir / "llm_model")
            or DEFAULT_LLM_MODEL
        )

    @property
    def llm_api_key(self) -> str | None:
        """Model API key, from the environment or the secrets directory."""
        return os.environ.get("WAYFARE_LLM_API_KEY") or _read_secret(self.secrets_dir / "llm_api_key")

    def provider_key(self, name: str) -> str | None:
        """The key for one endpoint, or None where it needs none.

        Each provider keeps its key beside the others under its own name, so
        adding a second endpoint is dropping a file in rather than editing
        anything. The unnamed key stays the default provider's, which is what
        every install written before this had.
        """
        return (
            os.environ.get(f"WAYFARE_LLM_KEY_{name.upper()}")
            or _read_secret(self.secrets_dir / f"llm_api_key.{name}")
            or (self.llm_api_key if name == self.default_provider else None)
        )

    @property
    def default_provider(self) -> str:
        return self.providers.default

    @property
    def providers(self):
        """Every endpoint this install may read documents with."""
        from .vendor import modelchain

        configured: dict[str, dict] = {}
        for item in (self.llm_extra_providers or "").split(","):
            item = item.strip()
            if not item:
                continue
            name, _, url = item.partition("=")
            configured.setdefault(name.strip(), {})
            if url.strip():
                configured[name.strip()]["api_base"] = url.strip()

        for item in (self.llm_provider_models or "").split(","):
            name, _, models = item.partition("=")
            listed = [m.strip() for m in models.split("|") if m.strip()]
            if name.strip() and listed:
                configured.setdefault(name.strip(), {})["models"] = listed

        return modelchain.Providers(configured, base_url=self.llm_base_url)

    @property
    def enabled_providers(self) -> list[str]:
        """The endpoints to actually read with, default first.

        modelchain knows the address of every provider it ships, which is not
        the same as being allowed to use it: one of them wants a key nobody
        here has. So an endpoint is used only when it is named here, and the
        default — the single endpoint every earlier install had — is always
        first.
        """
        names = [self.default_provider]
        for item in (self.llm_extra_providers or "").split(","):
            name = item.partition("=")[0].strip()
            if name and name not in names:
                names.append(name)
        return names

    @property
    def llm_disabled(self) -> bool:
        """Skip the model entirely, key or no key.

        Deleting the key is not a way to ask for this — the key lives in the
        secrets directory and the environment cannot unset it. Benchmarking a
        corpus needs the deterministic extractors on their own, and on a free
        tier capped at 50 requests a day, running a hundred documents through
        a model by accident spends the whole day's budget in a minute.
        """
        return os.environ.get("WAYFARE_DISABLE_LLM", "").strip().lower() in {"1", "true", "yes"}

    @property
    def schedule_api_key(self) -> str | None:
        return os.environ.get("WAYFARE_SCHEDULE_API_KEY") or _read_secret(
            self.secrets_dir / "schedule_api_key"
        )

    @property
    def agent_token(self) -> str | None:
        """Bearer token for the scoped agent API (ingest-to-pending only)."""
        return os.environ.get("WAYFARE_AGENT_TOKEN") or _read_secret(self.secrets_dir / "agent_token")

    @property
    def owner_token(self) -> str | None:
        """Bearer token for the full API, including promote and undo."""
        return os.environ.get("WAYFARE_OWNER_TOKEN") or _read_secret(self.secrets_dir / "owner_token")

    def ensure_dirs(self) -> None:
        for path in (self.state_dir, self.uploads_dir, self.records_dir, self.data_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def generate_token() -> str:
    """A fresh URL-safe bearer token, for deploy/setup.sh."""
    return secrets.token_urlsafe(32)
