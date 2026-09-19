"""Bounded private diagnostics and an event-oriented arbitrage operator view."""

import json
import logging
import os
import shutil
import stat
import time
import traceback
from collections import Counter
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from textwrap import wrap
from uuid import uuid4

from .domain import KernelError


def clean(text):
    """Escape terminal controls, including names supplied by token issuers."""
    return "".join(
        c if c.isprintable() else json.dumps(c, ensure_ascii=True)[1:-1]
        for c in str(text)
    )


def ada(quantity):
    sign = "-" if quantity < 0 else ""
    whole, fraction = divmod(abs(quantity), 1_000_000)
    return f"{sign}{whole}.{fraction:06d} tADA"


def asset_label(unit):
    if unit == "lovelace":
        return "ADA"
    try:
        name = bytes.fromhex(unit[56:]).decode("utf-8")
    except (ValueError, UnicodeError):
        name = unit[56:]
    return f"{clean(name)[:64]}[{unit[:8]}]"


def asset_names(units):
    """Display aliases only; full asset identity remains authoritative in diagnostics."""
    names = {}
    for unit in sorted(set(units)):
        try:
            name = (
                "ADA"
                if unit == "lovelace"
                else bytes.fromhex(unit[56:]).decode("utf-8")
            )
        except (ValueError, UnicodeError):
            name = ""
        names[unit] = clean(name)[:64].strip()
    counts = Counter(names.values())
    used = set(names.values())
    number = 0
    for unit, name in names.items():
        if unit != "lovelace" and (not name or counts[name] > 1 or name == "ADA"):
            while True:
                number += 1
                alias = f"token #{number}"
                if alias not in used:
                    break
            names[unit] = alias
            used.add(alias)
    return names


def trade_progress(trade):
    """Human labels never change the durable transaction state or imply a retry."""
    status, net = trade["status"], trade["net_lovelace"]
    if status == "unknown":
        return "checking", ["Awaiting chain evidence; monitoring saved transaction"]
    if status == "included":
        depth, required = (
            trade.get("confirmations"),
            trade.get("confirmations_required"),
        )
        return "confirming", [
            f"confirmations {depth}/{required}"
            if depth is not None and required
            else "Awaiting confirmation depth"
        ]
    if status == "confirmed" and net is None:
        return "verifying", ["On-chain; checking wallet result"]
    if status == "failed" and net is None:
        return "failed", ["Script failed; checking collateral loss"]
    if status == "rolled_back":
        return "rolled back", ["Previous result removed; checking chain"]
    if net is not None:
        return status.replace("_", " "), [f"net {ada(net)}"]
    details = []
    if status == "submitted":
        if trade.get("expected_net_lovelace") is not None:
            details.append(f"expected net {ada(trade['expected_net_lovelace'])}")
        if trade.get("fee_lovelace") is not None:
            details.append(f"fee {ada(trade['fee_lovelace'])}")
    return status.replace("_", " "), details


def error_details(error):
    # Third-party exceptions can repr entire builders/keys. Never format them or locals.
    detail = {"error_type": type(error).__name__}
    detail["reason"] = (
        clean(str(error))[:512]
        if isinstance(error, KernelError)
        else type(error).__name__
    )
    detail["frames"] = [
        {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
        for f in traceback.extract_tb(error.__traceback__)[-12:]
    ]
    if isinstance(error, ValueError) and any(
        f["file"] == "txbuilder.py" and f["function"] == "_set_collateral_return"
        for f in detail["frames"]
    ):
        detail["reason"] = (
            "Configured collateral cannot satisfy this transaction's collateral/return requirements"
        )
    return detail


class SDKFilter(logging.Filter):
    def filter(self, record):
        return not (
            record.name.lower().startswith(("pycardano", "httpx", "httpcore"))
            or record.msg
            == "No default backend could be set. Please set a backend manually."
        )


def configure_sdk_logging():
    """Install before SDK imports; debug output uses our explicit safe fields."""
    root = logging.getLogger()
    root.addFilter(SDKFilter())
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.addFilter(SDKFilter())
    logging.getLogger("PyCardano").disabled = True


class PrivateLog(RotatingFileHandler):
    def __init__(self, path, *, max_bytes=16 * 1024 * 1024, backups=15):
        path = Path(path)
        if path.parent.is_symlink():
            raise KernelError("Diagnostic directory must not be a symlink")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.parent.stat()
        if info.st_uid != os.getuid():
            raise KernelError("Diagnostic directory must belong to the current user")
        path.parent.chmod(0o700)
        super().__init__(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=True
        )
        self._check_files()

    @staticmethod
    def _check(info):
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise KernelError("Diagnostics require owned, unlinked regular files")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise KernelError("Diagnostic file permissions must be 0600")

    def _check_files(self):
        directory = Path(self.baseFilename).parent.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700
        ):
            raise KernelError("Diagnostic directory must be private and owned")
        for suffix in ("", *(f".{n}" for n in range(1, self.backupCount + 1))):
            try:
                self._check(Path(self.baseFilename + suffix).lstat())
            except FileNotFoundError:
                pass

    def _open(self):
        descriptor = os.open(
            self.baseFilename,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            self._check(os.fstat(descriptor))
            return os.fdopen(descriptor, "a", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise

    def doRollover(self):
        self._check_files()
        super().doRollover()

    def handleError(self, record):
        # Stdlib otherwise swallows disk failures. The reporter converts them to a gate.
        raise


def status_text(history):
    lines = []
    for run in history["runs"]:
        last = run["summary"].get("latest", {})
        lines.append(
            f"Run {run['id']} | {run['state']} | {run['config'].get('mode', '?')}"
        )
        lines.append(
            f"  Last update: {datetime.fromtimestamp(run['observed_at']).astimezone().isoformat(timespec='seconds')}"
        )
        observed = last.get("liquidity_observed_at", last.get("observed_at"))
        if observed is not None:
            lines.append(
                f"  Chain observation: {datetime.fromtimestamp(observed).astimezone().isoformat(timespec='seconds')} (saved state; no fresh query)"
            )
        if "funds" in last:
            lines.append(
                "  " + " | ".join(f"{k}: {ada(v)}" for k, v in last["funds"].items())
            )
        lines.append(
            f"  Confirmed net: {ada(run['realized_net_lovelace'])} | unverified outcomes: {run['unverified_outcomes']} | polls: {run['summary'].get('counts', {}).get('polls', 0)}"
        )
        if last.get("reason"):
            lines.append(f"  {clean(last['reason'])}")
        if "loss_headroom_lovelace" in last:
            lines.append(f"  Loss headroom: {ada(last['loss_headroom_lovelace'])}")
        elif "cost_headroom_lovelace" in last:
            lines.append(
                f"  Lifetime cost headroom: {ada(last['cost_headroom_lovelace'])}"
            )
        for trade in run["trades"]:
            status, details = trade_progress(trade)
            lines.append(" | ".join([f"  {status} {trade['txid']}", *details]))
    if not lines:
        lines.append("No arbitrage runs recorded.")
    for incident in history.get("execution_incidents", []):
        lines.append(
            f"Execution paused: investigate script failure {incident['txid']}; acknowledgment required"
        )
    lines.append(f"Pending wallet transactions: {len(history['pending_transactions'])}")
    lines.append(
        f"Historical/unattributed trades: {len(history['legacy_trades'])} (excluded from run totals)"
    )
    return "\n".join(lines)


class Reporter:
    def __init__(
        self,
        journal,
        config,
        *,
        output="text",
        debug=False,
        write=print,
        clock=time.time,
        monotonic=time.monotonic,
    ):
        self.journal, self.config = journal, config
        self.output, self.debug, self.write = output, debug, write
        self.clock, self.monotonic = clock, monotonic
        self.run_id = uuid4().hex
        self.cycle_id, self.phase = 0, "starting"
        self.stop_requested = False
        self.healthy, self.handler = False, None
        self.path = (
            Path(journal.db.execute("PRAGMA database_list").fetchone()[2]).parent
            / "logs/arbitrage.jsonl"
        )
        self.last_health = -float("inf")
        self.last_state = None
        self.last_opportunity = None
        self.last_funds = None
        self.last_allocation = None
        self.previous_trades = {}
        self.latest = {}
        self.asset_units = set(config.assets)
        self.labels = {}

    def route(self, trade):
        path, hops = trade.get("path", []), trade.get("hops", [])
        if len(path) < 2:
            return ""
        self.asset_units.update(path)
        labels = asset_names(self.asset_units)
        if labels != self.labels:
            self.labels = labels
            self.event(
                "asset_labels", {"labels": labels}, level="DEBUG", visible=self.debug
            )
        label = asset_label if self.debug else labels.__getitem__
        swaps = []
        for index, (left, right) in enumerate(zip(path, path[1:])):
            hop = hops[index] if index < len(hops) else {}
            # Never guess a venue from a ticker, including for historical records.
            venue = {
                "swaps-v1": "Swaps",
                "swaps-v1-two-way": "Swaps",
                "dano": "Dano",
                "splash": "Splash",
                "genius-yield": "Genius",
                "saturnswap": "Saturn",
            }.get(hop.get("venue"))
            if hop.get("input_unit") != left or hop.get("output_unit") != right:
                venue = None
            swaps.append(
                f"{label(left)}->{label(right)}({venue or 'venue unavailable'})"
            )
        return f"{len(swaps)}-way hop · " + ", ".join(swaps)

    def _save(self, event, *, sync=False):
        try:
            if self.handler is None:
                self.handler = PrivateLog(self.path)
            self.handler._check_files()
            if self.handler.stream is not None:
                info = os.fstat(self.handler.stream.fileno())
                self.handler._check(info)
                named = Path(self.handler.baseFilename).lstat()
                if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
                    raise KernelError("Diagnostic file changed while open")
            payload = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
            if len(payload) > min(65536, self.handler.maxBytes // 2):
                event = {k: v for k, v in event.items() if k != "data"}
                event["data"] = {
                    "record_truncated": True,
                    "original_bytes": len(payload),
                }
                payload = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
            if len(payload) + 1 > self.handler.maxBytes:
                raise KernelError("Diagnostic record exceeds file limit")
            self.handler.emit(
                logging.LogRecord("kernel", logging.INFO, "", 0, payload, (), None)
            )
            if sync:
                self.handler.flush()
                os.fsync(self.handler.stream.fileno())
            self.healthy = True
        except Exception:
            # Never throw through a provider response and obscure a successful wire submission.
            self.healthy = False
            if self.handler is not None:
                try:
                    self.handler.close()
                except Exception:
                    pass
                self.handler = None
        return self.healthy

    def _display(self, event, message):
        try:
            if self.output == "jsonl":
                self.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")))
            elif self.output == "text":
                stamp = (
                    datetime.fromtimestamp(event["observed_at"])
                    .astimezone()
                    .strftime("%H:%M:%S")
                )
                prefix = f"{stamp} {event['namespace']:9} {event['event'].upper():14} {event['display_status']:14} | "
                message = (
                    " | ".join(message)
                    if isinstance(message, (tuple, list))
                    else message
                )
                if self.debug or event["level"] in ("WARN", "ERROR"):
                    message = f"[{event['level']}] {message}"
                width = max(
                    20,
                    shutil.get_terminal_size(fallback=(120, 24)).columns - len(prefix),
                )
                for line in wrap(
                    clean(message), width=width, break_on_hyphens=False
                ) or [""]:
                    self.write(prefix + line)
                    # Keep wrapped lines with their namespace when filtering text.
                    prefix = f"{stamp} {event['namespace']:9} {'':14} {'':14}   "
        except Exception:
            self.stop_requested = True

    def event(
        self,
        name,
        data=None,
        *,
        namespace="ARBITRAGE",
        status="",
        message="",
        level="INFO",
        visible=True,
        sync=False,
    ):
        now = self.clock()
        event = {
            "schema_version": 1,
            "timestamp": datetime.fromtimestamp(now, UTC).isoformat(),
            "observed_at": now,
            "namespace": namespace,
            "level": level,
            "event": name,
            "display_status": status
            or {"WARN": "warning", "ERROR": "error"}.get(level, ""),
            "run_id": self.run_id,
            "cycle_id": self.cycle_id,
            "phase": self.phase,
            "network": self.journal.profile.name,
            "wallet": self.journal.profile.wallet_id,
            "data": data or {},
        }
        was_healthy = self.healthy
        self._save(event, sync=sync)
        if not self.healthy and (was_healthy or self.cycle_id == 0):
            failure = {
                **event,
                "namespace": "SYSTEM",
                "level": "ERROR",
                "event": "logging_failed",
                "display_status": "blocked",
                "data": {},
            }
            self._display(
                failure,
                "Diagnostic write failed; new submissions paused, reconciliation remains enabled",
            )
        if visible or self.output == "jsonl" and (level != "DEBUG" or self.debug):
            self._display(event, message or clean(json.dumps(data or {})))
        return self.healthy

    def start(self, mode):
        from dataclasses import asdict

        config = {
            **asdict(self.config),
            "fingerprint": self.config.fingerprint,
            "mode": mode,
        }
        self.journal.start_arbitrage_run(self.run_id, self.clock(), config)
        self.previous_trades = self._trades()
        self.asset_units.update(
            u for t in self.previous_trades.values() for u in t.get("path", [])
        )
        self.last_health = self.monotonic()
        self.event(
            "start",
            {"config": config, "log_path": str(self.path)},
            namespace="SYSTEM",
            status="starting",
            message=(
                f"{self.journal.profile.name} | {mode} | wallet {self.journal.profile.wallet_id}",
                f"{datetime.fromtimestamp(self.clock()).astimezone().isoformat(timespec='seconds')} | run {self.run_id[:8]}",
                f"Drawdown limit {ada(self.config.max_drawdown_lovelace)} | {len(self.config.assets)} allowed assets",
            ),
        )
        self.event(
            "logs",
            {"path": str(self.path)},
            namespace="SYSTEM",
            status="file",
            message=str(self.path),
        )
        token_env = self.journal.profile.token_env
        profile = self.journal.profile
        bindings = profile.capabilities
        access = (
            tuple(
                f"{capability} {name} ({profile.providers[name].kind})"
                for capability, name in bindings.items()
            )
            if bindings
            else (
                f"Koios | token from {token_env}"
                if token_env
                else "Koios | anonymous access"
            )
        )
        self.event(
            "access",
            {"token_env": token_env, "capabilities": bindings},
            namespace="SYSTEM",
            status="configured",
            message=access,
        )
        self.event(
            "allowlist",
            {"assets": list(self.config.assets)},
            message=", ".join(asset_label(u) for u in self.config.assets),
            visible=self.debug,
        )
        names = {
            "swaps-v1": "Swaps one-way",
            "swaps-v1-two-way": "Swaps two-way",
            "dano": "Dano",
            "splash": "Splash",
            "genius-yield": "Genius Yield",
            "saturnswap": "SaturnSwap",
        }
        self.event(
            "venues",
            {"venues": list(self.config.venues)},
            namespace="SYSTEM",
            status="enabled",
            message=", ".join(names[v] for v in self.config.venues),
        )

    def set_phase(self, phase):
        self.phase = phase
        self.event(
            "phase", {"phase": phase}, message=phase, level="DEBUG", visible=self.debug
        )
        self.health()

    def funds(self, values):
        if values != self.last_funds:
            self.event(
                "funds",
                values,
                namespace="SYSTEM",
                status="snapshot",
                message=" | ".join(
                    f"{k} {ada(values[k])}"
                    for k in ("operating", "collateral", "protected")
                ),
            )
            self.last_funds = dict(values)

    def provider_event(self, data):
        if self.debug:
            self.event("request", data, namespace="SYSTEM", level="DEBUG", visible=True)
        self.health()

    def guard(self, intent):
        if self.stop_requested or self.journal.stop_requested():
            raise KernelError("Stop requested before submission")
        if not self.event(
            "submission_check",
            {"intent": intent},
            namespace="SYSTEM",
            visible=False,
            sync=True,
        ):
            raise KernelError("Diagnostic logging unavailable; new submissions paused")
        if self.stop_requested or self.journal.stop_requested():
            raise KernelError("Stop requested before submission")

    def _trades(self):
        history = self.journal.arbitrage_history("all")
        return {
            t["intent"]: t
            for t in [
                *history["legacy_trades"],
                *(t for r in history["runs"] for t in r["trades"]),
            ]
        }

    def health(self, *, force=False):
        if not force and self.monotonic() - self.last_health < 300:
            return
        self.last_health = self.monotonic()
        history = self.journal.arbitrage_history(self.run_id)
        run = history["runs"][0]
        pending = history["pending_transactions"]
        report = self.latest
        stats = report.get("search", {})
        observed = report.get("liquidity_observed_at")
        data = {
            "state": report.get("stage", "starting"),
            "phase": self.phase,
            "counts": run["summary"].get("counts", {}),
            "realized_net_lovelace": run["realized_net_lovelace"],
            "unverified_outcomes": run["unverified_outcomes"],
            "edge_count": report.get("edge_count"),
            "search": stats,
            "timings": report.get("timings", {}),
            "observation_age_seconds": max(0, self.clock() - observed)
            if observed is not None
            else None,
            "pending": len(pending),
            "loss_headroom_lovelace": report.get("loss_headroom_lovelace"),
            "logging_healthy": self.healthy,
        }
        counts = data["counts"]
        state = data["state"].replace("_", " ")
        if state != "paused":
            if any(t["status"] in ("unknown", "rolled_back") for t in pending):
                state = "checking"
            elif pending:
                state = (
                    "confirming"
                    if all(t["status"] == "included" for t in pending)
                    else "pending"
                )
            elif run["unverified_outcomes"]:
                state = "verifying"
            elif state in ("pending", "submitted", "prepared", "recovered"):
                state = "scanning"
        message = [
            f"run net {ada(run['realized_net_lovelace']):<15} | pending {data['pending']:<2}",
            f"uptime {max(0, self.clock() - run['started_at']) / 60:.0f}m",
        ]
        headroom = data["loss_headroom_lovelace"]
        message.append(
            f"loss headroom {ada(headroom) if headroom is not None else 'unknown'}"
        )
        age = data["observation_age_seconds"]
        message.append(
            f"market {age:.0f}s old" if age is not None else "market not observed"
        )
        if not self.healthy:
            message.append("logs FAILED")
        if self.debug:
            message.append(
                f"polls {counts.get('polls', 0)} | evaluated {counts.get('evaluated_candidates', 0)} | rejected {counts.get('build_rejections', 0)} | logs {'ok' if self.healthy else 'FAILED'}"
            )
            if stats.get("search_truncated"):
                message.append("search limited")
        if report.get("reason") and self.debug:
            message.append(report["reason"])
        if run["unverified_outcomes"]:
            message.append(f"verifying {run['unverified_outcomes']} result(s)")
        self.event(
            "health",
            data,
            status=state,
            message=message,
            level="WARN" if not self.healthy else "INFO",
        )

    def transactions(self):
        """Publish durable transitions now; cycle emission reuses the same deduplication."""
        current = self._trades()
        for intent, trade in current.items():
            old = self.previous_trades.get(intent)
            if old is None or any(
                old[k] != trade[k] for k in ("status", "net_lovelace", "block_hash")
            ):
                status, details = trade_progress(trade)
                if trade["status"] == "submitted" and trade.get("path"):
                    details.append(self.route(trade))
                details.append(f"tx {trade['txid'][:12]}")
                if trade["run_id"] != self.run_id:
                    details.append(f"run {(trade['run_id'] or 'historical')[:8]}")
                self.event(
                    "transaction",
                    trade,
                    status=status,
                    message=details,
                    level="WARN"
                    if trade["status"]
                    in ("failed", "expired", "conflicted", "rolled_back")
                    else "INFO",
                    visible=self.debug
                    or trade["status"] not in ("prepared", "aborted"),
                )
        self.previous_trades = current

    def cycle(self, report):
        report.update(
            run_id=self.run_id,
            cycle_id=self.cycle_id,
            phase=self.phase,
            logging_healthy=self.healthy,
        )
        # Reconciliation-only polls have no new market/funds observation.
        self.latest = {**self.latest, **report, "reason": report.get("reason")}
        # Reports contain public plans/metrics only, never dependency bodies or keys.
        self.event(
            "cycle", report, visible=self.debug, level="DEBUG" if self.debug else "INFO"
        )
        self.transactions()
        state = (report["stage"], report.get("reason"))
        if state != self.last_state:
            self.event(
                "state",
                {
                    "stage": state[0],
                    "reason": state[1],
                    "retry_after_seconds": report.get("retry_after_seconds"),
                },
                status="blocked"
                if state[0] == "paused"
                else state[0].replace("_", " "),
                message=(state[1] or "")
                + (
                    f" | retry after {report['retry_after_seconds']}s"
                    if report.get("retry_after_seconds")
                    else ""
                ),
                level="WARN" if state[0] == "paused" else "INFO",
                visible=self.debug or state[0] in ("paused", "no_opportunity"),
            )
            if state[0] != "recovered":
                self.last_state = state
        selected = (
            report.get("selected")
            if report["stage"] in ("evaluated", "submitted")
            else None
        )
        allocation = report.get("allocation")
        if allocation is not None and allocation != self.last_allocation:
            self.event(
                "allocation",
                allocation,
                status=report["stage"],
                message=f"restore operating {ada(allocation['operating_target_lovelace'])} | fee {ada(allocation['fee_lovelace'])}"
                + (
                    f" | create collateral {ada(allocation['collateral_created_lovelace'])}"
                    if allocation["collateral_created_lovelace"]
                    else ""
                ),
            )
        self.last_allocation = allocation
        identity = (
            None
            if selected is None
            else (selected["route_id"], selected["net_profit_lovelace"])
        )
        if identity is not None and identity != self.last_opportunity:
            route = self.route(selected)
            self.event(
                "opportunity",
                {
                    k: selected[k]
                    for k in ("route_id", "path", "fee_lovelace", "net_profit_lovelace")
                }
                | {"hops": selected.get("hops", []), "txid": report.get("txid")},
                status="expected",
                message=(
                    f"expected net {ada(selected['net_profit_lovelace'])} | fee {ada(selected['fee_lovelace'])}",
                    f"{route} (not realized)",
                ),
                visible=self.debug or report["stage"] == "evaluated",
            )
        self.last_opportunity = identity
        if "funds" in report:
            self.funds(report["funds"])
        report["logging_healthy"] = self.healthy
        self.journal.update_arbitrage_run(self.run_id, self.clock(), report)
        self.health(force=self.cycle_id == 1)
        report["logging_healthy"] = self.healthy
        if self.output == "json":
            try:
                self.write(json.dumps(report, indent=2, ensure_ascii=True))
            except Exception:
                self.stop_requested = True

    def finish(self, state):
        self.journal.finish_arbitrage_run(self.run_id, self.clock(), state)
        history = self.journal.arbitrage_history(self.run_id)
        run = history["runs"][0]
        pending = len(history["pending_transactions"])
        self.event(
            "stop",
            {
                "state": state,
                "pending": pending,
                "realized_net_lovelace": run["realized_net_lovelace"],
                "unverified_outcomes": run["unverified_outcomes"],
            },
            namespace="SYSTEM",
            status=state,
            message=f"run net {ada(run['realized_net_lovelace']):<15} | pending {pending}"
            + (
                f" | verifying {run['unverified_outcomes']} result(s)"
                if run["unverified_outcomes"]
                else ""
            )
            + (
                " | restart reconciles saved transaction bytes"
                if pending or run["unverified_outcomes"]
                else ""
            ),
        )
        if self.handler is not None:
            self.handler.close()
