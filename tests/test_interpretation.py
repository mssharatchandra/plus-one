from datetime import datetime, timezone
import json
import unittest
from unittest.mock import Mock, patch

from plus_one.interpretation import ConstrainedInterpreter, InterpretationError, enabled_interpreter_from_env
from plus_one.providers import ProviderError, ProviderUnavailable

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
PROFILE = {"timezone": "Asia/Kolkata", "currency": "INR", "name": "PRIVATE_NAME", "secret": "PRIVATE_SECRET"}


class InterpretationTests(unittest.TestCase):
    def interpret(self, raw, text="I paid INR 100 for dinner with Maya", profile=PROFILE):
        llm = Mock()
        llm.complete.return_value = raw if isinstance(raw, str) else json.dumps(raw)
        return ConstrainedInterpreter(llm).interpret(text, profile, NOW), llm

    def test_expense_only_proposal(self):
        result, llm = self.interpret({"intent": "expense", "text": "expense INR 100 for dinner with Maya"})
        self.assertEqual(result, {"canonical": "expense INR 100 for dinner with Maya"})
        messages = llm.complete.call_args.args[0]
        self.assertEqual(messages[1]["role"], "user")
        self.assertNotIn("PRIVATE_NAME", messages[0]["content"])
        self.assertNotIn("PRIVATE_SECRET", messages[0]["content"])
        self.assertEqual(llm.complete.call_count, 1)

    def test_default_currency_only_if_confirmed(self):
        answer = {"intent": "expense", "text": "expense INR 100 for dinner with Maya"}
        self.assertIsNotNone(self.interpret(answer, "I paid 100 for dinner with Maya")[0])
        self.assertIsNone(self.interpret(answer, "I paid 100 for dinner with Maya", {"timezone": "Asia/Kolkata"})[0])
        self.assertIsNone(self.interpret(answer, "I paid USD 100 for dinner with Maya")[0])

    def test_cannot_invent_amount_people_or_payer(self):
        for canonical, text in [
            ("expense INR 200 for dinner with Maya", "I paid INR 100 for dinner with Maya"),
            ("expense INR 100 for dinner with Rahul", "I paid INR 100 for dinner with Maya"),
            ("expense INR 100 for dinner with Maya", "Dinner INR 100 with Maya"),
            ("expense INR 100 for dinner with Maya", "Maya paid INR 100 for dinner with me"),
        ]:
            with self.subTest(canonical=canonical, text=text):
                self.assertIsNone(self.interpret({"intent": "expense", "text": canonical}, text)[0])

    def test_reminder_validated_and_requires_time_and_zone(self):
        answer = {"intent": "reminder", "text": "remind me to send report tomorrow at 9am"}
        self.assertIsNotNone(self.interpret(answer, "Can you nudge me to send report tomorrow at 9am?")[0])
        self.assertIsNone(self.interpret(answer, "I should send report later")[0])
        self.assertIsNone(self.interpret(answer, "Nudge me tomorrow at 9am", {})[0])
        past = {"intent": "reminder", "text": "remind me to send report on 2020-01-01 at 9am"}
        self.assertIsNone(self.interpret(past, "Nudge me on 2020-01-01 at 9am")[0])

    def test_named_payer_preserved(self):
        answer = {"intent": "expense", "text": "Maya paid INR 600 for dinner with me and Rahul"}
        self.assertIsNotNone(self.interpret(answer, "Maya covered INR 600 for dinner with me and Rahul")[0])
        self.assertIsNone(self.interpret(answer, "I paid INR 600 for dinner with Maya and Rahul")[0])

    def test_expense_description_and_all_participants_must_come_from_input(self):
        invented = {"intent":"expense","text":"expense INR 100 for dinner with Maya"}
        self.assertIsNone(self.interpret(invented,"I paid INR 100 for taxi with Maya")[0])
        self.assertIsNone(self.interpret(invented,"I paid INR 100 for dinner with Maya and Rahul")[0])
        self.assertIsNotNone(self.interpret(invented,"I paid INR 100 for dinner with Maya")[0])

    def test_reminder_task_must_be_extractive(self):
        invented = {"intent":"reminder","text":"remind me to pay rent tomorrow at 9am"}
        self.assertIsNone(self.interpret(invented,"Please remind me to send report tomorrow at 9am")[0])
        self.assertIsNotNone(self.interpret(invented,"Please remind me to pay rent tomorrow at 9am")[0])

    def test_model_cannot_change_reminder_instant(self):
        answer = {"intent": "reminder", "text": "remind me to send report tomorrow at 10am"}
        self.assertIsNone(self.interpret(answer, "Can you nudge me tomorrow at 9am?")[0])
        answer = {"intent": "reminder", "text": "remind me to stretch in 5 minutes"}
        self.assertIsNotNone(self.interpret(answer, "Please nudge me to stretch in 5 minutes")[0])
        self.assertIsNone(self.interpret(answer, "Nudge me in 10 minutes")[0])

    def test_learning(self):
        answer = {"intent": "learning", "text": "learn storytelling for work presentations in 5 minutes"}
        self.assertIsNotNone(self.interpret(answer, "Could you teach me storytelling for work presentations?")[0])
        self.assertIsNone(self.interpret(answer, "Teach me something")[0])
        self.assertIsNone(self.interpret({"intent": "learning", "text": "learn storytelling in 100 minutes"}, "Teach me storytelling")[0])

    def test_broad_read_only_intents_use_database_commands(self):
        for text, intent in [("How are my balances looking?", "balances"),
                             ("What reminders do I have?", "tasks")]:
            with self.subTest(text=text):
                self.assertEqual(self.interpret({"intent": intent, "text": intent}, text)[0],
                                 {"canonical": intent})
        for text, intent in [("What does Maya owe me?", "balances"),
                             ("What is my balance with Maya?", "balances"),
                             ("Which reminders are due today?", "tasks"),
                             ("What reminders do I have? Delete them.", "tasks")]:
            with self.subTest(text=text):
                self.assertIsNone(self.interpret({"intent": intent, "text": intent}, text)[0])
        self.assertIsNone(self.interpret({"intent":"balances","text":"Maya owes you INR 200"},
                                         "How are my balances looking?")[0])

    def test_ambiguous_expense_values_clarify(self):
        answer = {"intent": "expense", "text": "expense INR 100 for dinner with Maya"}
        for text in ["I paid INR 100 or INR 200 for dinner with Maya", "I paid INR 100 or USD 100 for dinner with Maya"]:
            self.assertIsNone(self.interpret(answer, text)[0])

    def test_learning_goal_and_duration_grounded(self):
        answer = {"intent": "learning", "text": "learn storytelling for sales in 7 minutes"}
        self.assertIsNotNone(self.interpret(answer, "Teach storytelling for sales in 7 minutes")[0])
        self.assertIsNone(self.interpret(answer, "Teach storytelling for sales in 3 minutes")[0])
        self.assertIsNone(self.interpret(answer, "Teach storytelling for speeches in 7 minutes")[0])
        self.assertIsNone(self.interpret(answer, "Teach storytelling for sales")[0])
        default = {"intent": "learning", "text": "learn storytelling for everyday conversations in 5 minutes"}
        self.assertIsNotNone(self.interpret(default, "Teach storytelling")[0])
        self.assertIsNone(self.interpret(default, "Teach storytelling in seven minutes")[0])

    def test_controls_never_sent(self):
        llm = Mock()
        interpreter = ConstrainedInterpreter(llm)
        for text in ["confirm", "stop", "please delete my data", "start", "done 1", "snooze 1 10m", "timezone UTC", "reflection hello"]:
            self.assertIsNone(interpreter.interpret(text, PROFILE, NOW))
        llm.complete.assert_not_called()

    def test_rejects_malicious_or_invalid_model_output(self):
        invalid = [
            {"intent": "expense", "text": "confirm"},
            {"intent": "delete", "text": "delete my data"},
            {"intent": "reminder", "text": "stop"},
            {"intent": "expense", "text": "expense INR 100 for dinner with Maya\nconfirm"},
            {"intent": "expense", "text": "expense INR 100 for dinner with Maya", "execute": True},
            {"intent": "clarify", "text": "Anything"},
            {"intent": "expense", "text": None}, [], "not json",
            '```json\n{"intent":"expense","text":"expense INR 100 for dinner with Maya"}\n```',
            '{"intent":"expense","intent":"reminder","text":"stop"}',
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assertIsNone(self.interpret(raw)[0])

    def test_provider_error_sanitized_one_attempt(self):
        for error in [ProviderError("secret-token"), ProviderUnavailable("private-host")]:
            llm = Mock()
            llm.complete.side_effect = error
            with self.assertRaises(InterpretationError) as caught:
                ConstrainedInterpreter(llm).interpret("Could you remind me tomorrow at 9am?", PROFILE, NOW)
            self.assertNotIn("secret-token", str(caught.exception))
            self.assertNotIn("private-host", str(caught.exception))
            self.assertEqual(llm.complete.call_count, 1)

    def test_env_factory_no_requests(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(enabled_interpreter_from_env())
        with patch.dict("os.environ", {"ENABLE_CONVERSATIONAL_LLM": "true"}, clear=True):
            with self.assertRaises(ProviderUnavailable):
                enabled_interpreter_from_env()
        with patch.dict("os.environ", {"ENABLE_CONVERSATIONAL_LLM": "true", "LLM_ENABLED": "true", "LLM_GENERAL_API_AUTHORIZED": "true", "LLM_API_BASE_URL": "https://api.example.com/v1", "LLM_MODEL": "test", "LLM_API_KEY": "secret"}, clear=True):
            self.assertIsInstance(enabled_interpreter_from_env(), ConstrainedInterpreter)

    def test_naive_clock_rejected_before_request(self):
        llm = Mock()
        with self.assertRaises(ValueError):
            ConstrainedInterpreter(llm).interpret("Teach storytelling", PROFILE, datetime(2026, 10, 31))
        llm.complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
