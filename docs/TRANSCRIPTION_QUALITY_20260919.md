# Local RU/UZ quality release, 19 September 2026

## Changes

- Russian GigaAM v3 RNNT adapter alongside existing Whisper backends.
- Optional standalone GigaAM multilingual CTC with local word posterior scores.
- Phrase/pause-based RU/UZ routing, with conservative Russian-transliteration cues.
- Pyannote Community-1 exclusive speaker timeline, while retaining overlap flags.
- Up to two local expanded-context retries; uncertain individual words marked
  `[неразборчиво]`, raw hypotheses/timestamps retained for review.
- Exact catalogue aliases only. No fuzzy model-number or price repair.
- Previous transcript versions archived transactionally on successful replacement.
- Independent Telegram refresh loop; original call delivery does not wait for ASR.
- Private human-reference preparation and WER evaluation scripts, no invented labels.

## Real offline smoke comparison

CPU quota 1.5, two threads, CPU models, pyannote batch 1; server 8 GB.
Calls are identified only by internal ID; audio/text remain private on the server.

| Candidate | Call | Audio seconds | Processing seconds | Peak RSS KiB |
|---|---:|---:|---:|---:|
| GigaAM multilingual CTC | 2470 | 46.296 | 96.605 | 2362492 |
| GigaAM multilingual CTC | 2475 | 55.872 | 89.908 | 2463604 |
| GigaAM multilingual CTC | 2484 | 26.176 | 35.535 | 2463604 |
| GigaAM v3 + Uzbek Callcenter | 2470 | 46.296 | 236.358 | 3242380 |
| GigaAM v3 + Uzbek Callcenter | 2484 | 26.176 | 106.812 | 3287544 |
| GigaAM v3 + NavAI Uzbek | 2470 | 46.296 | 306.855 | 3283412 |
| GigaAM v3 + NavAI Uzbek | 2484 | 26.176 | 194.676 | 3283412 |

These are operational measurements, **not accuracy scores**. The preliminary
outputs show Russian Cyrillic replacing many earlier transliterated/hallucinated
phrases. NavAI did not give a convincing improvement on the problematic mixed
call; some greetings became unrelated Uzbek text and prices remained unreliable.
The Callcenter checkpoint is retained for the production Uzbek branch. The
multilingual CTC alternative is faster but preliminary Uzbek wording was less
readable; it remains configurable rather than the automatic production default.
Human references are required to validate these impressions and choose by WER.

The first comparisons predate the names-only catalogue export and CTC confidence
hook. Final smoke tests are run again before release. Genuine overlap, weak
microphones, prices, names and very short answers remain difficult. Exactly two
configured voices does not guarantee correct role or word assignment.

## Reproducibility

GigaAM source: `7447938d791c4f3e643386ee22c33777004293a5`.
Checkpoints (official download MD5 checked before inference):

- multilingual_ctc.ckpt: `5379d887c53ccd9cb95981e2a1832720`
- v3_rnnt.ckpt: `0fd2c9a1ff66abd8d32a3a07f7592815`

NavAI candidate: `navai-uz/whisper-medium-uzbek`, revision
`9c67dea55c8ac11f237d60ca0c32e5dc5a8c3de5`, locally converted CTranslate2 INT8.
No cloud inference, external LLM post-edit, training or automatic voiceprint
enrollment is performed. No calibrated confidence percentage is claimed.
