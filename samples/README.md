# Sample inputs

Test ads in every supported format. **All doctors, clinics, addresses and phone numbers are
fictional**, and the spoken ads use a synthetic voice.

| File | Format | What it tests |
|---|---|---|
| `poster_en.png` | Image (English) | OCR; superiority, guarantee, painless, patient count, Diwali discount, call-to-action |
| `poster_hi.png` | Image (Hindi) | Devanagari OCR; "सबसे अच्छे", "जड़ से इलाज", "100% गारंटी", "मुफ्त जांच" |
| `brochure_scanned.pdf` | Scanned PDF (hospital) | OCR fallback; "most trusted", success rates, "no side effects", free package offer |
| `clinic_website.pdf` | Text PDF | Before/after, star ratings, celebrity endorsement, "clinically proven" |
| `reel_script.docx` | Word | Fear-based hook, success rate, testimonials, free consultation, "DM us now" |
| `spoken_ad_en.mp3` | Audio | Speech-to-text of a spoken ad |
| `voice_note_hi.ogg` | Audio (Opus, like WhatsApp) | Hindi speech-to-text |
| `reel_en.mp4` | Video | Voice-over transcript + on-screen captions read from frames |
| `voice_note_1_hinglish_not_compliant.ogg` | Voice note (Hinglish, male) | "Number one", permanent cure, 100% result guarantee, no side effects, 5000+ happy patients, Diwali discount, free consultation, "call today" |
| `voice_note_2_english_not_compliant.ogg` | Voice note (English) | Fear ("could be fatal"), "reverse your diabetes", no side effects, 2,000 patients, happy patients, free check-up offer, "book today" |
| `voice_note_3_english_compliant.ogg` | Voice note (English) | Formal change-of-address notice with name, qualifications and registration number: should be **Compliant** |
| `voice_note_4_hindi_needs_changes.ogg` | Voice note (Hindi) | Dengue-awareness tips with no promotion: **Needs changes** only because qualifications and registration number are missing |

For the Hindi voice notes choose "Spoken language: Hindi or Hinglish", and use `WHISPER_MODEL=large-v3-turbo`
in `.env` for accurate Hindi transcripts (the default `small` model is fine for English).
