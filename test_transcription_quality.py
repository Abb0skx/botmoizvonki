import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from call_transcription import CallTranscriber, TranscriptionConfig
from call_transcription.asr.hybrid_backend import choose_ru_uz_by_phrases
from call_transcription.asr.review_backend import ReviewBackend
from call_transcription.catalog import load_catalog, catalogue_aliases
from call_transcription.models import ASRSegment, SpeakerTurn, Word
from call_transcription.processing import ProductNameNormalizer, sanitize_transcript_dict
from scripts.evaluate_call_asr import distance, evaluate
from scripts.requeue_recent_transcriptions import requeue
from test_call_transcription import write_wav


class QualityTests(unittest.TestCase):
    def test_gigaam_missing_checkpoint_never_downloads(self):
        from call_transcription.asr.gigaam_backend import GigaAMBackend
        from call_transcription.errors import ConfigurationError
        with tempfile.TemporaryDirectory() as tmp:
            module = Mock()
            backend = GigaAMBackend(TranscriptionConfig(backend='gigaam',gigaam_model_path=tmp))
            with patch.dict('sys.modules', {'torch':Mock(),'gigaam':module}):
                with self.assertRaises(ConfigurationError):
                    backend.transcribe('fake.wav',start=0,end=1,initial_prompt='')
            module.load_model.assert_not_called()

    def test_gigaam_public_api_windows_have_relative_word_timestamps(self):
        from call_transcription.asr.gigaam_backend import GigaAMBackend
        class Samples:
            def __len__(self): return 16000
            def __mul__(self, value): return self
            def clip(self, *args): return self
            def astype(self, *args): return self
            def tobytes(self): return b'\0' * 32000
        backend = GigaAMBackend(TranscriptionConfig(backend='gigaam'))
        backend._model = Mock()
        backend._model.transcribe.return_value = SimpleNamespace(text='да',words=[SimpleNamespace(start=.1,end=.5,text='да')])
        with patch.dict('sys.modules',{'torch':Mock(),'gigaam':Mock()}), patch('call_transcription.asr.gigaam_backend.read_samples',return_value=Samples()):
            result = backend.transcribe('fake',start=10,end=36,initial_prompt='')
        self.assertEqual(backend._model.transcribe.call_count,2)
        self.assertAlmostEqual(result[0].words[0].start,.1)
        self.assertAlmostEqual(result[1].words[0].start,24.1)

    def test_russian_transliteration_loses_to_russian_phrase(self):
        ru = [ASRSegment(0, 3, "подскажите пожалуйста у вас есть", [
            Word(0, .5, "подскажите"), Word(.5, 1, "пожалуйста"),
            Word(1, 1.3, "у"), Word(1.3, 1.6, "вас"), Word(1.6, 2, "есть")])]
        uz = [ASRSegment(0, 3, "Podkazite pojaluysta u vas yest", [
            Word(0, .5, "Podkazite", .99), Word(.5, 1, "pojaluysta", .99),
            Word(1, 1.3, "u", .99), Word(1.3, 1.6, "vas", .99), Word(1.6, 2, "yest", .99)])]
        result = choose_ru_uz_by_phrases(ru, uz)
        self.assertIn("подскажите", result[0].text)

    def test_short_pause_allows_language_switch_inside_six_seconds(self):
        ru = [ASRSegment(0, 5, "", [Word(0, .5, "Здравствуйте"), Word(.5, 1, "сколько"),
                                   Word(2, 2.5, "борми"), Word(2.5, 3, "канча")])]
        uz = [ASRSegment(0, 5, "", [Word(0, .5, "zdravstvuyte", .7), Word(.5, 1, "skolko", .7),
                                   Word(2, 2.5, "bormi", .9), Word(2.5, 3, "qancha", .9)])]
        result = choose_ru_uz_by_phrases(ru, uz)
        self.assertEqual([s.language for s in result], ["ru", "uz"])
        self.assertEqual(len([w for s in result for w in s.words]), 4)

    def test_product_alias_only_for_existing_complete_model(self):
        terms = ["Google Pixel 9A"]
        normalizer = ProductNameNormalizer(terms, catalogue_aliases(terms))
        self.assertEqual(normalizer.normalize("гугл пиксель девять а"), "Google Pixel 9A")
        self.assertEqual(normalizer.normalize("гугл пиксель восемь а 7000000"), "гугл пиксель восемь а 7000000")

    def test_catalogue_sqlite_is_read_only_and_has_current_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp, "prices.db")
            with sqlite3.connect(db) as c:
                c.execute("CREATE TABLE price_snapshots(payload_json TEXT, is_current INTEGER)")
                c.execute("INSERT INTO price_snapshots VALUES (?,1)", (json.dumps({"products":[{"model_name":"Google Pixel 9A"}]}),))
            before = db.read_bytes()
            terms, aliases = load_catalog(db)
            self.assertEqual(terms, ["Google Pixel 9A"])
            self.assertEqual(db.read_bytes(), before)

    def test_missing_reference_never_claims_accuracy(self):
        result = evaluate([{"call_id": 12, "reference": None, "verified": False}], "/missing", "gigaam")
        self.assertIsNone(result['wer'])
        self.assertTrue(result['requires_human_references'])
        self.assertEqual(distance("one two".split(), "one three four".split()), 2)

    def test_low_confidence_word_is_masked_without_losing_good_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "test.wav")
            write_wav(path, 3)
            backend = Mock()
            backend.transcribe.return_value = [ASRSegment(0, 3, "Да шум есть", [
                Word(0, .5, "Да", .95), Word(.5, 1, "шум", .01), Word(1, 2, "есть", .9)])]
            diarizer = Mock()
            diarizer.diarize.return_value = [SpeakerTurn("SPEAKER_00", 0, 2)]
            result = CallTranscriber(TranscriptionConfig(normalize_audio=False), backend=backend, diarizer=diarizer).transcribe(path)
            self.assertIn("Да", result.full_text)
            self.assertIn("есть", result.full_text)
            self.assertIn("[неразборчиво]", result.full_text)
            self.assertEqual(result.raw_segments[0]['text'], "Да шум есть")
            self.assertTrue(any(s.raw_text == "шум" for s in result.segments))
            repaired = sanitize_transcript_dict(result.to_dict())
            self.assertEqual(repaired['raw_segments'], result.raw_segments)

    def test_review_is_bounded_and_offsets_are_clipped(self):
        inner = Mock()
        weak = ASRSegment(0, 2, "шум", confidence=.1)
        good = ASRSegment(0, 4, "да есть", [Word(1.1, 1.5, "да", .9), Word(2, 2.5, "есть", .9)], confidence=.9)
        inner.transcribe.side_effect = [[weak], [good], [weak]]
        review = ReviewBackend(inner, max_reviews=1)
        review.begin_call(20)
        result = review.transcribe("fake", start=5, end=7, initial_prompt="")
        self.assertAlmostEqual(result[0].words[0].start, .1)
        self.assertTrue(review.reviews[0]['used_retry'])
        review.transcribe("fake", start=10, end=12, initial_prompt="")
        self.assertEqual(inner.transcribe.call_count, 3)

    def test_exclusive_timeline_keeps_overlap_evidence(self):
        from call_transcription.diarization import PyannoteDiarizer
        regular = Mock()
        regular.itertracks.return_value = [(SimpleNamespace(start=0,end=2),'a','SPEAKER_00'),
                                          (SimpleNamespace(start=1,end=3),'b','SPEAKER_01')]
        exclusive = Mock()
        exclusive.itertracks.return_value = [(SimpleNamespace(start=0,end=1.5),'a','SPEAKER_00'),
                                            (SimpleNamespace(start=1.5,end=3),'b','SPEAKER_01')]
        pipeline = Mock(return_value=SimpleNamespace(speaker_diarization=regular, exclusive_speaker_diarization=exclusive))
        with patch.dict('sys.modules', {'torch': Mock(), 'pyannote.audio':SimpleNamespace(Pipeline=SimpleNamespace(from_pretrained=Mock(return_value=pipeline)))}), patch('call_transcription.diarization.read_samples',return_value=[]):
            diarizer = PyannoteDiarizer(TranscriptionConfig())
            turns = diarizer.diarize('test')
        self.assertEqual(turns[0].end, 1.5)
        self.assertEqual(diarizer.overlap_intervals, [(1.,2.)])

    def test_requeue_preserves_old_transcript_and_skips_internal_and_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp,'calls.db')
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE calls(id INTEGER, recording TEXT, duration INTEGER, is_internal_contact INTEGER)')
                c.execute('CREATE TABLE call_transcriptions(call_id INTEGER,status TEXT,attempts INTEGER,next_attempt_at INTEGER,queued_at INTEGER,updated_at INTEGER,error TEXT,lease_token TEXT,lease_until INTEGER,transcript_json TEXT)')
                for id,status,internal in ((1,'completed',0),(2,'processing',0),(3,'completed',1)):
                    c.execute('INSERT INTO calls VALUES (?,?,?,?)',(id,'recording',20,internal))
                    c.execute('INSERT INTO call_transcriptions(call_id,status,transcript_json) VALUES (?,?,?)',(id,status,'old'))
            self.assertEqual(requeue(db), [1])
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute('SELECT transcript_json,status FROM call_transcriptions WHERE call_id=1').fetchone(),('old','queued'))


if __name__ == '__main__':
    unittest.main()
