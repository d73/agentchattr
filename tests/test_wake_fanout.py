import unittest
from unittest.mock import AsyncMock, Mock, call, patch

import app


class WakeFanoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_failure_notifies_sender_and_still_queues_next_target(self):
        router = Mock()
        router.get_targets.return_value = ["first", "second"]
        router.is_paused.return_value = False
        agents = Mock()
        agents.trigger = AsyncMock(side_effect=[OSError("queue unavailable"), None])
        with (
            patch.object(app, "registry", None),
            patch.object(app, "router", router),
            patch.object(app, "agents", agents),
            patch.object(app, "session_engine", None),
            patch.object(app, "broadcast", AsyncMock()),
            patch.object(app, "_unarchive_for_activity", return_value=False),
            patch.object(app, "store") as store,
            patch("mcp_bridge.is_online", return_value=True),
            self.assertLogs("app", level="ERROR"),
        ):
            await app._handle_new_message({
                "id": 7, "sender": "human", "sender_kind": "human",
                "text": "@first @second", "channel": "ops",
            })
        self.assertEqual(
            agents.trigger.await_args_list,
            [call(target, message="human: @first @second", channel="ops", prompt="")
             for target in ("first", "second")],
        )
        store.add.assert_called_once_with(
            "system", "Could not queue a wake for @first.", msg_type="system",
            channel="ops", reply_to=7,
        )
