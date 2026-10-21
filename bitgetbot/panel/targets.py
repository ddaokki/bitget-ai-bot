"""User take-profit prices that SafeTrendLive reads from freqtrade/user_data/manual_targets.json.

The strategy exits a trade at market once the price touches its target; the exchange stop stays
in place until then. Dry and live keep separate sections because their databases both number trades
from 1, and each target names its trade's open time (ms) so a later trade reusing the id gets none.
Format: {"take_profit": {"live": {"<trade id>": {"price": p, "open_ts": ms}}, "dry": {...}}}.
"""

import json
import os
import tempfile
from pathlib import Path

PATH = Path("freqtrade/user_data/manual_targets.json")  # relative to the repo root (panel cwd)
SECTIONS = {True: "dry", False: "live"}


def _section(raw) -> dict[int, dict]:
    targets = {}
    if not isinstance(raw, dict):
        return targets
    for key, entry in raw.items():
        try:
            trade_id = int(key)
        except (TypeError, ValueError, OverflowError):
            continue
        if not isinstance(entry, dict):
            continue
        price, open_ts = entry.get("price"), entry.get("open_ts")
        try:
            valid_price = type(price) in (int, float) and 0 < float(price) < float("inf")
        except OverflowError:
            valid_price = False
        if valid_price and type(open_ts) is int and open_ts > 0:
            targets[trade_id] = {"price": float(price), "open_ts": open_ts}
    return targets


def load(path: Path = PATH) -> dict[bool, dict[int, dict]]:
    """{dry_run: {trade id: {"price", "open_ts"}}}; a missing or malformed file means none."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    sections = data.get("take_profit") if isinstance(data, dict) else None
    sections = sections if isinstance(sections, dict) else {}
    return {dry_run: _section(sections.get(name)) for dry_run, name in SECTIONS.items()}


def price_for(saved: dict[bool, dict[int, dict]], trade_id: int, open_ts, dry_run: bool) -> float | None:
    entry = saved.get(dry_run, {}).get(trade_id)
    if entry is None or entry["open_ts"] != open_ts:
        return None
    return entry["price"]


def _save(saved: dict[bool, dict[int, dict]], path: Path) -> None:
    payload = {"take_profit": {name: {str(key): value for key, value in sorted(saved.get(dry_run, {}).items())}
                               for dry_run, name in SECTIONS.items()}}
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        # Readable by the bot container (uid 1000) even when the panel runs with a 077 umask.
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def set_target(trade_id: int, price: float | None, open_ts: int, dry_run: bool,
               open_trades: dict[int, int], path: Path = PATH) -> dict[bool, dict[int, dict]]:
    """Set (or clear with None) one trade's target in this mode's section and drop this mode's
    targets of trades no longer open (open_trades: open trade id -> open time ms, this mode).
    The other mode's section is left alone: that bot may still hold its trades."""
    saved = load(path)
    section = {key: entry for key, entry in saved[dry_run].items() if open_trades.get(key) == entry["open_ts"]}
    if price is None:
        section.pop(trade_id, None)
    else:
        section[trade_id] = {"price": float(price), "open_ts": open_ts}
    saved[dry_run] = section
    _save(saved, path)
    return saved
