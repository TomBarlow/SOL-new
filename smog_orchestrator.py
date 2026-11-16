"""Conservative event-driven SMOG candidate detection (research only).

This is a wake-up filter, NOT trade validation. The proprietary indicator
signals remain authoritative. No orders or LLM calls are made here.
"""
from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

OG = {"long": "og_long", "short": "og_short"}
CHOCH = {"long": "internal_bullish_choch", "short": "internal_bearish_choch"}
FVG = {"long": "bullish_fvg_formed", "short": "bearish_fvg_formed"}
MITIGATED = {"long": "bullish_fvg_mitigated", "short": "bearish_fvg_mitigated"}
SUPPORTED = set(OG.values()) | set(CHOCH.values()) | set(FVG.values()) | set(MITIGATED.values())
WINDOW_MS = 30 * 60_000


@dataclass(frozen=True)
class Event:
    id: int
    event: str
    bar_time_ms: int
    price: float

    def __post_init__(self):
        if self.id <= 0 or self.event not in SUPPORTED or self.bar_time_ms < 0 or self.price <= 0:
            raise ValueError("Invalid event")


@dataclass(frozen=True)
class Candidate:
    key: str
    direction: str
    og_id: int
    choch_id: int
    fvg_id: int
    triggered_by_id: int
    asof_bar_time_ms: int

    def to_payload(self) -> dict:
        return {
            "schema_version": 1,
            "type": "smog_candidate_for_review",
            "candidate_key": self.key,
            "direction": self.direction,
            "event_ids": {"og": self.og_id, "internal_choch": self.choch_id, "fvg_formed": self.fvg_id},
            "triggered_by_event_id": self.triggered_by_id,
            "asof_bar_time_ms": self.asof_bar_time_ms,
            "status": "UNVERIFIED",
            "execution_allowed": False,
            "hermes_tasks": [
                "Retrieve closed 1m/5m candles and ADX at evaluation time",
                "Verify OG and internal CHoCH refer to same directional move",
                "Verify FVG boundaries, mitigation state and associated CHoCH leg",
                "Check 50% retracement, entry, stop and risk rules",
                "Return TAKE, WAIT or SKIP with reasons and supporting evidence"
            ],
        }


def detect_candidates(events: Iterable[Event], window_ms: int = WINDOW_MS) -> list[Candidate]:
    """Replay sorted events; wake once per unique OG/CHoCH/FVG triple.

    Only *previously received* events can contribute to a candidate. FVG may
    arrive before or after CHoCH. Mitigation alerts do not identify individual
    gaps, so a mitigation of the same direction since the FVG conservatively
    blocks that FVG; Hermes must later verify precise gap identity.

    This is intentionally not an exhaustive strategy signal generator.
    """
    if window_ms <= 0:
        raise ValueError("window_ms must be positive")
    history: list[Event] = []
    seen_ids: set[int] = set()
    seen_keys: set[str] = set()
    candidates: list[Candidate] = []
    for current in events:  # delivery order; don't assume bars arrive in order
        if current.id in seen_ids:
            continue
        seen_ids.add(current.id)
        history.append(current)
        for direction in ("long", "short"):
            relevant = [e for e in history
                        if 0 <= current.bar_time_ms - e.bar_time_ms <= window_ms]
            # Ignore events from future-dated bars, and only consider the
            # current arrival when it completes the three-signal combination.
            for e in reversed(relevant):
                if e.event != FVG[direction]:
                    continue
                ogs = [x for x in relevant if x.event == OG[direction]]
                chochs = [x for x in relevant if x.event == CHOCH[direction]]
                if not ogs or not chochs:
                    continue
                og, choch = ogs[-1], chochs[-1]
                if current.id not in (e.id, og.id, choch.id):
                    continue
                ids = (og.id, choch.id, e.id)
                key = sha256(("%s|%s|%s|%s" % (direction, *ids)).encode()).hexdigest()
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                candidates.append(Candidate(key, direction, *ids, current.id,
                                            current.bar_time_ms))
                break
    return candidates
