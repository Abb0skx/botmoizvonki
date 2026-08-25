import json
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram_business.config import BusinessSettings
from telegram_business.intents import classify
from telegram_business.language import detect_language
from telegram_business.migrations import connect
from telegram_business.products import (
    ProductMatch,
    ProductVariant,
    format_ambiguous_result,
    format_result,
    load_product_urls,
    model_link_keyboard,
    normalize_model,
    safe_product_url,
)
from telegram_business.repository import BusinessRepository
from telegram_business.service import BusinessService, sender_type
from telegram_business.timeutils import business_date, is_night, manager_phrases, work_seconds
from telegram_business.telegram_api import TelegramBusinessAPI

TZ=ZoneInfo("Asia/Tashkent")


def settings(path):
    return BusinessSettings(False,"token","secret","connection","","Asia/Tashkent",time(20),time(9,30),time(10),time(20),300,3,120,720,4,8,Path(path),"sheet",60,300,"existing_google_bot_prices","",1440)


class FakeAPI:
    def __init__(self): self.sent=[]
    def send_message(self, connection_id, chat_id, text, **options):
        self.sent.append(text); return {"result":{"message_id":len(self.sent)}}


class FakeProducts:
    def search(self, query, memory=None, color=None):
        if "iphone 16 pro max" in normalize_model(query):
            return ProductMatch("found",("iPhone 16 Pro Max",),(ProductVariant("iPhone 16 Pro Max","256 GB","Black",Decimal("14500000")),))
        return ProductMatch("not_found")


class TelegramBusinessUnitTests(unittest.TestCase):
    def test_telegram_product_link_payload_can_disable_preview(self):
        class Response:
            status_code=200
            def raise_for_status(self): pass
            def json(self): return {"ok":True,"result":{"message_id":10}}
        with patch("telegram_business.telegram_api.requests.post",return_value=Response()) as post:
            TelegramBusinessAPI("secret").send_message("connection","42",'<a href="https://t.me/a/1">Phone</a>',parse_mode="HTML",reply_markup={"inline_keyboard":[]})
        payload=post.call_args.kwargs["json"]
        self.assertEqual(payload["business_connection_id"],"connection")
        self.assertEqual(payload["parse_mode"],"HTML")
        self.assertEqual(payload["link_preview_options"],{"is_disabled":True})

    def test_product_urls_and_linked_multiple_models(self):
        from openpyxl import Workbook

        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"Bot_URLS.xlsx"; book=Workbook(); sheet=book.active
            sheet.append(["post_id","product_id","Model"])
            sheet.append(["https://t.me/Texnikach_Phone/100","123, 124.0","Phone A"])
            sheet.append(["https://t.me/Texnikach_dop/200","123","Phone A duplicate"])
            sheet.append(["https://evil.example/1","125","Unsafe"]); book.save(path)
            urls=load_product_urls(path)
            self.assertEqual(urls[123],"https://t.me/Texnikach_Phone/100")
            self.assertEqual(urls[124],"https://t.me/Texnikach_Phone/100")
            self.assertNotIn(125,urls)
        match=ProductMatch("ambiguous",("Phone <A>","Phone B"),(),(("Phone <A>","https://t.me/Texnikach_Phone/100"),("Phone B","https://t.me/Texnikach_Phone/200")))
        text=format_ambiguous_result(match,"ru")
        self.assertIn("Phone &lt;A&gt;",text); self.assertEqual(text.count("https://t.me/"),2)
        self.assertEqual(len(model_link_keyboard(match)["inline_keyboard"]),2)
        self.assertIsNone(safe_product_url("https://evil.example/photo"))

    def test_price_rows_group_equal_colors_and_link_model(self):
        url="https://t.me/Texnikach_Phone/100"
        variants=(
            ProductVariant("Phone <A>","256 GB","Black",Decimal("1000000"),1,url),
            ProductVariant("Phone <A>","256 GB","White",Decimal("1000000"),2,url),
        )
        text=format_result(ProductMatch("found",("Phone <A>",),variants,(("Phone <A>",url),)),"ru")
        self.assertIn("Black, White",text); self.assertEqual(text.count("1 000 000 сум"),1)
        self.assertIn("Phone &lt;A&gt;",text); self.assertLessEqual(len(text),4096)

    def test_schedule_boundaries_and_session(self):
        values=[("2026-08-22T19:59",False),("2026-08-22T20:00",True),("2026-08-23T09:29",True),("2026-08-23T09:30",False),("2026-08-23T09:31",False)]
        for raw,expected in values: self.assertEqual(is_night(datetime.fromisoformat(raw).replace(tzinfo=TZ)),expected)
        self.assertEqual(business_date(datetime(2026,8,22,22,tzinfo=TZ)),business_date(datetime(2026,8,23,9,29,tzinfo=TZ)))
        self.assertEqual(manager_phrases(datetime(2026,8,22,21,tzinfo=TZ))[0],"завтра после 10:00")
        self.assertEqual(manager_phrases(datetime(2026,8,23,8,tzinfo=TZ))[0],"сегодня после 10:00")

    def test_language_and_intents(self):
        self.assertEqual(detect_language("Какая цена и сколько стоит?")[0],"ru")
        self.assertEqual(detect_language("Narxi qancha, kerak edi")[0],"uz")
        self.assertEqual(detect_language("Қанча, нархини айтинг")[0],"uz")
        self.assertEqual(detect_language("iPhone 16 Pro Max")[0],"bi")
        self.assertIn("credit",classify("можно в рассрочку?"))
        self.assertNotIn("credit",classify("кредит не нужен, оплачу полностью"))
        self.assertIn("order_request",classify("беру, оформите"))
        self.assertIn("complaint",classify("у меня жалоба и брак"))

    def test_sender_classification(self):
        self.assertEqual(sender_type({"sender_business_bot":{"id":9}},"1","9"),"business_bot")
        self.assertEqual(sender_type({"is_from_offline":True,"from":{"id":1}},"1"),"telegram_auto")
        self.assertEqual(sender_type({"from":{"id":1},"text":"manual"},"1"),"manager")
        self.assertEqual(sender_type({"from":{"id":1},"delete_chat_photo":True},"1"),"telegram_auto")
        self.assertEqual(sender_type({"from":{"id":2}},"1"),"client")

    def test_work_response_time(self):
        self.assertEqual(work_seconds(datetime(2026,8,22,23,15,tzinfo=TZ),datetime(2026,8,23,10,8,tzinfo=TZ)),480)
        self.assertEqual(work_seconds(datetime(2026,8,23,9,45,tzinfo=TZ),datetime(2026,8,23,10,7,tzinfo=TZ)),420)
        self.assertEqual(work_seconds(datetime(2026,8,23,15,tzinfo=TZ),datetime(2026,8,23,15,12,tzinfo=TZ)),720)
        self.assertEqual(work_seconds(datetime(2026,8,23,8,tzinfo=TZ),datetime(2026,8,23,9,tzinfo=TZ)),0)

    def test_idempotent_update_and_durable_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=BusinessRepository(Path(tmp)/"business.db"); now=datetime(2026,8,22,20,tzinfo=TZ)
            update={"update_id":1,"business_connection":{"id":"connection"}}
            self.assertTrue(repo.save_update(update,now)); self.assertFalse(repo.save_update(update,now))
            repo.schedule("debounce:1","1","s","debounce",now,{"x":1},now)
            repo.schedule("debounce:1","1","s","debounce",now+timedelta(seconds=3),{"x":2},now)
            with connect(repo.path) as db:
                self.assertEqual(db.execute("select count(*) from scheduled_actions").fetchone()[0],1)
                self.assertEqual(json.loads(db.execute("select payload from scheduled_actions").fetchone()[0])["x"],2)
            repo.replace_model_choices("s",(("Phone A","https://t.me/a/1"),("Phone B",None)),now)
            self.assertEqual(repo.model_choice("s",2)["model_name"],"Phone B")

    def test_night_model_flow_and_manual_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=datetime(2026,8,22,20,0,tzinfo=TZ); api=FakeAPI(); service=BusinessService(settings(Path(tmp)/"business.db"),clock=lambda:now,api=api,products=FakeProducts())
            service.repo.upsert_connection({"id":"connection","user":{"id":100},"rights":{"can_reply":True}},now)
            update={"update_id":1,"business_message":{"business_connection_id":"connection","message_id":10,"date":int(now.timestamp()),"chat":{"id":200,"type":"private"},"from":{"id":200,"language_code":"ru"},"text":"айфон 16 про макс"}}
            self.assertTrue(service.repo.save_update(update,now)); service.process_update(update)
            action=service.repo.due_actions(now+timedelta(seconds=3))[0]; service.repo.claim_action(action["action_id"]); service.execute(action)
            self.assertTrue(any("Цены в базе" in text for text in api.sent))
            manual={"update_id":2,"business_message":{"business_connection_id":"connection","message_id":11,"date":int(now.timestamp()),"chat":{"id":200,"type":"private"},"from":{"id":100},"text":"Ответ менеджера"}}
            self.assertTrue(service.repo.save_update(manual,now)); service.process_update(manual)
            self.assertFalse(service.repo.may_automate("200",now+timedelta(minutes=1)))


if __name__ == "__main__": unittest.main()
