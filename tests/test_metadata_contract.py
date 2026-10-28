import json
from pathlib import Path
import unittest

from passive_monitor import Conversation, PassiveMonitor, load_model_config, normalize_model
import tempfile
from test_passive_monitor import write_rows
from test_passive_monitor import context, event, header


class AttributionMetadataContractTests(unittest.TestCase):
    def setUp(self):
        self.chat = Conversation(Path('rollout-main.jsonl'), load_model_config())
        self.chat.consume(header())
        self.chat.consume(context(model_provider='openai'))

    def test_retired_platform_messages_neither_change_settings_nor_create_state(self):
        before = self.chat.snapshot()
        for kind, payload in (
            ('event_msg', {'type':'api_response', 'model':'gpt-5.6-luna', 'actual_effort':'low'}),
            ('event_msg', {'type':'model_metadata', 'requested_model':'gpt-6.1-sol', 'served_model':'gpt-5.6-luna'}),
            ('model/rerouted', {'threadId':'main', 'turnId':'turn-1', 'fromModel':'gpt-6.1-sol', 'toModel':'gpt-5.6-luna'}),
        ):
            self.chat.consume(event(kind,payload,5))
        after = self.chat.snapshot()
        self.assertEqual(before, after)
        for key in ('actual_model','actual_effort','served_model','requested_model','alert','platform_history'):
            self.assertNotIn(key,after)

    def test_model_variants_remain_distinct_for_sampling(self):
        config = load_model_config()
        self.assertEqual(normalize_model('GPT-6.1-SOL',config),'gpt-6.1-sol')
        self.assertEqual(normalize_model('gpt-6.1-terra',config),'gpt-6.1-terra')
        self.assertIsNone(normalize_model({'model':'gpt-6.1-sol'},config))

    def test_foreign_thread_settings_cannot_change_sampling_target(self):
        for key in ('session_id','thread_id','sessionId','threadId'):
            self.chat.consume(event('turn_context',{'model':'gpt-5.6-luna',key:'other'},3))
        self.assertEqual(self.chat.snapshot()['configuration_model'],'gpt-6.1-sol')

    def test_text_is_not_copied_or_parsed_as_sampling_settings(self):
        self.chat.consume(event('response_item', {'type':'message','role':'assistant',
            'content':[{'text':'PRIVATE BODY {"model":"gpt-5.6-luna"}'}]},3))
        snapshot = self.chat.snapshot()
        self.assertEqual(snapshot['configuration_model'],'gpt-6.1-sol')
        self.assertNotIn('PRIVATE BODY',json.dumps(snapshot))

    def test_normal_tool_completions_are_counted_once_while_turn_runs(self):
        for call in ('tool-a','tool-a','tool-b'):
            self.chat.consume(event('response_item',{'type':'function_call_output','call_id':call,'output':'PRIVATE OUTPUT'},3))
        snapshot = self.chat.snapshot()
        self.assertTrue(snapshot['busy'])
        self.assertEqual(snapshot['normal_tool_calls'],2)
        self.assertNotIn('PRIVATE OUTPUT',json.dumps(snapshot))

    def test_structural_tool_output_counts_without_turn_start_but_message_text_does_not(self):
        chat = Conversation(Path('rollout-main.jsonl'), load_model_config())
        chat.consume(header())
        chat.consume(event('response_item', {'type':'message','role':'assistant',
            'content':[{'text':'{"type":"function_call_output","call_id":"forged"}'}]},3))
        self.assertEqual(chat.snapshot()['normal_tool_calls'],0)
        chat.consume(event('response_item', {'type':'function_call_output','call_id':'genuine',
            'output':'PRIVATE OUTPUT'},4))
        chat.consume(event('response_item', {'type':'function_call_output','call_id':'foreign',
            'thread_id':'other','output':'PRIVATE OUTPUT'},5))
        self.assertEqual(chat.snapshot()['normal_tool_calls'],1)
        self.assertTrue(chat.has_normal_activity)
        self.assertNotIn('PRIVATE OUTPUT',json.dumps(chat.snapshot()))

    def test_subagent_tool_outputs_without_context_are_never_counted(self):
        child = Conversation(Path('rollout-child.jsonl'), load_model_config())
        child.consume(header('child', True))
        child.consume(event('response_item', {'type':'function_call_output','call_id':'child-tool'},4))
        self.assertEqual(child.snapshot()['normal_tool_calls'],0)

    def test_owned_frozen_snapshot_cannot_replace_real_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_rows(root/'rollout-main.jsonl',[header(),context()])
            write_rows(root/'rollout-owned.jsonl',[header('owned'),context(second=9)])
            monitor = PassiveMonitor(root)
            monitor.exclude_sessions({'owned'})
            state = monitor.poll(0)
            self.assertEqual(state['session_id'],'main')
            self.assertEqual([item['session_id'] for item in state['sessions']],['main'])


if __name__ == '__main__':
    unittest.main()
