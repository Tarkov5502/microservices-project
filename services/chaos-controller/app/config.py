"""
chaos-controller/app/config.py

Configuration for the Chaos Theater controller service.

The controller has two operating modes:

  MOCK MODE (default for local dev):
    No real Kubernetes cluster needed. Chaos actions are simulated against an
    in-memory model of the cluster. Events, metrics, and recovery timelines
    are generated to mirror what a real cluster would produce. This means
    the frontend can be developed and demoed without any infrastructure.

  LIVE MODE:
    Connects to a real Kubernetes cluster via the in-cluster ServiceAccount
    (production) or a local kubeconfig (dev). Watches real events, executes
    real pod deletes / network disruptions / scale changes, polls real
    Prometheus metrics. Set CHAOS_MODE=live and provide the relevant
    credentials.

  Sensible defaults below let `docker compose up chaos-controller` work
  immediately. Production sets these via Helm values / K8s env vars.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Operating mode ────────────────────────────────────────────────
    # mock = simulated cluster (no K8s required). live = real cluster.
    chaos_mode: str = "mock"

    # ── Kubernetes target ─────────────────────────────────────────────
    # Only used in live mode. In-cluster ServiceAccount is preferred;
    # the kubeconfig path is a dev fallback.
    k8s_namespace: str = "microservices"
    k8s_in_cluster: bool = True
    k8s_kubeconfig: str = ""

    # ── Prometheus integration ─────────────────────────────────────────
    # Used to fetch live latency / RPS / error rate panels. Only used in
    # live mode. Service URL inside the cluster.
    prometheus_url: str = "http://prometheus.monitoring:9090"
    prometheus_poll_interval_sec: float = 1.0

    # ── Health probes ──────────────────────────────────────────────────
    # The controller probes each upstream service every N ms during a
    # chaos event to produce the user-facing impact timeline. This is what
    # makes "the outage window" visible — actual failed requests, not just
    # K8s events.
    probe_interval_ms: int = 200
    probe_targets: str = "api-gateway:8000,user-service:8001,task-service:8002,notification-service:8003"

    # ── CORS ───────────────────────────────────────────────────────────
    # The frontend will typically be on the same origin (served by the
    # gateway) in production, so this is mostly relevant for local dev.
    allowed_origins: str = "http://localhost:3000,http://localhost:5173,http://localhost:8000,http://localhost:8080"

    # ── Safety guardrails ──────────────────────────────────────────────
    # Hard limit on chaos events per minute to prevent runaway scripts
    # from frying a real cluster. Set to a high value in mock mode.
    max_events_per_minute: int = 60

    # ── Environment ────────────────────────────────────────────────────
    environment: str = "development"

    class Config:
        env_file = ".env"
        env_prefix = ""

    @property
    def is_mock(self) -> bool:
        return self.chaos_mode.lower() == "mock"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def probe_targets_list(self) -> list[tuple[str, int]]:
        out = []
        for entry in self.probe_targets.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" in entry:
                host, port = entry.split(":", 1)
                out.append((host.strip(), int(port.strip())))
            else:
                out.append((entry, 80))
        return out


settings = Settings()
