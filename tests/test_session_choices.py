"""A choice is a question within a turn, not a completed turn or interruption."""
import asyncio
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from session_engine import SessionEngine
from session_store import SessionStore
from store import MessageStore


def setup_session(tmp_path, stack):
    sessions = SessionStore(str(tmp_path / 'session_runs.json'))
    sessions.save_custom_template({'id': 'test', 'name': 'Test', 'roles': ['asker', 'finisher'],
        'phases': [{'name': 'Work', 'participants': ['asker', 'finisher'], 'prompt': 'Test', 'is_output': True}]})
    messages = MessageStore(str(tmp_path / 'messages.jsonl'))
    registry = Mock()
    registry.is_registered.side_effect = lambda name: name in ('claude', 'codex')
    trigger = Mock()
    engine = SessionEngine(sessions, messages, trigger, registry)
    timers = []
    def timer(delay, callback, args):
        result = Mock()
        result.start.side_effect = lambda: timers.append(lambda: callback(*args))
        return result
    stack.enter_context(patch('session_engine.threading.Timer', timer))
    stack.enter_context(patch.object(app, 'store', messages))
    stack.enter_context(patch.object(app, 'room_settings', {'username': 'Ben'}))
    stack.enter_context(patch.object(app, 'ws_clients', set()))
    engine.start_session('test', 'general', {'asker': 'claude', 'finisher': 'codex'}, 'Ben')
    return sessions, messages, engine, trigger, timers


def ask(messages, sender='claude', channel='general'):
    return messages.add(sender, 'Which?', msg_type='decision', channel=channel, sender_kind='agent',
                        metadata={'choices': ['Blue', 'Green'], 'resolved': False})


def answer(question):
    request = Mock()
    async def body(): return {'choice': 'Blue'}
    request.json = body
    return asyncio.run(app.resolve_decision(question['id'], request))


class SessionChoiceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp_path = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.fixture = setup_session(self.tmp_path, self.stack)

    def test_choice_keeps_turn_then_response_advances_once(self):
        sessions, messages, engine, trigger, timers = self.fixture
        question = ask(messages)
        assert sessions.get(1)['choice_message_id'] == question['id']
        assert timers == []
        messages.add('claude', 'Please pick one.', sender_kind='agent')
        assert timers == []
        assert answer(question)['ok']
        assert sessions.get(1)['state'] == 'waiting'
        assert sessions.get(1)['choice_message_id'] is None
        assert sessions.get(1)['current_turn'] == 0
        assert trigger.trigger_sync.call_count == 1  # reply is routed by app's normal @mention path
        messages.add('claude', 'I will use Blue.', sender_kind='agent')
        messages.add('claude', 'Ready for review.', sender_kind='agent')
        for timer in list(timers): timer()
        assert sessions.get(1)['current_turn'] == 1
        assert trigger.trigger_sync.call_count == 2
        assert trigger.trigger_sync.call_args.args == ('codex',)
        messages.add('codex', 'Done.', sender_kind='agent')
        timers[-1]()
        assert sessions.get(1)['state'] == 'complete'

    def test_question_blocks_already_queued_advance(self):
        sessions, messages, engine, trigger, timers = self.fixture
        messages.add('claude', 'I need a choice.', sender_kind='agent')
        ask(messages)
        timers[0]()
        assert sessions.get(1)['current_turn'] == 0
        assert trigger.trigger_sync.call_count == 1

    def test_typed_interruption_can_resume_without_clicking_choice(self):
        sessions, messages, engine, trigger, timers = self.fixture
        ask(messages)
        messages.add('Ben', '@claude Use red instead.', sender_kind='human')
        assert sessions.get(1)['state'] == 'paused'
        assert sessions.get(1)['choice_message_id'] is None
        messages.add('claude', 'Using red.', sender_kind='agent')
        timers[0]()
        assert sessions.get(1)['current_turn'] == 1

    def test_human_with_agents_display_name_pauses_instead_of_advancing(self):
        sessions, messages, engine, trigger, timers = self.fixture
        messages.add('claude', 'Please pause.', sender_kind='human')
        self.assertEqual(sessions.get(1)['state'], 'paused')
        self.assertEqual(timers, [])

    def test_duplicate_answer_is_rejected_without_second_reply(self):
        sessions, messages, engine, trigger, timers = self.fixture
        question = ask(messages)
        answer(question)
        result = answer(question)
        assert result.status_code == 400
        replies = [m for m in messages.get_recent() if m.get('reply_to') == question['id']]
        assert len(replies) == 1
        assert sessions.get(1)['state'] == 'waiting'

    def test_old_choice_cannot_answer_current_question(self):
        sessions, messages, engine, trigger, timers = self.fixture
        old_question = ask(messages)
        current_question = ask(messages)
        answer(old_question)
        assert sessions.get(1)['state'] == 'waiting'
        assert sessions.get(1)['choice_message_id'] == current_question['id']
        messages.add('claude', 'Responding to the old answer.', sender_kind='agent')
        assert timers == []
        assert sessions.get(1)['current_turn'] == 0
        assert trigger.trigger_sync.call_count == 1

    def test_question_in_other_channel_or_from_other_participant_is_ignored(self):
        sessions, messages, engine, trigger, timers = self.fixture
        ask(messages, channel='elsewhere')
        ask(messages, sender='codex')
        assert sessions.get(1).get('choice_message_id') is None
        assert timers == []

    def test_pending_choice_survives_restart(self):
        sessions, messages, engine, trigger, timers = self.fixture
        question = ask(messages)
        restored = SessionStore(str(self.tmp_path / 'session_runs.json'))
        engine._store = restored
        engine.resume_active_sessions()
        assert restored.get(1)['choice_message_id'] == question['id']
        assert trigger.trigger_sync.call_count == 1
        answer(question)
        assert restored.get(1)['state'] == 'waiting'
        assert restored.get(1)['choice_message_id'] is None

    def test_failed_wake_persists_and_retries_on_heartbeat_after_restart(self):
        sessions, messages, engine, trigger, timers = self.fixture
        trigger.trigger_sync.side_effect = OSError("queue write failed")
        messages.add("claude", "My turn is done.", sender_kind="agent")
        with self.assertLogs("session_engine", level="ERROR"):
            timers[-1]()

        restored = SessionStore(str(self.tmp_path / "session_runs.json"))
        pending = restored.get(1)
        self.assertEqual((pending["state"], pending["current_turn"], pending["waiting_on"]),
                         ("waiting", 1, "codex"))
        self.assertTrue(pending["wake_pending"])

        new_trigger = Mock()
        engine = SessionEngine(restored, MessageStore(str(self.tmp_path / "messages.jsonl")),
                               new_trigger, engine._registry)
        engine._registry.is_registered.return_value = False
        engine._registry.is_registered.side_effect = None
        engine.resume_active_sessions()
        new_trigger.trigger_sync.assert_not_called()

        engine._registry.is_registered.return_value = True
        request = Mock()
        request.json.side_effect = ValueError("no activity report")
        with (
            patch.object(app, "session_engine", engine),
            patch.object(app, "_extract_agent_token", return_value="test"),
            patch.object(app, "_resolve_authenticated_agent",
                         return_value={"name": "codex", "state": "active"}),
        ):
            self.assertEqual(asyncio.run(app.heartbeat("old-name", request))["name"], "codex")
            asyncio.run(app.heartbeat("codex", request))
        new_trigger.trigger_sync.assert_called_once()
        self.assertEqual(new_trigger.trigger_sync.call_args.args, ("codex",))
        self.assertEqual(new_trigger.trigger_sync.call_args.kwargs["channel"], "general")

        engine._store = SessionStore(str(self.tmp_path / "session_runs.json"))
        engine.resume_active_sessions()
        self.assertFalse(engine._store.get(1).get("wake_pending"))
        new_trigger.trigger_sync.assert_called_once()

    def test_end_session_cancels_queued_response(self):
        sessions, messages, engine, trigger, timers = self.fixture
        messages.add('claude', 'Done.', sender_kind='agent')
        engine.end_session(1)
        timers[0]()
        assert sessions.get(1)['state'] == 'interrupted'
        assert trigger.trigger_sync.call_count == 1

    def test_fast_choice_answer_does_not_complete_turn_with_pre_question_message(self):
        sessions, messages, engine, trigger, timers = self.fixture
        messages.add('claude', 'I need a choice.', sender_kind='agent')
        question = ask(messages)
        answer(question)
        timers[0]()
        assert sessions.get(1)['current_turn'] == 0
        assert trigger.trigger_sync.call_count == 1
        messages.add('claude', 'Using Blue.', sender_kind='agent')
        timers[-1]()
        assert sessions.get(1)['current_turn'] == 1


if __name__ == '__main__':
    unittest.main()
