import pytest

from app.engine import disclosures
from app.engine.rules import load_rules, scan
from app.textnorm import normalize


def hits(text: str) -> set[tuple[str, str]]:
    text = normalize(text)
    return {(h.rule.id, text[h.start : h.end].lower()) for h in scan(text)}


def rule_ids(text: str) -> set[str]:
    return {rule for rule, _ in hits(text)}


def test_rules_load():
    rules = load_rules()
    assert len(rules) >= 20
    assert all(r.patterns for r in rules)
    assert {r.severity for r in rules} <= {"high", "medium", "low"}


@pytest.mark.parametrize(
    "text, rule",
    [
        ("Mumbai's No.1 hair transplant surgeon", "superiority_claim"),
        ("Best cardiologist in Pune", "superiority_claim"),
        ("India's most trusted hospital", "superiority_claim"),
        ("Our success rate is the highest in Pune", "superiority_claim"),
        ("100% guaranteed results", "guaranteed_outcome"),
        ("Permanent cure for back pain", "guaranteed_outcome"),
        ("We cure diabetes naturally", "disease_cure_claim"),
        ("Painless delivery package", "painless_miracle"),
        ("A miracle treatment for knees", "painless_miracle"),
        ("Laser with no side effects", "conceals_risk"),
        ("10,000+ happy patients", "success_rate_numbers"),
        ("98% success rate", "success_rate_numbers"),
        ("Diwali offer: flat 40% off", "inducement_offer"),
        ("Free first consultation this month only", "inducement_offer"),
        ("Free full body check-up", "inducement_offer"),
        ("Refer a friend and earn cashback", "inducement_offer"),
        ("Consultation at just Rs 299", "price_framing"),
        ("Book your appointment now!", "solicitation_cta"),
        ("Don't ignore chest pain, it could be a heart attack", "fear_marketing"),
        ("Hear what our patients say", "testimonial_review"),
        ("Rated 4.9 stars on Google", "testimonial_review"),
        ("See the before and after photos", "before_after"),
        ("Trusted by Bollywood stars", "celebrity_endorsement"),
        ("Dermatologist recommended sunscreen", "product_endorsement"),
        ("20% referral commission for doctors", "referral_commission"),
        ("Buy 5k followers", "fake_engagement"),
        ("Award-winning eye hospital", "award_ranking"),
        ("Renowned surgeon with expert hands", "doctor_self_promotion"),
        ("Knee pain cured without surgery", "unproven_therapy"),
        ("Watch the live surgery on our page", "live_surgery"),
        ("Our patient Mrs. Sharma, 45 years, from Andheri", "patient_identity"),
        # Hindi / Hinglish
        ("शहर के सबसे अच्छे डॉक्टर", "superiority_claim"),
        ("बवासीर का जड़ से इलाज", "disease_cure_claim"),
        ("100% गारंटी", "guaranteed_outcome"),
        ("कोई साइड इफेक्ट नहीं", "conceals_risk"),
        ("5000+ संतुष्ट मरीज", "success_rate_numbers"),
        # mixed-script / spelling variants seen in real Hinglish speech-to-text
        ("कोई side effect नहीं", "conceals_risk"),
        ("सौ प्रतिशत रिजल्ट की गारंटी", "guaranteed_outcome"),
        ("हेयर फॉल का पर्मानेंट इलाज", "guaranteed_outcome"),
        ("50 प्रतिशत डिस्काउंट और फ्री Consultation", "inducement_offer"),
        ("पाँच हजार से ज्यादा खुश मरीज़", "success_rate_numbers"),
        ("दिवाली ऑफर: मुफ्त जांच", "inducement_offer"),
        ("अभी कॉल करें", "solicitation_cta"),
        ("बिना ऑपरेशन इलाज", "unproven_therapy"),
        ("pakka ilaj, sabse accha clinic", "guaranteed_outcome"),
        ("Abhi call karein!", "solicitation_cta"),
    ],
)
def test_flags_prohibited_phrases(text, rule):
    assert rule in rule_ids(text), hits(text)


@pytest.mark.parametrize(
    "text",
    [
        "Results are not guaranteed and vary from person to person.",
        "There is no miracle cure for obesity; diet and exercise help.",
        "Shop No. 1, Ground floor, Andheri West.",
        "With best wishes for the festive season.",
        "Heart disease is a leading cause of death.",
        "Call our toll-free number 1800 000 000.",
        "We offer cardiology, orthopaedics and paediatrics.",
        "Sugar-free diet advice is part of diabetes care.",
        "National Medical Commission guidelines apply to all doctors.",
        "Consultation fee: ₹600. OPD timings: Mon-Sat 10 am-1 pm.",
        "Dr. Anita Shah, MBBS, MD (General Medicine), Reg. No. MMC 2009/04/1234",
        "हमारा क्लिनिक नए पते पर शिफ्ट हो गया है। समय: सुबह 10 से 1 बजे।",
    ],
)
def test_ignores_permitted_text(text):
    assert hits(text) == set()


def test_disclosure_detection():
    found = disclosures.check(
        normalize("Dr. Anita Shah, MBBS, MD. Reg. No. MMC 2009/04/1234"), "rmp", "social"
    )
    assert found.required and found.doctor_name and found.qualifications and found.registration_number
    assert found.missing("no") == []

    missing = disclosures.check("Best skin clinic in Bandra", "hospital", "website").missing("yes")
    assert {m["item"] for m in missing} == {"doctor_name", "qualifications", "registration_number", "ai_source_label"}


def test_disclosure_not_required_for_print_or_others():
    assert not disclosures.disclosure_required("rmp", "print")
    assert not disclosures.disclosure_required("other", "social")
    assert disclosures.disclosure_required("agency", "whatsapp")


def test_ai_label_detected():
    found = disclosures.check("This video is AI-generated. Dr. Rao, MBBS", "rmp", "social")
    assert found.ai_label
    assert all(m["item"] != "ai_source_label" for m in found.missing("yes"))
