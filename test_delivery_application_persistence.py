import tempfile
import unittest
from pathlib import Path

from telegram.ext import ConversationHandler, PicklePersistence

from app.bot.application import (
    AtomicPicklePersistence,
    CONVERSATION_PERSISTENCE_NAME,
    PERSISTENCE_UPDATE_INTERVAL,
    build_application,
)
from app.config import Settings


class DeliveryApplicationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def build_settings(self, directory: str) -> Settings:
        return Settings(
            bot_token="123456:TEST_TOKEN",
            delivery_group_id=-1001234567890,
            location_channel_id=-1009876543210,
            database_path=Path(directory) / "delivery.db",
            manager_ids=frozenset({1}),
            courier_ids=frozenset({2}),
        )

    async def test_only_user_and_conversation_state_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            application = build_application(self.build_settings(directory))
            persistence = application.persistence

            self.assertIsInstance(persistence, AtomicPicklePersistence)
            self.assertEqual(
                persistence.filepath,
                Path(directory) / "delivery-state.pickle",
            )
            self.assertEqual(persistence.update_interval, PERSISTENCE_UPDATE_INTERVAL)
            self.assertTrue(persistence.store_data.user_data)
            self.assertFalse(persistence.store_data.bot_data)
            self.assertFalse(persistence.store_data.chat_data)
            self.assertFalse(persistence.store_data.callback_data)

            conversations = [
                handler
                for handlers in application.handlers.values()
                for handler in handlers
                if isinstance(handler, ConversationHandler)
            ]
            self.assertEqual(len(conversations), 2)
            self.assertTrue(all(handler.persistent for handler in conversations))
            self.assertEqual(
                {handler.name for handler in conversations},
                {CONVERSATION_PERSISTENCE_NAME, "delivery_order_edit"},
            )

            await persistence.update_user_data(101, {"draft": {"product": "A56"}})
            await persistence.update_conversation(
                CONVERSATION_PERSISTENCE_NAME,
                (101, 101),
                2,
            )
            await persistence.flush()

            restored = PicklePersistence(
                filepath=persistence.filepath,
                store_data=persistence.store_data,
                update_interval=PERSISTENCE_UPDATE_INTERVAL,
            )
            restored.set_bot(application.bot)
            self.assertEqual(
                (await restored.get_user_data())[101],
                {"draft": {"product": "A56"}},
            )
            self.assertEqual(
                (await restored.get_conversations(CONVERSATION_PERSISTENCE_NAME))[(101, 101)],
                2,
            )

    async def test_corrupt_state_is_quarantined_instead_of_blocking_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "delivery-state.pickle"
            state_path.write_bytes(b"not-a-pickle")
            persistence = AtomicPicklePersistence(filepath=state_path)
            application = build_application(self.build_settings(directory))
            persistence.set_bot(application.bot)

            self.assertEqual(await persistence.get_user_data(), {})
            self.assertFalse(state_path.exists())
            quarantined = list(Path(directory).glob("delivery-state.pickle.corrupt-*"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_bytes(), b"not-a-pickle")


if __name__ == "__main__":
    unittest.main()
