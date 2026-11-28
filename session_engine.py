"""Session engine — orchestrates structured multi-agent sessions."""

import logging
import threading
import time

log = logging.getLogger(__name__)

# Dissent mandate injected for review/critique roles
_DISSENT_LINE = "Provide your own independent analysis. Do not repeat or defer to other participants."

# Roles that get the dissent mandate
_DISSENT_ROLES = {"reviewer", "red_team", "critic", "challenger", "against"}


class SessionEngine:
    """Orchestrates session turn flow on top of existing chat infrastructure.

    Listens to message store callbacks, advances session state, and triggers
    agents via the AgentTrigger system.
    """

    def __init__(self, session_store, message_store, agent_trigger, registry=None):
        self._store = session_store
        self._messages = message_store
        self._trigger = agent_trigger
        self._registry = registry
        self._lock = threading.RLock()

        # Hook into message stream
        self._messages.on_message(self._on_message)

    # --- Public API ---

    def start_session(self, template_id: str, channel: str, cast: dict,
                      started_by: str, goal: str = "") -> dict | None:
        """Start a new session. Returns the session dict or None on failure."""
        roster = self._registry.get_all() if self._registry else {}
        identities = {role: roster[name]["identity_id"] for role, name in cast.items()
                      if name in roster and roster[name].get("identity_id")}
        session = self._store.create(
            template_id=template_id,
            channel=channel,
            cast=cast,
            started_by=started_by,
            goal=goal,
            cast_identities=identities,
        )
        if not session:
            return None

        log.info("Session %d started: %s in #%s", session["id"],
                 session["template_name"], channel)

        with self._lock:
            self._trigger_current(session)
        return session

    def emit_current_phase_banner(self, session: dict):
        """Post the banner for the session's current phase."""
        tmpl = self._store.get_template(session.get("template_id", ""))
        if not tmpl:
            return

        phases = tmpl.get("phases", [])
        phase_idx = session.get("current_phase", 0)
        if phase_idx >= len(phases):
            return

        phase = phases[phase_idx]
        self._messages.add(
            sender="system",
            text=f"Phase: {phase['name']}",
            msg_type="session_phase",
            channel=session.get("channel", "general"),
            metadata={
                "session_id": session["id"],
                "phase": phase_idx,
                "phase_name": phase["name"],
            },
        )

    def end_session(self, session_id: int, reason: str = "ended by user") -> dict | None:
        """End a session early."""
        with self._lock:
            session = self._store.interrupt(session_id, reason)
        if session:
            log.info("Session %d interrupted: %s", session_id, reason)
        return session

    def get_active(self, channel: str) -> dict | None:
        """Get the active session for a channel, enriched with phase info."""
        session = self._store.get_active(channel)
        if not session:
            return None
        return self._enrich(session)

    def get_allowed_wake_target(self, channel: str) -> tuple[str, str | None] | None:
        """Return the current agent's name and identity, or None for unrestricted routing."""
        session = self._store.get_active(channel)
        if not session or session.get("state") not in ("active", "waiting"):
            return None
        role = self._get_expected_role(session)
        if "cast_identities" not in session:
            name = session["cast"].get(role)
            return (name, None) if name else None
        identity_id = session["cast_identities"].get(role)
        instance = self._active_instance(identity_id)
        return (instance["name"] if instance else "", identity_id)

    def list_active(self) -> list[dict]:
        """List all active/waiting/paused sessions, enriched for the frontend."""
        active = []
        for session in self._store.list_all():
            if session.get("state") in ("active", "waiting", "paused"):
                active.append(self._enrich(session))
        return active

    def resume_active_sessions(self):
        """Resume undispatched turns after restart and leave delivered turns alone."""
        with self._lock:
            for session in self._store.list_all():
                if (session["state"] == "active"
                        or (session["state"] == "waiting" and session.get("wake_pending"))):
                    self._trigger_current(session)

    def retry_pending_wakes(self, agent: str, identity_id: str | None = None):
        """Retry unsent wakes when an authenticated wrapper reports it is ready."""
        with self._lock:
            for session in self._store.list_all():
                if (session["state"] != "waiting" or not session.get("wake_pending")
                        or session.get("choice_message_id") is not None):
                    continue
                if "cast_identities" in session:
                    role = self._get_expected_role(session)
                    matches = identity_id and session["cast_identities"].get(role) == identity_id
                else:
                    matches = session.get("waiting_on") == agent
                if matches:
                    self._trigger_current(session)

    def _is_agent(self, name: str) -> bool:
        """Check if name belongs to a registered agent (not a human)."""
        if self._registry:
            return self._registry.is_registered(name)
        return False

    def _active_instance(self, identity_id: str | None) -> dict | None:
        if identity_id and self._registry:
            return self._registry.get_active_instance_by_identity(identity_id)
        return None

    # --- Message callback ---

    def _on_message(self, msg: dict):
        """Called on every new chat message. Checks if it advances a session."""
        channel = msg.get("channel", "general")
        sender = msg.get("sender", "")

        # Ignore system-generated messages (banners, phase markers, etc.)
        if (msg.get("sender_kind") not in ("human", "agent")
                or msg.get("type", "chat") not in ("chat", "decision")):
            return

        with self._lock:
            self._handle_participant_message(msg, channel, sender)

    def _handle_participant_message(self, msg: dict, channel: str, sender: str):
        session = self._store.get_active(channel)
        if not session:
            return

        role = self._get_expected_role(session)
        expected_agent = session["cast"].get(role)
        if not expected_agent:
            return

        sender_is_agent = msg["sender_kind"] == "agent"
        identity_id = None
        if "cast_identities" in session:
            identity_id = session["cast_identities"].get(role)
            expected_is_agent = bool(identity_id)
            matches = msg.get("sender_identity") == identity_id if identity_id else sender == expected_agent
        else:
            # Older sessions contain only the chat IDs selected at creation.
            expected_is_agent = self._is_agent(expected_agent)
            matches = sender == expected_agent
        is_expected_sender = matches and sender_is_agent == expected_is_agent

        # A choice card asks for input; it is not the participant's completed turn.
        if msg.get("type") == "decision":
            if is_expected_sender:
                self._store.set_choice(session["id"], msg["id"])
            return

        choice_id = session.get("choice_message_id")
        if choice_id is not None and not sender_is_agent and msg.get("reply_to") is not None:
            choice = self._messages.get_by_id(msg["reply_to"])
            if (choice and choice.get("type") == "decision"
                    and (choice.get("metadata") or {}).get("resolved")):
                if msg["reply_to"] == choice_id:
                    self._store.set_choice(session["id"], None)
                    if identity_id and not self._active_instance(identity_id):
                        self._store.set_waiting(session["id"], expected_agent, wake_pending=True)
                # Normal @mention routing wakes the participant with the answer.
                # An older card must not release the current question.
                return

        # Human spoke but it's not their turn — pause if an agent is expected
        if not sender_is_agent and expected_is_agent:
            if choice_id is not None:
                self._store.set_choice(session["id"], None)
            self._store.pause(session["id"])
            log.info("Session %d paused: human interruption by %s", session["id"], sender)
            return

        if is_expected_sender:
            if choice_id is not None:
                return
            # Auto-resume if paused
            if session["state"] == "paused":
                self._store.resume(session["id"])
            # Defer advance slightly so the triggering message broadcasts
            # before phase/completion banners are added
            threading.Timer(0.3, self._advance, args=(session, msg["id"])).start()
            return

        # Wrong agent spoke - ignore
        return

    # --- Engine core ---

    def _advance(self, session: dict, message_id: int):
        """Advance once, only if this response still belongs to the current turn."""
        with self._lock:
            current = self._store.get(session["id"])
            if (not current or current["state"] not in ("active", "waiting")
                    or current.get("choice_message_id") is not None
                    or message_id <= current.get("last_choice_message_id", -1)
                    or (current["current_phase"], current["current_turn"])
                    != (session["current_phase"], session["current_turn"])):
                return
            self._advance_current(current, message_id)

    def _advance_current(self, session: dict, message_id: int):
        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            self._store.interrupt(session["id"], "template not found")
            return

        phases = tmpl.get("phases", [])
        phase_idx = session["current_phase"]
        turn_idx = session["current_turn"]

        if phase_idx >= len(phases):
            self._store.complete(session["id"], message_id)
            return

        phase = phases[phase_idx]
        participants = phase.get("participants", [])

        next_turn = turn_idx + 1
        if next_turn < len(participants):
            # More turns in this phase
            session = self._store.advance_turn(session["id"], message_id)
            if session:
                self._trigger_current(session)
        else:
            # Phase complete
            next_phase = phase_idx + 1
            if next_phase < len(phases):
                # More phases
                session = self._store.advance_phase(session["id"], message_id)
                if session:
                    next_phase_obj = phases[next_phase]
                    self._messages.add(
                        sender="system",
                        text=f"Phase: {next_phase_obj['name']}",
                        msg_type="session_phase",
                        channel=session.get("channel", "general"),
                        metadata={"session_id": session["id"],
                                  "phase": next_phase, "phase_name": next_phase_obj["name"]},
                    )
                    self._trigger_current(session)
            else:
                # Session complete - check if this was the output phase
                is_output = phase.get("is_output", False)
                self._store.complete(session["id"],
                                     message_id if is_output else None)
                log.info("Session %d complete", session["id"])

    def _trigger_current(self, session: dict):
        """Trigger the agent whose turn it is."""
        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            return

        phases = tmpl.get("phases", [])
        phase_idx = session["current_phase"]
        turn_idx = session["current_turn"]

        if phase_idx >= len(phases):
            return

        phase = phases[phase_idx]
        participants = phase.get("participants", [])

        if turn_idx >= len(participants):
            return

        role = participants[turn_idx]
        cast = session.get("cast", {})
        agent = cast.get(role)

        if not agent:
            log.warning("Session %d: no agent cast for role '%s'", session["id"], role)
            self._store.interrupt(session["id"], f"no agent for role '{role}'")
            return

        if session.get("choice_message_id") is not None:
            return
        identities = session.get("cast_identities")
        identity_id = identities.get(role) if identities is not None else None
        if identity_id:
            instance = self._active_instance(identity_id)
            if instance is None:
                if not session.get("wake_pending"):
                    self._store.set_waiting(session["id"], agent, wake_pending=True)
                return
            agent = instance["name"]
        elif identities is not None or not self._is_agent(agent):
            # Legacy pending wakes and human turns both remain waiting.
            if not session.get("wake_pending"):
                self._store.set_waiting(session["id"], agent)
            return

        if not session.get("wake_pending") or session.get("waiting_on") != agent:
            if not self._store.set_waiting(session["id"], agent, wake_pending=True):
                return

        prompt = self._assemble_prompt(session, tmpl, phase, role)
        channel = session.get("channel", "general")
        log.info("Session %d: triggering %s (%s) for phase '%s'",
                 session["id"], agent, role, phase["name"])
        try:
            self._trigger.trigger_sync(agent, channel=channel, prompt=prompt, identity_id=identity_id)
        except OSError:
            log.exception("Session %d: wake for %s is pending after a queue write failure",
                          session["id"], agent)
            return

        try:
            self._store.mark_wake_dispatched(session["id"])
        except OSError:
            log.exception("Session %d: could not persist wake completion", session["id"])

    def _assemble_prompt(self, session: dict, tmpl: dict, phase: dict,
                         role: str) -> str:
        """Build the session-aware prompt for an agent."""
        phases = tmpl.get("phases", [])
        phase_idx = session["current_phase"]
        total_phases = len(phases)

        channel = session.get("channel", "general")
        lines = [
            f"SESSION: {tmpl.get('name', '?')}",
        ]
        if session.get("goal"):
            lines.append(f"GOAL: {session['goal']}")
        lines.append(f"PHASE: {phase['name']} ({phase_idx + 1}/{total_phases})")
        lines.append(f"YOUR ROLE: {role}")
        lines.append(f"INSTRUCTION: {phase.get('prompt', '')}")

        # Dissent mandate for review/critique roles
        if role.lower() in _DISSENT_ROLES:
            lines.append(f"\n{_DISSENT_LINE}")

        lines.append("")
        lines.append(f"IMPORTANT: You MUST respond using the 'chat_send' tool in the #{channel} channel. "
                      "The session flow is blocked until your message appears in the chat. "
                      "Do NOT respond only in your terminal.")
        lines.append("Read recent messages in the channel for context (use 'chat_read' or 'mcp read'), "
                      "then post your response. Stay focused on the session goal.")

        # Use double newlines to ensure separation in TUIs that might collapse single newlines
        return "\n\n".join(lines)

    def _get_expected_role(self, session: dict) -> str | None:
        """Get the cast role whose turn it is."""
        tmpl = self._store.get_template(session["template_id"])
        if not tmpl:
            return None

        phases = tmpl.get("phases", [])
        phase_idx = session["current_phase"]
        turn_idx = session["current_turn"]

        if phase_idx >= len(phases):
            return None

        phase = phases[phase_idx]
        participants = phase.get("participants", [])

        if turn_idx >= len(participants):
            return None

        return participants[turn_idx]

    def _enrich(self, session: dict) -> dict:
        """Add computed fields to a session dict for the frontend."""
        tmpl = self._store.get_template(session["template_id"])
        if tmpl:
            phases = tmpl.get("phases", [])
            session["total_phases"] = len(phases)
            phase_idx = session["current_phase"]
            if phase_idx < len(phases):
                phase = phases[phase_idx]
                session["phase_name"] = phase["name"]
                participants = phase.get("participants", [])
                turn_idx = session["current_turn"]
                if turn_idx < len(participants):
                    role = participants[turn_idx]
                    session["current_role"] = role
                    identity_id = session.get("cast_identities", {}).get(role)
                    instance = self._active_instance(identity_id)
                    session["current_agent"] = instance["name"] if instance else session["cast"].get(role)
        return session
