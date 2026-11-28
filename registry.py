"""Runtime Registry — single source of truth for all live agent state.

Seeded from config.toml base definitions. All systems read from the registry
at runtime, never from config.toml directly.

Thread-safe: a single threading.Lock guards all mutations.
"""

import colorsys
import json
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path


@dataclass
class Instance:
    """A live agent instance."""
    name: str       # canonical ID: "gemini", "gemini-2"
    base: str       # base family: "gemini"
    slot: int       # 1, 2, 3...
    label: str      # "Gemini", "Gemini 2", or human-set custom
    color: str      # hex color (derived from base + slot)
    identity_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    token: str = field(default_factory=lambda: secrets.token_hex(16))
    epoch: int = 1
    state: str = "pending"   # "pending" | "active"
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.monotonic)


class RuntimeRegistry:
    HEARTBEAT_TIMEOUT = 15

    def __init__(self, data_dir: str = "./data"):
        self._lock = threading.Lock()
        self._persistence_lock = threading.Lock()
        self._bases: dict[str, dict] = {}          # base name → config template
        self._instances: dict[str, Instance] = {}   # canonical name → Instance
        self._renames: dict[str, str] = {}           # old name → new name (for heartbeat redirect)
        self._reclaimable: dict[str, Instance] = {}  # name → deregistered Instance, recoverable on reconnect
        self._on_change_cbs: list = []
        self._data_dir = Path(data_dir)
        self._profiles: dict[str, dict[str, dict]] = {}
        self._legacy_slot_names = False
        self._load_renames()
        self._load_instances()

    # --- Setup ---

    def seed(self, agents_config: dict):
        """Load base templates from config.toml [agents.*] section."""
        with self._lock:
            for name, cfg in agents_config.items():
                self._bases[name] = dict(cfg)
            # Migrate the old automatic slot-1 suffix, including sleeping wrappers.
            for old_name, inst in list(self._reclaimable.items()):
                if self._legacy_slot_names and inst.slot == 1 and old_name == f"{inst.base}-1":
                    if inst.base in self._reclaimable:
                        continue
                    del self._reclaimable[old_name]
                    inst.name = inst.base
                    default_label = self._bases.get(inst.base, {}).get("label", inst.base.capitalize())
                    if inst.label == f"{default_label} 1":
                        inst.label = default_label
                    self._reclaimable[inst.name] = inst
                    self._renames.pop(inst.name, None)
                    self._renames[old_name] = inst.name
                self._remember_profile_locked(inst)
            self._legacy_slot_names = False

    def on_change(self, cb):
        """Register a callback fired after any registry mutation."""
        self._on_change_cbs.append(cb)

    def _notify(self):
        for cb in self._on_change_cbs:
            try:
                cb()
            except Exception:
                pass

    # --- Rename persistence ---

    def _renames_path(self) -> Path:
        return self._data_dir / "renames.json"

    def _load_renames(self):
        p = self._renames_path()
        if p.exists():
            try:
                self._renames = json.loads(p.read_text("utf-8"))
            except Exception:
                self._renames = {}

    def _save_renames(self):
        """Persist renames to disk. Must be called outside the lock."""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with self._persistence_lock:
                tmp = self._renames_path().with_suffix(".tmp")
                with self._lock:
                    data = dict(self._renames)
                tmp.write_text(json.dumps(data), "utf-8")
                tmp.replace(self._renames_path())
        except Exception:
            pass

    # --- Instance persistence (survives server restart) ---

    def _instances_path(self) -> Path:
        return self._data_dir / "registry.json"

    def _save_instances(self):
        """Persist live + reclaimable instances (incl. tokens) to disk.

        Tokens live in the local ./data dir, consistent with the existing local-only
        posture (server binds 127.0.0.1). Must be called outside the lock.
        """
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with self._persistence_lock:
                with self._lock:
                    for inst in self._instances.values():
                        self._remember_profile_locked(inst)
                    data = json.dumps({
                        "instances": {n: _inst_full(i) for n, i in self._instances.items()},
                        "reclaimable": {n: _inst_full(i) for n, i in self._reclaimable.items()},
                        "profiles": self._profiles,
                    })
                tmp = self._instances_path().with_suffix(".tmp")
                tmp.write_text(data, "utf-8")
                tmp.replace(self._instances_path())
        except Exception:
            pass

    def _load_instances(self):
        p = self._instances_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text("utf-8"))
        except Exception:
            return
        # Slot-based profiles were added when automatic slot 1 stopped using "-1".
        self._legacy_slot_names = "profiles" not in data
        saved_profiles = data.get("profiles", {})
        if not isinstance(saved_profiles, dict):
            saved_profiles = {}
        for base, profiles in saved_profiles.items():
            if not isinstance(profiles, dict):
                continue
            self._profiles[base] = {
                slot: profile for slot, profile in profiles.items()
                if isinstance(profile, dict)
                and all(isinstance(profile.get(k), str) for k in ("name", "label", "color"))
            }
        # On startup nobody is proven alive: a persisted "active" instance may be a
        # dead pre-restart wrapper. Load everything as reclaimable — a wrapper that is
        # genuinely still alive reactivates its token on its next call (resolve_token),
        # while dead wrappers stay dormant instead of squatting a slot. Loading them as
        # active would make the empty post-restart presence map immortalize the ghost
        # (the crash-timeout only fires for names with last_seen > 0).
        for section in ("instances", "reclaimable"):
            for n, d in data.get(section, {}).items():
                try:
                    self._reclaimable[n] = _inst_from_dict(d)
                except Exception:
                    pass

    def _evict_reclaimable_collisions_locked(self):
        """Drop reclaimables that collide with any live instance, by name or (base, slot).

        The live instance is authoritative: a reclaimable sharing its canonical name or
        its (base, slot) coordinate is stale and must not survive to be persisted. If it
        did, `_save_instances` would write both records and `_load_instances` could let
        the stale one overwrite the live one (same dict key) on restart — reviving a dead
        token and stranding the live one. Call this after any mutation that creates or
        moves a live instance (register, claim, rename), while holding self._lock.
        Keeps _instances and _reclaimable disjoint so persistence is unambiguous.
        """
        live_names = set(self._instances.keys())
        live_coords = {(i.base, i.slot) for i in self._instances.values()}
        for rn in [rn for rn, ri in self._reclaimable.items()
                   if rn in live_names or (ri.base, ri.slot) in live_coords]:
            del self._reclaimable[rn]

    # --- Registration ---

    def register(self, base: str, label: str | None = None, requested_name: str | None = None) -> dict | None:
        """Register a new instance of `base`. Returns slot info or None if unknown base.

        Primary names never change just because another instance joins or leaves.
        Fresh registrations rotate tokens while retaining saved display preferences.
        """
        with self._lock:
            if base not in self._bases:
                return None

            now = time.monotonic()
            expired = [old_name for old_name, old in self._instances.items()
                       if now - old.last_seen > self.HEARTBEAT_TIMEOUT]
            live = {n: i for n, i in self._instances.items() if n not in expired}
            if requested_name is not None:
                if error := self._chat_id_error_locked(requested_name, base, live):
                    raise ValueError(error)

            taken = {i.slot for i in live.values() if i.base == base}
            base_cfg = self._bases[base]
            profiles = self._profiles.get(base, {})
            for slot in count(1):
                if slot in taken:
                    continue
                if requested_name is not None:
                    name = requested_name
                    profile = None
                    break
                default_name = base if slot == 1 else f"{base}-{slot}"
                default_available = default_name not in live and (
                    default_name == base or default_name not in self._bases
                )
                profile = profiles.get(str(slot))
                if profile:
                    saved_name = profile["name"]
                    if saved_name == default_name:
                        saved_available = default_available
                    else:
                        saved_available = not self._chat_id_error_locked(saved_name, base, live)
                    if saved_available:
                        name = saved_name
                        break
                if default_available:
                    name = default_name
                    profile = None
                    break

            # Expire stale sessions only after selecting both a slot and a name.
            for old_name in expired:
                old = self._instances.pop(old_name)
                self._remember_profile_locked(old)
                self._reclaimable[old_name] = old

            default_label = base_cfg.get("label", base.capitalize())
            if profile:
                lbl = label or profile["label"]
                color = profile["color"]
            else:
                if requested_name is not None:
                    default_label = requested_name
                elif slot > 1:
                    default_label = f"{default_label} {slot}"
                lbl = label or default_label
                color = _derive_color(base_cfg.get("color", "#888"), slot)

            self._renames.pop(name, None)

            # Fresh registrations are immediately authoritative. Identity
            # recovery/reclaim still uses chat_claim, but normal startup should
            # not block on a manual confirmation step.
            state = "active"
            inst = Instance(name=name, base=base, slot=slot, label=lbl, color=color, state=state,
                            last_seen=now)
            self._instances[name] = inst
            # Fresh registration supersedes reclaimable
            # identities sharing those names/(base, slot) coordinates — including custom
            # aliases that don't match `name`. Enforces fresh-wins by coordinate, not name.
            self._evict_reclaimable_collisions_locked()
            result = _inst_dict(inst, include_token=True)
            if expired:
                result["_expired"] = expired

        self._notify()
        self._save_renames()
        self._save_instances()
        return result

    def deregister(self, name: str, reclaimable: bool = False, *,
                   identity_id: str | None = None, last_seen_before: float | None = None) -> dict | None:
        """Remove a session, keeping its display profile available for immediate reuse.

        `reclaimable` decides whether the token survives:
          - True  — sleep / crash-timeout path: keep the instance (and its token)
            in _reclaimable so a wrapper reconnecting after wake transparently
            recovers the same identity.
          - False (default) — a deliberate deregister (wrapper shutdown, explicit
            /api/deregister): invalidate the token for a clean slate. A later
            reconnect must re-register. This keeps a deliberate close from
            silently reviving a dead session — reclaim is only wanted for sleep,
            not for closing the server or an agent.

        Optional identity and last-seen checks are evaluated with the removal
        under the same lock as authenticated heartbeat updates.
        """
        with self._lock:
            inst_removed = self._instances.get(name)
            if (inst_removed is None
                    or (identity_id is not None and inst_removed.identity_id != identity_id)
                    or (last_seen_before is not None and inst_removed.last_seen >= last_seen_before)):
                return None
            self._remember_profile_locked(inst_removed)
            del self._instances[name]
            if reclaimable:
                self._reclaimable[name] = inst_removed  # token recoverable on reconnect (sleep)

            self._evict_reclaimable_collisions_locked()

        self._notify()
        self._save_renames()
        self._save_instances()
        return {"ok": True}

    # --- Identity Claim ---

    def claim(self, sender: str, target_name: str | None = None, *,
              identity_id: str | None = None) -> dict | str:
        """Confirm or rename one exact registered identity without moving its slot."""
        with self._lock:
            inst = self._instances.get(sender)
            reclaim = inst is None
            if reclaim:
                inst = self._reclaimable.get(sender)

            if inst is None or (identity_id is not None and inst.identity_id != identity_id):
                return f"No matching {sender} instance. Use its exact registered chat ID."
            if reclaim and any(
                live.base == inst.base and live.slot == inst.slot for live in self._instances.values()
            ):
                return f"Slot {inst.slot} already occupied in {inst.base} family"
            if target_name is not None and target_name != inst.name:
                if error := self._chat_id_error_locked(target_name, inst.base, self._instances):
                    return error

            if reclaim:
                del self._reclaimable[sender]
                inst.last_seen = time.monotonic()
                self._instances[sender] = inst
            if target_name is not None:
                if target_name != inst.name:
                    self._rename_locked(inst, target_name, target_name)
                inst.state = "active"

            self._evict_reclaimable_collisions_locked()
            result = _inst_dict(inst)

        self._notify()
        self._save_renames()
        self._save_instances()
        return result

    def confirm_pending(self, name: str) -> bool:
        """Auto-confirm a pending instance (10s timeout path)."""
        with self._lock:
            inst = self._instances.get(name)
            if not inst or inst.state != "pending":
                return False
            inst.state = "active"

        self._notify()
        self._save_renames()
        self._save_instances()
        return True

    # --- Rename / Label ---

    def rename(self, old_name: str, new_name: str, label: str | None = None) -> dict | str:
        """Change the chat ID and display label without moving the registry slot."""
        with self._lock:
            inst = self._instances.get(old_name)
            if not inst:
                return f"Not found: {old_name}"

            if new_name == old_name:
                if label:
                    inst.label = label
            else:
                if error := self._chat_id_error_locked(new_name, inst.base, self._instances):
                    return error
                self._rename_locked(inst, new_name, label or new_name)
                self._evict_reclaimable_collisions_locked()
            result = _inst_dict(inst)

        self._notify()
        self._save_renames()
        self._save_instances()
        return result

    def _rename_locked(self, inst: Instance, new_name: str, label: str):
        old_name = inst.name
        del self._instances[old_name]
        inst.name = new_name
        inst.label = label
        self._instances[new_name] = inst
        self._renames.pop(new_name, None)
        self._renames[old_name] = new_name

    def set_label(self, name: str, label: str) -> bool:
        """Set display label only (no identity change)."""
        with self._lock:
            inst = self._instances.get(name)
            if not inst:
                return False
            inst.label = label

        self._notify()
        self._save_renames()
        self._save_instances()
        return True

    # --- Queries ---

    def get_instance(self, name: str) -> dict | None:
        with self._lock:
            inst = self._instances.get(name)
            return _inst_dict(inst) if inst else None

    def get_all(self) -> dict[str, dict]:
        """All registered instances as {name: {name, base, slot, label, color, state}}."""
        with self._lock:
            return {n: _inst_dict(i) for n, i in self._instances.items()}

    def get_agent_config(self) -> dict[str, dict]:
        """Public roster keyed by chat ID, including the stable identity used in messages."""
        with self._lock:
            return {
                n: {"color": i.color, "label": i.label, "base": i.base, "state": i.state,
                    "identity_id": i.identity_id}
                for n, i in self._instances.items()
            }

    def get_all_names(self) -> list[str]:
        with self._lock:
            return list(self._instances.keys())

    def get_active_names(self) -> list[str]:
        with self._lock:
            return [n for n, i in self._instances.items() if i.state == "active"]

    def get_instances_for(self, base: str) -> list[dict]:
        with self._lock:
            return [_inst_dict(i) for i in self._instances.values() if i.base == base]

    def get_bases(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._bases)

    def get_base_config(self, base: str) -> dict | None:
        with self._lock:
            return dict(self._bases[base]) if base in self._bases else None

    def is_agent_family(self, name: str) -> bool:
        """Protect registered IDs and reserved agent names from anonymous use."""
        with self._lock:
            return (
                name in self._instances
                or name in self._reclaimable
                or name in self._renames
                or any(profile["name"] == name for profiles in self._profiles.values()
                       for profile in profiles.values())
                or any(name == base or name.startswith(f"{base}-") for base in self._bases)
            )

    def family_instance_count(self, name: str) -> int:
        """Count instances for an exact registered chat ID or configured family."""
        with self._lock:
            inst = self._instances.get(name)
            if inst is None:
                inst = self._reclaimable.get(name)
            base = inst.base if inst is not None else name
            return sum(1 for inst in self._instances.values() if inst.base == base)

    def has_claimed_instances(self, base: str) -> bool:
        """Check if any instance in this family has been claimed (state=active)."""
        with self._lock:
            return any(
                i.state == "active" and i.base == base
                for i in self._instances.values()
            )

    def resolve_to_instances(self, name: str) -> list[str]:
        """Resolve a name to actual registered instance names.

        If `name` is a registered instance, returns [name].
        If `name` is a base family name with no exact match, returns all
        active instances in that family (e.g. 'claude' → ['claude-prime']).
        Otherwise returns [name] unchanged (for non-agent names like 'ben').
        """
        with self._lock:
            if name in self._instances:
                return [name]
            # Check if it's a base name with registered family members
            if name in self._bases:
                members = [i.name for i in self._instances.values()
                           if i.base == name and i.state == "active"]
                if members:
                    return members
            return [name]

    def resolve_name(self, name: str) -> str:
        """Follow rename chain to find current canonical name."""
        with self._lock:
            # Follow renames (e.g. claude-2 → claude-music)
            seen = set()
            current = name
            while current in self._renames and current not in seen:
                if current in self._instances:
                    break
                seen.add(current)
                current = self._renames[current]
            return current

    def is_registered(self, name: str) -> bool:
        with self._lock:
            return name in self._instances

    def is_active_identity(self, identity_id: str) -> bool:
        with self._lock:
            return any(i.identity_id == identity_id and i.state == "active"
                       for i in self._instances.values())

    def is_pending(self, name: str) -> bool:
        with self._lock:
            i = self._instances.get(name)
            return i is not None and i.state == "pending"

    def resolve_token(self, token: str) -> dict | None:
        """Map an instance_token to the current canonical instance dict, or None.

        If the token belongs to a deregistered-but-reclaimable instance (e.g. the agent
        was crash-timed-out while the machine slept), reactivate it transparently rather
        than rejecting it — provided its name has not since been freshly re-registered.
        """
        result = None
        reactivated = False
        changed = False
        with self._lock:
            for inst in self._instances.values():
                if inst.token == token:
                    inst.last_seen = time.monotonic()
                    return _inst_dict(inst)
            for name, inst in list(self._reclaimable.items()):
                if inst.token != token or name in self._instances:
                    continue
                # Fresh-wins guard: if a live instance already holds this (base, slot),
                # the token is stale (its identity was superseded by a fresh launch or a
                # rename-back). Drop it rather than reviving a colliding second instance.
                if any(li.base == inst.base and li.slot == inst.slot
                       for li in self._instances.values()):
                    del self._reclaimable[name]
                    changed = True
                    continue
                inst.state = "active"
                inst.last_seen = time.monotonic()
                self._instances[name] = inst
                del self._reclaimable[name]
                result = _inst_dict(inst)
                reactivated = True
                changed = True
                break
        if reactivated:
            self._notify()
        if changed:
            self._save_instances()
        return result

    def get_pending(self) -> list[dict]:
        """All pending instances (for timeout checks)."""
        with self._lock:
            return [_inst_dict(i) for i in self._instances.values()
                    if i.state == "pending"]

    # --- Internal ---

    def _remember_profile_locked(self, inst: Instance):
        """Display preferences outlive sessions; tokens are never part of profiles."""
        self._profiles.setdefault(inst.base, {})[str(inst.slot)] = {
            "name": inst.name, "label": inst.label, "color": inst.color,
        }

    def _chat_id_error_locked(self, name: str, base: str, live: dict[str, Instance]) -> str | None:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", name):
            return "Name must be 1–40 lowercase letters, digits or internal hyphens"
        if name in ("system", "user"):
            return "That name is reserved"
        if name in live:
            return f"Name already in use: {name}"
        for other in self._bases:
            if other != base and (name == other or re.fullmatch(rf"{re.escape(other)}-[0-9]+", name)):
                return f"Name '{name}' conflicts with the {other} agent family"
        return None

    def clean_renames_for(self, name: str):
        """Remove all rename chain entries pointing to or from `name`."""
        with self._lock:
            # Remove entries where name is a key (old name → ...)
            self._renames.pop(name, None)
            # Remove entries where name is a value (... → name)
            stale = [k for k, v in self._renames.items() if v == name]
            for k in stale:
                del self._renames[k]
        self._save_renames()

# --- Module-level helpers ---

def _inst_dict(inst: Instance, include_token: bool = False) -> dict:
    d = {
        "identity_id": inst.identity_id,
        "name": inst.name, "base": inst.base, "slot": inst.slot,
        "label": inst.label, "color": inst.color, "state": inst.state,
        "epoch": inst.epoch,
        "registered_at": inst.registered_at,
    }
    if include_token:
        d["token"] = inst.token
    return d


def _inst_full(inst: Instance) -> dict:
    """Full serialization incl. token + identity, for persistence/reclaim."""
    return {
        "name": inst.name, "base": inst.base, "slot": inst.slot, "label": inst.label,
        "color": inst.color, "identity_id": inst.identity_id, "token": inst.token,
        "epoch": inst.epoch, "state": inst.state, "registered_at": inst.registered_at,
    }


def _inst_from_dict(d: dict) -> Instance:
    return Instance(
        name=d["name"], base=d["base"], slot=int(d["slot"]), label=d["label"],
        color=d["color"], identity_id=d.get("identity_id", uuid.uuid4().hex),
        token=d["token"], epoch=int(d.get("epoch", 1)), state=d.get("state", "active"),
        registered_at=float(d.get("registered_at", time.time())),
    )


def _derive_color(base_hex: str, slot: int) -> str:
    """Derive variant color: slot 1 = base, slot N = hue/lightness shifted.

    Pattern: slot 2 = hue +25 deg, L +5%; slot 3 = hue -25 deg, L -5%; etc.
    """
    if slot == 1:
        return base_hex
    hx = base_hex.lstrip("#")
    if len(hx) != 6:
        return base_hex
    r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)

    # Alternating hue shifts with increasing magnitude
    magnitude = ((slot - 1 + 1) // 2) * 25
    direction = 1 if slot % 2 == 0 else -1
    h = (h + direction * magnitude / 360) % 1.0
    l = max(0.15, min(0.85, l + direction * 0.05))

    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return f"#{int(r2 * 255):02x}{int(g2 * 255):02x}{int(b2 * 255):02x}"
