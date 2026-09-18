import json
import math
import os
import re
import shutil
import struct
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from call_transcription import CallTranscriber, TranscriptionConfig, CallTranscript, TranscriptSegment
from call_transcription.audio import prepared_audio
from call_transcription.diarization import align_segment, assign_speaker, speech_chunks
from call_transcription.errors import AudioDecodeError, NoSpeechDetectedError, ModelMemoryError
from call_transcription.models import ASRSegment, SpeakerTurn, Word
from call_transcription.processing import (
    ProductNameNormalizer, detect_language, merge_same_speaker, normalize_text, suspicious_segment,
)
from call_transcription.roles import RoleResolver
from call_transcription.telegram import append_transcript_quote, utf16_length


def segment(text="Да.", speaker="SPEAKER_00", start=0, end=1):
    return TranscriptSegment(start, end, speaker, None, text, confidence=0.9)


def write_wav(path, duration=8, rate=16000):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(i / 10))) for i in range(int(duration * rate))))


def example_result():
    return CallTranscript("fixture.wav", 8, [
        replace(segment("Здравствуйте, магазин Texnikach. Слушаю вас.", start=0.5, end=3), role="manager", language="ru"),
        replace(segment("Assalomu alaykum, Samsung S25 Ultra bormi?", "SPEAKER_01", 4, 7), role="client", language="uz"),
    ], "Здравствуйте. Assalomu alaykum.", {
        "SPEAKER_00": {"role": "manager", "role_confidence": 0.9, "label": "speaker_1"},
        "SPEAKER_01": {"role": "client", "role_confidence": 0.9, "label": "speaker_2"},
    })


class LocalTranscriptionUnitTests(unittest.TestCase):
    def test_merge_same_speaker(self):
        items = [segment("Да.", start=10, end=11.5), segment("Конечно.", start=11.6, end=13), segment("Сейчас посмотрю.", start=13.1, end=15)]
        result = merge_same_speaker(items)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0].start, result[0].end, result[0].text), (10, 15, "Да. Конечно. Сейчас посмотрю."))
        self.assertEqual(items[0].end, 11.5)

    def test_different_speakers_not_merged(self):
        self.assertEqual(len(merge_same_speaker([segment(), segment("Нет.", "SPEAKER_01", 1.1, 2)])), 2)
        self.assertEqual(len(merge_same_speaker([segment(), segment(start=3, end=4)])), 2)

    def test_mixed_language(self):
        cases = {
            "iPhone 17 Pro bor, черный тоже есть.": "mixed",
            "Здравствуйте, Samsung S25 Ultra есть?": "ru",
            "Ha, bor. Qaysi rang kerak?": "uz",
            "Samsung S25 Ultra bormi, черный цвет?": "mixed",
            "13 million 500 ming so'm": "uz", "Ҳа, бор, раҳмат": "uz",
            "Apple Samsung PlayStation MacBook 256 GB": "unknown",
            "Hello thank you": "unknown", "Нет": "ru", "Yo‘q": "uz", "Ha": "uz",
        }
        for text, language in cases.items():
            with self.subTest(text=text):
                self.assertEqual(detect_language(text), language)

    def test_role_resolver(self):
        resolution = RoleResolver().resolve(example_result().segments)
        self.assertEqual(resolution.manager_speaker, "SPEAKER_00")
        self.assertGreaterEqual(resolution.confidence, 0.8)
        reverse = [segment("Здравствуйте, магазин Texnikach, слушаю вас.", "SPEAKER_01"), segment("Bormi?", "SPEAKER_00", 2, 3)]
        self.assertEqual(RoleResolver().resolve(reverse).manager_speaker, "SPEAKER_01")

    def test_greetings_and_bor_do_not_assign_role(self):
        items = [segment("Здравствуйте! Цена?"), segment("Assalomu alaykum. Bor.", "SPEAKER_01", 2, 3)]
        self.assertIsNone(RoleResolver().resolve(items).manager_speaker)
        self.assertIsNone(RoleResolver().resolve([items[0]]).manager_speaker)
        same_cues = [segment("Магазин Texnikach, слушаю вас."), segment("Магазин Texnikach, слушаю вас.", "SPEAKER_01", 2, 3)]
        self.assertIsNone(RoleResolver().resolve(same_cues).manager_speaker)

    def test_product_normalization(self):
        normalizer = ProductNameNormalizer(["iPhone 17 Pro Max"], {
            "айфон 17 про макс": "iPhone 17 Pro Max", "самсунг 99": "Samsung 99",
        })
        self.assertEqual(normalizer.normalize("айфон 17 про макс 256 GB, 1350 долларов"), "iPhone 17 Pro Max 256 GB, 1350 долларов")
        self.assertEqual(normalizer.normalize("самсунг 99"), "самсунг 99")
        self.assertEqual(ProductNameNormalizer().normalize("S25 Ultra, 13 500 000 сум"), "S25 Ultra, 13 500 000 сум")

    def test_safe_normalization_does_not_paraphrase(self):
        self.assertEqual(normalize_text("ну  да есть кажется сейчас посмотрю"), "ну да есть кажется сейчас посмотрю")
        self.assertEqual(normalize_text("Yo‘q  ,  yo‘q"), "Yo‘q, yo‘q")

    def test_short_answers_survive(self):
        for text in ("Да", "Нет", "Bor", "Yo'q", "Ha", "Aha"):
            self.assertFalse(suspicious_segment(ASRSegment(0, 0.3, text, no_speech_probability=0.7, avg_logprob=-1.1)))
        self.assertTrue(suspicious_segment(ASRSegment(0, 3, "Выдуманный длинный текст", no_speech_probability=0.95, avg_logprob=-2)))

    def test_only_low_confidence_long_repetitions_filtered(self):
        first = ASRSegment(0, 1, "Подписывайтесь на наш канал пожалуйста")
        self.assertTrue(suspicious_segment(replace(first, avg_logprob=-2), first))
        self.assertFalse(suspicious_segment(first, first))
        self.assertFalse(suspicious_segment(ASRSegment(1, 2, "Да"), ASRSegment(0, 1, "Да")))

    def test_word_alignment_and_overlap_uncertainty(self):
        turns = [SpeakerTurn("SPEAKER_00", 10, 12), SpeakerTurn("SPEAKER_01", 12, 14)]
        asr = ASRSegment(0, 4, "Да Нет", [Word(0.2, 1, "Да", 0.9), Word(2.1, 3, "Нет", 0.8)])
        aligned = align_segment(asr, 10, 14, turns)
        self.assertEqual([s.speaker_id for s in aligned], ["SPEAKER_00", "SPEAKER_01"])
        self.assertEqual([s.start for s in aligned], [10.2, 12.1])
        self.assertEqual(assign_speaker(1, 2, [SpeakerTurn("A", 0, 3), SpeakerTurn("B", 0, 3)]), ("SPEAKER_UNKNOWN", True))
        self.assertEqual(assign_speaker(5, 6, turns), ("SPEAKER_UNKNOWN", False))

    def test_vad_chunks_skip_silence_and_do_not_duplicate_overlap(self):
        turns = [SpeakerTurn("A", 0, 2), SpeakerTurn("B", 1, 3), SpeakerTurn("A", 40, 45)]
        chunks = list(speech_chunks(turns, 20))
        self.assertEqual(chunks, [(0, 1), (1, 2), (2, 3), (40, 45)])
        self.assertEqual(list(speech_chunks([SpeakerTurn("A", 0, 45)], 20)), [(0, 20), (20, 40), (40, 45)])

    def test_json_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            example_result().save_json(path)
            data = json.loads(path.read_text())
            self.assertEqual(data["segments"][1]["language"], "uz")
            self.assertEqual(data["dialogue"][0]["role"], "manager")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_txt_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.txt"
            example_result().save_txt(path)
            self.assertIn("[00:00:04] Клиент:\nAssalomu alaykum", path.read_text())

    def test_telegram_quote_escapes_and_obeys_caption_limit(self):
        data = example_result().to_dict()
        data["segments"][0]["text"] = '<b>не тег</b> & ' + "📝" * 1200
        text = append_transcript_quote("👤 Менеджер: <b>Olmas</b>", data, full_url="https://example.test/full")
        self.assertIn("<blockquote expandable>", text)
        self.assertIn("&lt;b&gt;", text)
        self.assertIn("Фрагмент", text)
        import html
        self.assertLessEqual(utf16_length(html.unescape(re.sub(r"<[^>]+>", "", text))), 1024)
        self.assertTrue(text.endswith("</blockquote>"))

    def test_backend_auto_selection(self):
        with patch("platform.system", return_value="Darwin"), patch("platform.machine", return_value="arm64"):
            self.assertEqual(TranscriptionConfig().resolved_backend(), "mlx")
        with patch("platform.system", return_value="Linux"):
            self.assertEqual(TranscriptionConfig().resolved_backend(), "faster-whisper")


class LocalPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = Path(self.tmp.name) / "call.wav"
        write_wav(self.audio)
        self.config = TranscriptionConfig(normalize_audio=False)
        self.diarizer = Mock()
        self.diarizer.diarize.return_value = [SpeakerTurn("SPEAKER_00", 0.5, 3), SpeakerTurn("SPEAKER_01", 4, 7)]
        self.backend = Mock()
        self.backend.transcribe.side_effect = lambda path, **kw: [ASRSegment(
            0, kw["end"] - kw["start"], "Здравствуйте, магазин Texnikach. Слушаю вас." if kw["start"] == 0.5 else "Samsung S25 Ultra bormi?",
            confidence=0.9,
        )]

    def transcriber(self, **options):
        return CallTranscriber(self.config, backend=self.backend, diarizer=self.diarizer, **options)

    def test_pipeline_two_languages_roles_timestamps_and_reuse(self):
        service = self.transcriber(context_terms_provider=lambda _: ["iPhone 17 Pro Max"])
        for _ in range(2):
            result = service.transcribe(self.audio, call_id=12)
            self.assertEqual([s.language for s in result.segments], ["ru", "uz"])
            self.assertEqual([s.role for s in result.segments], ["manager", "client"])
            self.assertEqual([s.start for s in result.segments], [0.5, 4])
        self.assertEqual(self.diarizer.diarize.call_count, 2)
        self.assertIs(service.backend, self.backend)
        self.assertIn("iPhone 17 Pro Max", self.backend.transcribe.call_args.kwargs["initial_prompt"])

    def test_empty_audio(self):
        write_wav(self.audio, duration=0)
        with self.assertRaises(NoSpeechDetectedError):
            self.transcriber().transcribe(self.audio)
        self.backend.transcribe.assert_not_called()

    def test_no_speech(self):
        self.diarizer.diarize.return_value = []
        with self.assertRaises(NoSpeechDetectedError):
            self.transcriber().transcribe(self.audio)
        self.backend.transcribe.assert_not_called()

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            self.transcriber().transcribe(Path(self.tmp.name) / "not-here.mp3")

    def test_single_speaker_is_not_invented_into_two_roles(self):
        self.diarizer.diarize.return_value = [SpeakerTurn("SPEAKER_00", 0.5, 3)]
        result = self.transcriber().transcribe(self.audio)
        self.assertIsNone(result.segments[0].role)
        self.assertIn("speaker_count_mismatch", result.warnings)
        self.assertEqual(result.dialogue[0]["role"], "speaker_1")

    def test_privacy_no_client_text_in_logs(self):
        with self.assertLogs("call_transcription.transcriber", level="INFO") as logs:
            self.transcriber().transcribe(self.audio, call_id=42)
        self.assertIn("call_id=42", " ".join(logs.output))
        self.assertNotIn("Здравствуйте", " ".join(logs.output))
        self.assertNotIn(str(self.audio), " ".join(logs.output))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg unavailable")
    def test_normalized_temp_is_removed_after_failure_original_preserved(self):
        service = self.transcriber()
        service.config = replace(self.config, normalize_audio=True)
        self.backend.transcribe.side_effect = ModelMemoryError("oom")
        with self.assertRaises(ModelMemoryError):
            service.transcribe(self.audio)
        normalized = self.backend.transcribe.call_args.args[0]
        self.assertFalse(Path(normalized).exists())
        self.assertTrue(self.audio.exists())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg unavailable")
    def test_corrupt_audio_and_missing_ffmpeg(self):
        corrupt = Path(self.tmp.name) / "bad.mp3"
        corrupt.write_bytes(b"invalid audio")
        for path, config in ((corrupt, replace(self.config, normalize_audio=True)),
                             (self.audio, replace(self.config, normalize_audio=True, ffmpeg="no-such-ffmpeg"))):
            with self.subTest(path=path):
                with self.assertRaises(AudioDecodeError):
                    with prepared_audio(path, config):
                        pass


class BackendContractTests(unittest.TestCase):
    def test_pyannote_model_loaded_once_and_overlap_retained(self):
        from call_transcription.diarization import PyannoteDiarizer
        pipeline = Mock()
        pipeline.return_value.speaker_diarization.itertracks.return_value = [
            (SimpleNamespace(start=0, end=2), "track1", "SPEAKER_00"),
            (SimpleNamespace(start=1, end=3), "track2", "SPEAKER_01"),
        ]
        factory = Mock(return_value=pipeline)
        torch = Mock()
        torch.cuda.is_available.return_value = False
        with patch.dict("sys.modules", {"torch": torch,
             "pyannote.audio": SimpleNamespace(Pipeline=SimpleNamespace(from_pretrained=factory))}), \
             patch("call_transcription.diarization.read_samples", return_value=[]):
            diarizer = PyannoteDiarizer(TranscriptionConfig())
            for _ in range(2):
                turns = diarizer.diarize("audio.wav")
        factory.assert_called_once()
        self.assertTrue(Path(factory.call_args.args[0]).is_absolute())
        self.assertEqual(pipeline.call_args.kwargs["num_speakers"], 2)
        self.assertEqual(turns[1].start, 1)

    def test_faster_whisper_local_multilingual_model_loaded_once(self):
        from call_transcription.asr.faster_whisper_backend import FasterWhisperBackend
        factory = Mock()
        factory.return_value.transcribe.return_value = ([], SimpleNamespace())
        with patch.dict("sys.modules", {"faster_whisper": SimpleNamespace(WhisperModel=factory),
                                      "ctranslate2": SimpleNamespace(get_cuda_device_count=lambda: 0)}), \
             patch("call_transcription.asr.faster_whisper_backend.read_samples", return_value=[]):
            backend = FasterWhisperBackend(TranscriptionConfig())
            for _ in range(2):
                backend.transcribe("test.wav", start=1, end=2, initial_prompt="RU/UZ")
        factory.assert_called_once()
        self.assertEqual(factory.call_args.kwargs["device"], "cpu")
        self.assertTrue(factory.call_args.kwargs["local_files_only"])
        options = factory.return_value.transcribe.call_args.kwargs
        self.assertIsNone(options["language"])
        self.assertTrue(options["multilingual"])
        self.assertEqual(options["task"], "transcribe")

    def test_mlx_never_forces_ru_or_logs_speech(self):
        from call_transcription.asr.mlx_backend import MLXWhisperBackend
        transcribe = Mock(return_value={"segments": []})
        with patch.dict("sys.modules", {"mlx_whisper": SimpleNamespace(transcribe=transcribe)}), \
             patch("call_transcription.asr.mlx_backend.read_samples", return_value=[]):
            MLXWhisperBackend(TranscriptionConfig()).transcribe("test.wav", start=0, end=1, initial_prompt="RU/UZ")
        self.assertEqual(transcribe.call_args.kwargs["task"], "transcribe")
        self.assertIsNone(transcribe.call_args.kwargs["language"])
        self.assertIsNone(transcribe.call_args.kwargs["verbose"])
        self.assertTrue(Path(transcribe.call_args.kwargs["path_or_hf_repo"]).is_absolute())
        self.assertEqual(os.environ["PYANNOTE_METRICS_ENABLED"], "0")

    def test_oom_error_is_actionable(self):
        from call_transcription.asr.base import inference_error
        self.assertIsInstance(inference_error(RuntimeError("CUDA out of memory")), ModelMemoryError)


if __name__ == "__main__":
    unittest.main()
