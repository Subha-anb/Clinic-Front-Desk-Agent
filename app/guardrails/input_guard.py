"""Layer 1 of the guardrail: deterministic rules on the patient's message.

Runs before any LLM sees the message, is fully testable offline, and is deliberately
biased towards escalating. The LLM classifier (layer 2) can make a verdict *stricter*
but never looser than this layer.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

from .policy import Category, Verdict

_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿]")
_LEET = str.maketrans({"@": "a", "0": "o", "1": "i", "3": "e", "$": "s", "4": "a", "5": "s", "7": "t"})


def normalize(text: str) -> tuple[str, str]:
    """Return (plain, deleeted) lower-case views of the text.

    `plain` keeps digits (needed for doses like "650 mg"); `deleeted` undoes common obfuscation
    such as "p@r@cetam0l" or "p.a.r.a.c.e.t.a.m.o.l" so it can be matched against term lists.
    """
    t = unicodedata.normalize("NFKC", text)
    t = _ZERO_WIDTH.sub("", t).lower()
    t = t.replace("’", "'")
    plain = re.sub(r"\s+", " ", t).strip()
    # Join letters split by dots/dashes/underscores: p.a.r.a -> para
    joined = re.sub(r"(?<=\w)[._\-*](?=\w)", "", plain)
    deleeted = " ".join(
        tok.translate(_LEET) if re.search(r"[a-z]", tok) else tok for tok in joined.split(" ")
    )
    return plain, deleeted


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns))


# --- Emergencies: always first, always win ------------------------------------------------
EMERGENCY = _rx(
    r"\bchest\b.{0,25}\b(pain|tight\w*|pressure|heav\w*|crush\w*)",
    r"\b(pain|tight\w*|pressure|heav\w*)\b.{0,15}\bchest\b",
    r"\b(can'?t|cannot|unable to|hard to|difficult\w* (in )?|trouble|struggling to)\s?breath\w*",
    r"\bshort(ness)? of breath\b", r"\bnot breathing\b", r"\bgasping\b",
    r"\b(unconscious|unresponsive|not responding|collapsed|passed out|fainted)\b",
    r"\b(severe|heavy|lot of|uncontroll\w*) bleeding\b", r"\bbleeding (heavily|a lot|won'?t stop|non ?stop)\b",
    r"\b(cough\w*|vomit\w*|throw\w* up) (up )?blood\b",
    r"\bstroke\b", r"\bface (is )?droop\w*", r"\bslurred speech\b",
    r"\b(seizure|convuls\w*|having fits)\b", r"\bheart attack\b", r"\bchoking\b",
    r"\b(throat|tongue|lips?|face)\b.{0,15}\b(swell\w*|swollen|closing)\b", r"\banaphyla\w*",
    r"\boverdos\w*", r"\btook (too many|a lot of|the whole)\b", r"\bswallowed\b.{0,20}\b(poison|bleach|pills|tablets|acid)\b",
    r"\bsuicid\w*", r"\bkill(ing)? myself\b", r"\bend(ing)? (it all|my life)\b", r"\bwant to die\b",
    r"\bself[- ]?harm\w*", r"\bhurt(ing)? myself\b", r"\bno reason to live\b", r"\bbetter off dead\b",
)

# --- Medicines: any mention -> clinical --------------------------------------------------
DRUGS = [
    "paracetamol", "acetaminophen", "crocin", "dolo", "calpol", "ibuprofen", "brufen", "advil",
    "aspirin", "ecosprin", "diclofenac", "combiflam", "nimesulide", "metformin", "insulin",
    "glimepiride", "amoxicillin", "augmentin", "azithromycin", "ciprofloxacin", "cetirizine",
    "levocetirizine", "allegra", "omeprazole", "pantoprazole", "ranitidine", "warfarin",
    "clopidogrel", "heparin", "atorvastatin", "rosuvastatin", "amlodipine", "telmisartan",
    "losartan", "thyroxine", "levothyroxine", "thyronorm", "eltroxin", "prednisolone",
    "dexamethasone", "salbutamol", "montelukast", "sertraline", "fluoxetine", "escitalopram",
    "alprazolam", "melatonin", "ondansetron", "domperidone", "loperamide", "folic",
]
_DRUG_RX = r"\b(" + "|".join(DRUGS) + r")\w*"
MEDICATION = _rx(
    _DRUG_RX,
    r"\b(medicine|medication|meds|tablet|pill|capsule|syrup|drops|inhaler|ointment|cream)s?\b",
    r"\b(dose|dosage|doses|dosing)\b", r"\b\d+\s?(mg|mcg|ml|iu)\b",
    r"\bprescri\w*", r"\bdrugs?\b", r"\b(antibiotic|antihistamine|painkiller|pain killer|steroid|"
    r"antidepressant|anticoagulant|statin|supplement|vitamin|contraceptive)s?\b",
    r"\bblood thinner", r"\bsleeping pill", r"\bmorning after\b", r"\bbirth control\b",
)

# --- Symptoms ----------------------------------------------------------------------------
SYMPTOM = _rx(
    r"\b(pain\w*|ache|aching|hurts?|hurting|sore|burning|cramps?)\b",
    r"\b(fever|feverish|temperature of|chills)\b", r"\b\d{2,3}(\.\d)?\s?(°|deg|degree|f\b|c\b)",
    r"\b(rash\w*|itch\w*|hives|swell\w*|swollen|lump|bump)\b",
    r"\b(cough\w*|wheez\w*|cold and|runny nose|sneez\w*)\b",
    r"\b(bleed\w*|blood in|bruis\w*)\b", r"\b(dizz\w*|faint\w*|lightheaded|vertigo)\b",
    r"\b(vomit\w*|nause\w*|diarrh\w*|loose motion\w*|constipat\w*)\b",
    r"\b(headache|migraine)\b", r"\b(infect\w*|pus|ooz\w*|discharge|wound)\b",
    r"\b(numb\w*|tingl\w*|palpitation\w*|breathless\w*|weak(ness)?)\b",
    r"\b(allerg\w*|reaction|side[- ]?effects?)\b", r"\bnot feeling (well|better|good)\b",
    r"\b(depress\w*|anxi\w*|panic attack\w*|can'?t sleep|insomnia)\b",
    r"\bfeel(ing)? (sick|unwell|worse|terrible|awful)\b",
)

# --- Results / diagnosis interpretation --------------------------------------------------
RESULT_TERM = _rx(
    r"\b(report|result|reading|level|value|count)s?\b", r"\bhba1c\b", r"\bcholesterol\b",
    r"\b(sugar|glucose|bp|blood pressure|tsh|hemoglobin|haemoglobin|creatinine|platelet\w*)\b",
    r"\b(scan|x-?ray|ultrasound|ecg|test) (shows?|says?|showed|found)\b",
)
RESULT_INTERPRET = _rx(
    r"\b(normal|abnormal|high|low|bad|good|okay|ok|fine|elevated|borderline|range|serious|"
    r"worr\w*|concern\w*|dangerous|mean|means|meaning)\b", r"\bis (that|it|this) \w+\b",
)
ALWAYS_CLINICAL = _rx(
    r"\bdiagnos\w*", r"\b(cancer|tumou?r|diabetic coma|std|hiv|covid positive)\b",
    r"\bwhat'?s wrong with me\b", r"\bdo i have\b", r"\b(cure|remedy|remedies|treatment|treat it)\b",
    r"\bsecond opinion\b",
)

# --- Advice-seeking phrasing ---------------------------------------------------------------
ADVICE = _rx(
    r"\bis (it|that|this) (normal|bad|serious|dangerous|safe|ok|okay|fine|alright|a problem)\b",
    r"\bshould i\b", r"\bcan i (take|have|use|mix|combine|give|skip|stop|continue|eat|drink)\b",
    r"\b(safe|okay|ok|fine|alright) to\b", r"\btoo much\b", r"\bhow (much|many)\b",
    r"\bwhat (should|can) i (do|take|use|give)\b", r"\bwhich (is )?better\b", r"\b(recommend|suggest)\w*\b",
    r"\b(worried|worry)\b", r"\bwhy (do|does|am|is)\b", r"\bwhat (does|do) .{0,30}\bmean\b",
    r"\bwhat (could|might) (it|this|that) be\b", r"\bdo i need to (stop|skip|change|avoid)\b",
)

# Patient-specific context that turns a routine prep question into a clinical one.
RISK_MODIFIER = _rx(
    r"\bpregnan\w*", r"\b\d+ weeks? pregnant\b", r"\bbreast ?feed\w*", r"\bnursing (mother|my baby)\b",
    r"\b(diabetic|diabetes|sugar patient)\b", r"\b(heart|kidney|liver|thyroid) (condition|problem|disease|patient)\b",
    r"\b(asthma\w*|epilep\w*|hypertension|bp patient)\b", r"\b(after|post)[- ](my )?(surgery|operation|procedure|extraction|delivery)\b",
    r"\b(stitches|sutures)\b",
)
PREP_CONTEXT = _rx(
    r"\b(fast|fasting|eat|eating|drink|drinking|food|breakfast|water)\b", r"\bbefore (my|the)\b",
    r"\b(still|also) need\b",
)

# --- Privacy: someone else's records --------------------------------------------------------
_OTHER_PERSON = (
    r"(husband|wife|son|daughter|mother|mom|mum|father|dad|brother|sister|friend|neighbou?r|"
    r"colleague|boss|partner|relative|grandmother|grandfather|someone else|another patient|other patients?)"
)
_RECORD = r"(record|report|result|prescri\w*|diagnos\w*|history|file|test result|what (the )?doctor said)"
PRIVACY = _rx(
    rf"\b{_OTHER_PERSON}'?s?\b.{{0,40}}\b{_RECORD}",
    rf"\b{_RECORD}s?\b.{{0,30}}\b(for|of|to) (my |his |her |our )?{_OTHER_PERSON}\b",
    r"\bwhat did (dr\.?|doctor) \w+ (prescribe|say|diagnose|find)\b.{0,30}\b(for|to) (my|his|her)\b",
    r"\b(list|names|numbers?|details) of (all )?(the )?patients\b", r"\bwho (else )?(is|was) (booked|seen|coming)\b",
    r"\b(phone|mobile|contact) (number|details) of\b",
)

# --- Attempts to change the assistant's rules ----------------------------------------------
INJECTION = _rx(
    r"\b(ignore|disregard|forget)\b.{0,30}\b(instruction|rule|guideline|prompt|polic)\w*",
    r"\byou are now\b", r"\bact as (a |an |my )?(doctor|nurse|pharmacist|physician|medical)",
    r"\bpretend\b", r"\brole ?play\w*", r"\bjailbreak\w*", r"\bdeveloper mode\b", r"\bdan mode\b",
    r"\bsystem prompt\b", r"\b(reveal|show|print) (your|the) (prompt|instructions|rules)\b",
    r"\bhypothetical\w*", r"\bfor a (story|novel|poem|movie|school project|assignment)\b",
    r"\b(just )?between us\b", r"\boff the record\b", r"\bi won'?t tell\b", r"\bno one will know\b",
    r"\bin general terms\b", r"\basking for a friend\b",
)

BOOKING = _rx(
    r"\bbook\w*", r"\bappointment\w*", r"\bslots?\b", r"\breschedul\w*", r"\bcancel\w*",
    r"\b(available|availability)\b", r"\b(see|meet|consult) (a |the )?(doctor|dr)\b", r"\bschedule (a|an|my)\b",
    r"\bmy appointments?\b", r"\bfree (time|slot)\b",
)

OPT_OUT = re.compile(r"^\s*(stop|unsubscribe|opt[ -]?out|stop messages)\s*[.!]*\s*$")

_LONG_DRUGS = [d for d in DRUGS if len(d) >= 6]


def _fuzzy_drug(text: str) -> str | None:
    """Catch misspelled drug names (parasetamol, metfromin) that slip past exact matching."""
    for tok in re.findall(r"[a-z]{6,}", text):
        match = difflib.get_close_matches(tok, _LONG_DRUGS, n=1, cutoff=0.82)
        if match:
            return match[0]
    return None


def classify_rules(message: str) -> Verdict:
    plain, deleeted = normalize(message)
    views = (plain, deleeted)

    def hit(rx: re.Pattern[str]) -> str | None:
        for v in views:
            m = rx.search(v)
            if m:
                return m.group(0)
        return None

    if OPT_OUT.match(plain):
        return Verdict(Category.OPT_OUT, ["opt-out keyword"])

    if (m := hit(EMERGENCY)):
        return Verdict(Category.EMERGENCY, [f"emergency: '{m}'"])

    reasons: list[str] = []
    injection = hit(INJECTION)
    if injection:
        reasons.append(f"injection: '{injection}'")

    if (m := hit(PRIVACY)):
        return Verdict(Category.PRIVACY, reasons + [f"third-party records: '{m}'"])

    clinical: list[str] = []
    if (m := hit(MEDICATION)):
        clinical.append(f"medication: '{m}'")
    elif (drug := _fuzzy_drug(deleeted)):
        clinical.append(f"medication (fuzzy): '{drug}'")
    if (m := hit(ALWAYS_CLINICAL)):
        clinical.append(f"diagnosis/treatment: '{m}'")
    result_term, interpret = hit(RESULT_TERM), hit(RESULT_INTERPRET)
    if result_term and interpret and not hit(re.compile(r"\b(ready|collect|pick up|when will|send|sent|copy)\b")):
        clinical.append(f"result interpretation: '{result_term}' + '{interpret}'")

    symptom, advice, booking = hit(SYMPTOM), hit(ADVICE), hit(BOOKING)
    if symptom and (advice or not booking):
        clinical.append(f"symptom: '{symptom}'" + (f" + advice-seeking: '{advice}'" if advice else ""))
    risk = hit(RISK_MODIFIER)
    if risk and (advice or hit(PREP_CONTEXT)) and not (booking and not advice):
        clinical.append(f"patient-specific context: '{risk}'")

    if clinical:
        return Verdict(Category.CLINICAL, reasons + clinical)
    if injection:
        return Verdict(Category.INJECTION, reasons)
    if booking:
        return Verdict(
            Category.BOOKING,
            [f"booking: '{booking}'"] + ([f"symptom mentioned: '{symptom}'"] if symptom else []),
            staff_review=bool(symptom or risk),
        )
    return Verdict(Category.LOGISTICS, ["no clinical signal"])
