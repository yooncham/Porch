"""The stand-in Engine: check the policy, hold risky actions for the owner, and drive the hub.

Every allowed action goes: policy -> dry run on the digital copy -> execute -> the hub's
read-back. The agent gets the confirmed state, not just an acknowledgement.
"""

import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from .audit import Audit, StageFeed
from .policy import Scope, Verdict, decide

APPROVAL_TTL_S = 10 * 60
VERBS = {"LockDoor": "locked", "UnlockDoor": "unlocked", "Start": "started", "Stop": "stopped", "Pause": "paused",
         "Resume": "resumed", "On": "turned on", "Off": "turned off", "Toggle": "toggled", "Open": "opened",
         "Close": "closed", "GoHome": "sent home"}


class HubUnavailable(Exception):
    pass


@dataclass
class Approval:
    id: str
    device_id: str
    device_name: str
    command: str
    params: dict[str, Any]
    reason: str
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending, approved, denied, used

    def expired(self) -> bool:
        return time.time() - self.created_at > APPROVAL_TTL_S

    def public(self) -> dict[str, Any]:
        return {"approval_id": self.id, "device_id": self.device_id, "device": self.device_name,
                "command": self.command, "params": self.params, "reason": self.reason,
                "status": "expired" if self.expired() and self.status == "pending" else self.status}


def flatten(clusters: dict[str, Any]) -> dict[str, Any]:
    """{"DoorLock": {"attributes": {"LockState": "Locked"}}} -> {"DoorLock.LockState": "Locked"}: easier for agents."""
    return {f"{cluster}.{name}": value
            for cluster, body in (clusters or {}).items() for name, value in body.get("attributes", {}).items()}


def describe(command: str, params: dict[str, Any]) -> str:
    if command == "SetDelayStart":
        return f"set to start in {params.get('hours', 0)} h" + (f" {params['minutes']} min" if params.get("minutes") else "")
    return VERBS.get(command, command)


class EngineLite:
    def __init__(self, hub: httpx.AsyncClient, agent_name: str, scope: Scope, audit: Audit,
                 stage: StageFeed | None = None) -> None:
        self._hub = hub
        self.agent_name = agent_name
        self.scope = scope
        self._audit = audit
        self._stage = stage
        self._approvals: dict[str, Approval] = {}

    # Tools the agent calls

    async def list_devices(self) -> dict[str, Any]:
        devices = await self._get("/devices")
        result = []
        for device in devices:
            commands = sorted({c for cluster in device["clusters"].values() for c in cluster["commands"]})
            decisions = {c: decide(self.scope, device["device_type"], c).verdict for c in commands}
            result.append({
                "device_id": device["id"], "name": device["name"], "room": device["room"],
                "type": device["device_type"], "online": device["online"], "state": flatten(device["clusters"]),
                "you_may": [c for c, v in decisions.items() if v == Verdict.ALLOW],
                "needs_owner_approval": [c for c, v in decisions.items() if v == Verdict.NEEDS_OWNER_APPROVAL],
            })
        self._audit.log("REQUEST", f"{self.agent_name} listed {len(result)} devices")
        return {"agent": self.agent_name, "scope": self.scope.label, "devices": result}

    async def preview_action(self, device_id: str, command: str, params: dict[str, Any]) -> dict[str, Any]:
        device = await self._device(device_id)
        decision = decide(self.scope, device["device_type"], command)
        preview = await self._post(f"/devices/{device_id}/simulate", {"command": command, "params": params})
        self._audit.log("DRY RUN", f"{self.agent_name} previewed {command} on {device['name']}: "
                        f"{'would succeed' if preview['would_succeed'] else 'would fail'} · policy {decision.verdict}")
        return {
            "device_id": device_id, "device": device["name"], "command": command, "params": params,
            "policy": decision.verdict.value, "policy_reason": decision.reason,
            "would_succeed": preview["would_succeed"], "reason": preview.get("reason"),
            "current_state": flatten(device["clusters"]), "predicted_state": flatten(preview["simulated_state_after"]),
            "estimated_latency_ms": preview["estimated_latency_ms"], "control_path": preview.get("control_path"),
            "note": "Nothing was sent to the device.",
        }

    async def perform_action(self, device_id: str, command: str, params: dict[str, Any], reason: str,
                             approval_id: str | None = None) -> dict[str, Any]:
        device = await self._device(device_id)
        name = device["name"]
        self._audit.log("REQUEST", f"{self.agent_name} → {command} {json.dumps(params) if params else ''} on {name} "
                        f"({device_id}) · reason: {reason or '—'}")

        decision = decide(self.scope, device["device_type"], command)
        if decision.verdict == Verdict.DENY:
            self._audit.log("DENY", decision.reason)
            await self._stage_safety("scope_violation", "Out of scope", f"{command} on {name} denied → {decision.reason}",
                                     device_id, command)
            return {"status": "denied", "device": name, "command": command, "reason": decision.reason,
                    "note": "Nothing was sent to the device."}

        if decision.verdict == Verdict.NEEDS_OWNER_APPROVAL:
            held = self._check_approval(approval_id, device_id, command, params)
            if held is not None:
                return held
            if approval_id is None:
                approval = self._hold(device, command, params, reason)
                self._audit.log("HOLD", f"{decision.reason}: approval {approval.id} sent to the owner")
                await self._stage_safety("approval_required", "Owner approval required",
                                         f"{command} on {name} held → Approval request sent to owner phone",
                                         device_id, command)
                return {"status": "needs_owner_approval", "approval_id": approval.id, "device": name,
                        "command": command, "reason": decision.reason,
                        "next_step": "Tell the user the owner must approve. Once they have, call perform_action again "
                                     "with the same device, command and params and this approval_id.",
                        "note": "Nothing was sent to the device."}
            self._audit.log("APPROVED", f"owner approved {approval_id}; carrying out {command} on {name}")
        else:
            self._audit.log("ALLOW", decision.reason)

        return await self._execute(device, command, params)

    # Owner decisions (not tools: an agent can never approve its own request)

    def approvals(self) -> list[dict[str, Any]]:
        return [a.public() for a in self._approvals.values() if a.status == "pending" and not a.expired()]

    async def answer(self, approval_id: str, approved: bool) -> dict[str, Any]:
        approval = self._approvals.get(approval_id)
        if approval is None or approval.status != "pending" or approval.expired():
            return {"error": f"no pending approval {approval_id}"}
        approval.status = "approved" if approved else "denied"
        self._audit.log("APPROVED" if approved else "DENIED",
                        f"owner {'approved' if approved else 'denied'} {approval.command} on {approval.device_name} "
                        f"({approval_id})")
        if self._stage:
            await self._stage.audit(f"Owner {'approved' if approved else 'denied'} {approval.command} on "
                                    f"{approval.device_name}", "done" if approved else "blocked")
        return approval.public()

    # Internals

    async def _execute(self, device: dict[str, Any], command: str, params: dict[str, Any]) -> dict[str, Any]:
        device_id, name = device["id"], device["name"]
        preview = await self._post(f"/devices/{device_id}/simulate", {"command": command, "params": params})
        if not preview["would_succeed"]:
            self._audit.log("REFUSED", f"dry run says it would fail: {preview.get('reason')}; not sent")
            await self._stage_audit(f"{self.agent_name}'s {command} on {name} not sent: {preview.get('reason')}", "failed")
            return {"status": "would_fail", "device": name, "command": command, "reason": preview.get("reason"),
                    "current_state": flatten(device["clusters"]), "note": "Nothing was sent to the device."}
        self._audit.log("DRY RUN", f"would succeed · predicted {self._diff(device['clusters'], preview['simulated_state_after'])}"
                        f" · est. {preview['estimated_latency_ms']} ms")

        key = f"engine-lite-{uuid.uuid4().hex}"
        body = {"command": command, "params": params, "idempotency_key": key}
        try:
            result = await self._post(f"/devices/{device_id}/commands", body)
        except HubUnavailable:
            # Same key: if the first attempt reached the device, the hub replays it instead of acting twice.
            result = await self._post(f"/devices/{device_id}/commands", body)

        confirmed = flatten(result["state_after"])
        if result["status"] == "done":
            self._audit.log("DONE", f"{name} {describe(command, params)} · read-back confirmed in {result['latency_ms']} ms "
                            f"via {result.get('control_path')}{' (digital copy)' if result.get('simulated') else ''}")
            await self._stage_audit(f"{self.agent_name}: {name} {describe(command, params)} · confirmed in "
                                    f"{result['latency_ms']} ms", "done")
        else:
            self._audit.log("FAILED", f"{command} on {name}: {result.get('error')}")
            await self._stage_audit(f"{self.agent_name}'s {command} on {name} failed: {result.get('error')}", "failed")
        return {
            "status": result["status"], "device": name, "device_id": device_id, "command": command, "params": params,
            "confirmed_state": confirmed, "latency_ms": result["latency_ms"], "control_path": result.get("control_path"),
            "simulated": result.get("simulated", False), "error": result.get("error"), "idempotency_key": key,
            "verification": ("The hub read the device back after the command; confirmed_state is that reading."
                             if result["status"] == "done" else "The read-back did not confirm the command."),
        }

    def _hold(self, device: dict[str, Any], command: str, params: dict[str, Any], reason: str) -> Approval:
        approval = Approval(secrets.token_hex(3), device["id"], device["name"], command, params, reason)
        self._approvals[approval.id] = approval
        return approval

    def _check_approval(self, approval_id: str | None, device_id: str, command: str,
                        params: dict[str, Any]) -> dict[str, Any] | None:
        """None when the request may proceed (approved) or needs a new approval; otherwise the answer to return."""
        if approval_id is None:
            return None
        approval = self._approvals.get(approval_id)
        if approval is None or approval.expired():
            return {"status": "needs_owner_approval", "reason": f"approval {approval_id} is unknown or expired",
                    "next_step": "Call perform_action without approval_id to ask the owner again."}
        if (approval.device_id, approval.command, approval.params) != (device_id, command, params):
            self._audit.log("DENY", f"approval {approval_id} was for a different action")
            return {"status": "denied", "reason": f"approval {approval_id} was given for a different action"}
        if approval.status == "pending":
            self._audit.log("HOLD", f"approval {approval_id} not answered yet; still waiting for the owner")
            return {"status": "waiting_for_owner", "approval_id": approval_id,
                    "next_step": "The owner has not answered yet. Ask the user to approve, then try again."}
        if approval.status == "denied":
            self._audit.log("DENY", f"the owner denied approval {approval_id}; nothing sent")
            return {"status": "denied", "reason": "the owner denied this request", "note": "Nothing was sent."}
        if approval.status == "used":
            self._audit.log("DENY", f"approval {approval_id} was already used")
            return {"status": "denied", "reason": f"approval {approval_id} was already used; each covers one action"}
        approval.status = "used"
        return None

    def _diff(self, before: dict[str, Any], after: dict[str, Any]) -> str:
        old, new = flatten(before), flatten(after)
        changes = [f"{k.split('.', 1)[1]} {old.get(k)} → {v}" for k, v in new.items() if old.get(k) != v]
        return ", ".join(changes) or "no change"

    async def _stage_audit(self, summary: str, outcome: str) -> None:
        if self._stage:
            await self._stage.audit(summary, outcome)

    async def _stage_safety(self, kind: str, title: str, detail: str, device_id: str, command: str) -> None:
        if self._stage:
            await self._stage.safety(kind, title, detail, device_id=device_id, command=command)

    async def _device(self, device_id: str) -> dict[str, Any]:
        for device in await self._get("/devices"):
            if device["id"] == device_id:
                return device
        raise LookupError(f"no device {device_id!r}; call list_devices for valid ids")

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        return await self._request("POST", path, body)

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._hub.request(method, path, json=body)
        except httpx.TransportError as exc:
            raise HubUnavailable(f"the Device Hub is unreachable: {exc!r}") from exc
        if response.status_code >= 400:
            detail = response.json().get("detail", response.text) if response.content else response.reason_phrase
            raise ValueError(f"hub refused {method} {path} ({response.status_code}): {detail}")
        return response.json()
