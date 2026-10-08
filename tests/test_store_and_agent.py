import asyncio
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app import followups
from app.config import get_settings
from app.guardrails import Action
from app.rag import KnowledgeBase
from app.security.pii import patient_ref
from clinic_store import LocalStore, StoreError
from tests.helpers import make_agent

ALICE, BOB = "+919000000001", "+919000000002"


def next_open_slot(store, doctor_id="dr-rao"):
    day = store.now().date()
    for i in range(1, 15):
        slots = store.available_slots((day + timedelta(days=i)).isoformat(), doctor_id)
        if slots:
            return slots[0]["slot_id"]
    raise AssertionError("no slots")


class Retrieval(unittest.TestCase):
    kb = KnowledgeBase(get_settings().knowledge_dir, get_settings().clinic)

    def top(self, q):
        return self.kb.search(q, k=1)[0][0].title

    def test_queries(self):
        self.assertEqual(self.top("what are your opening hours"), "Opening hours")
        self.assertEqual(self.top("is there parking"), "Location and parking")
        self.assertEqual(self.top("can I drink water before my fasting blood test"), "Fasting blood tests (sugar, lipid profile)")
        self.assertEqual(self.top("can I wear my ring for the x-ray"), "X-ray")
        self.assertIn("Iyer", self.top("when is Dr Iyer available"))


class StoreRules(unittest.TestCase):
    def setUp(self):
        self.store = LocalStore(get_settings().clinic, Path(tempfile.mkdtemp()) / "s.json")

    def test_book_and_double_book(self):
        slot = next_open_slot(self.store)
        appt = self.store.book("p_a", ALICE, slot, "Alice", "general_consultation")
        self.assertEqual(appt["status"], "booked")
        with self.assertRaises(StoreError):
            self.store.book("p_b", BOB, slot, "Bob", "general_consultation")

    def test_cannot_touch_someone_elses_appointment(self):
        appt = self.store.book("p_a", ALICE, next_open_slot(self.store), "Alice", "other")
        with self.assertRaises(StoreError) as ctx:
            self.store.cancel("p_b", appt["appointment_id"])
        self.assertIn("No appointment", str(ctx.exception))  # same message as "doesn't exist"
        self.assertEqual(self.store.list_appointments("p_b"), [])

    def test_invalid_slot(self):
        with self.assertRaises(StoreError):
            self.store.book("p_a", ALICE, "dr-rao@2026-01-04T03:00", "Alice", "other")

    def test_reschedule_restores_on_failure(self):
        appt = self.store.book("p_a", ALICE, next_open_slot(self.store), "Alice", "other")
        with self.assertRaises(StoreError):
            self.store.reschedule("p_a", appt["appointment_id"], "bogus")
        self.assertEqual(len(self.store.list_appointments("p_a")), 1)

    def test_followups_respect_opt_out_and_handoffs(self):
        slot = next_open_slot(self.store)
        appt = self.store.book("p_a", ALICE, slot, "Alice", "other")
        start = self.store.get_appointment(appt["appointment_id"])["start"]
        from datetime import datetime
        day_before = datetime.fromisoformat(start) - timedelta(days=1)
        self.assertEqual(len(followups.due(self.store, "reminder", now=day_before)), 1)
        self.store.set_opt_out("p_a", True)
        self.assertEqual(followups.due(self.store, "reminder", now=day_before), [])
        self.store.set_opt_out("p_a", False)

        self.store.log_visit(appt["appointment_id"], "completed")
        self.store.mark_followup_sent(appt["appointment_id"], "checkin")
        three_days = datetime.fromisoformat(start) + timedelta(hours=72)
        self.assertEqual(len(followups.due(self.store, "review", now=three_days)), 1)
        self.store.create_handoff("p_a", "clinical", "normal", "not feeling better")
        self.assertEqual(followups.due(self.store, "review", now=three_days), [])


class AgentOffline(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def say(self, phone, text):
        return asyncio.run(self.agent.handle(phone, text, "Alice"))

    def test_logistics_answer(self):
        reply = self.say(ALICE, "What are your opening hours?")
        self.assertEqual(reply.action, Action.ANSWER)
        self.assertIn("Monday to Saturday", reply.text)

    def test_unknown_logistics_goes_to_staff(self):
        reply = self.say(ALICE, "Do you have wifi in the waiting room?")
        self.assertEqual(reply.action, Action.ESCALATE)
        self.assertIsNotNone(reply.handoff_id)

    def test_booking_flow(self):
        offer = self.say(ALICE, "Book an appointment with Dr. Rao")
        self.assertEqual(offer.action, Action.BOOK)
        slot = re.search(r"BOOK (\S+)", offer.text).group(1)
        booked = self.say(ALICE, f"BOOK {slot}")
        self.assertIn("Booked", booked.text)
        appt_id = re.search(r"(APT-[0-9A-F]+)", booked.text).group(1)
        # Bob can't cancel Alice's appointment, even with the id.
        self.assertIn("No appointment", self.say(BOB, f"CANCEL {appt_id}").text)
        self.assertIn("Cancelled", self.say(ALICE, f"CANCEL {appt_id}").text)

    def test_opt_out(self):
        self.say(ALICE, "STOP")
        ref = patient_ref(ALICE, self.agent.settings.pii_pepper)
        self.assertTrue(self.agent.store.get_patient(ref)["opted_out"])

    def test_audit_has_no_phone(self):
        self.say(ALICE, "my number is 9876543210, can I take dolo?")
        log = (self.agent.settings.data_dir / "audit.jsonl").read_text()
        self.assertNotIn("9876543210", log)
        self.assertNotIn("9000000001", log)


if __name__ == "__main__":
    unittest.main()
