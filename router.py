"""Message routing based on @mentions with per-channel loop guard."""

import re


class Router:
    def __init__(self, agent_names: list[str], default_mention: str = "both",
                 max_hops: int = 4, online_checker=None):
        self.default_mention = default_mention
        self.max_hops = max_hops
        self._online_checker = online_checker  # callable() -> set of online agent names
        # Per-channel state: { channel: { hop_count, paused, guard_emitted } }
        self._channels: dict[str, dict] = {}
        self.update_agents(agent_names)

    def _get_ch(self, channel: str) -> dict:
        if channel not in self._channels:
            self._channels[channel] = {
                "hop_count": 0,
                "paused": False,
                "guard_emitted": False,
            }
        return self._channels[channel]

    def parse_mentions(self, text: str) -> list[str]:
        return self._parse_mentions(text, self._mention_roster)

    def _parse_mentions(self, text, roster):
        agent_names, mention_pattern = roster
        mentions = set()
        # Mentions shown as code are examples or references, not recipients.
        text = re.sub(r"(`+)([\s\S]*?)\1", "", text)
        for match in mention_pattern.finditer(text):
            name = match.group(1).lower()
            if name in ("both", "all"):
                # Only tag online agents when using @all
                if self._online_checker:
                    online = self._online_checker()
                    mentions.update(agent_names.intersection(online))
                else:
                    mentions.update(agent_names)
            else:
                mentions.add(name)
        return list(mentions)

    def get_targets(self, text: str, channel: str = "general", *,
                    sender_is_agent: bool) -> list[str]:
        """Apply the loop guard before callers resolve mentions to identities."""
        ch = self._get_ch(channel)
        roster = self._mention_roster
        mentions = self._parse_mentions(text, roster)

        if not sender_is_agent:
            # Human message resets hop counter and unpauses
            ch["hop_count"] = 0
            ch["paused"] = False
            ch["guard_emitted"] = False
            if re.match(r"^\s*/(?:summary|summarise|ping)(?:\s|$)", text, re.IGNORECASE):
                return mentions
            if not mentions:
                if self.default_mention in ("both", "all"):
                    return list(roster[0])
                elif self.default_mention == "none":
                    return []
                return [self.default_mention]
            return mentions
        else:
            # Agent message: blocked while loop guard is active
            if ch["paused"]:
                return []
            # Only route if explicit @mention
            if not mentions:
                return []
            ch["hop_count"] += 1
            if ch["hop_count"] > self.max_hops:
                ch["paused"] = True
                return []
            return mentions

    def continue_routing(self, channel: str = "general"):
        """Resume after loop guard pause."""
        ch = self._get_ch(channel)
        ch["hop_count"] = 0
        ch["paused"] = False
        ch["guard_emitted"] = False

    def is_paused(self, channel: str = "general") -> bool:
        return self._get_ch(channel)["paused"]

    def is_guard_emitted(self, channel: str = "general") -> bool:
        return self._get_ch(channel)["guard_emitted"]

    def set_guard_emitted(self, channel: str = "general"):
        self._get_ch(channel)["guard_emitted"] = True

    def update_agents(self, names: list[str]):
        """Publish the matching pattern and its agent names together."""
        agent_names = frozenset(n.lower() for n in names)
        escaped = [re.escape(n) for n in sorted(agent_names, key=len, reverse=True)]
        alternatives = "|".join(escaped + ["both", "all"])
        pattern = re.compile(rf"@({alternatives})(?![\w-])", re.IGNORECASE)
        self._mention_roster = (agent_names, pattern)
