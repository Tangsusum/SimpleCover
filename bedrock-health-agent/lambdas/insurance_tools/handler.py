"""
Bedrock Agent action-group Lambda for the SimpleCover health-insurance demo.

Implements four MOCK tools:
  - get_quotes
  - estimate_rebate_and_lhc
  - schedule_callback
  - process_uploaded_document

All data is fictional/illustrative. No external APIs are called. The document
tool uses lightweight heuristics (keyword scoring + regex) rather than real OCR
or an LLM call, so the Lambda is fully self-contained and needs no extra IAM.

Bedrock invokes this with a "function schema" event and expects a specific
response envelope; see _respond().
"""
import json
import re

# ---------------------------------------------------------------------------
# Mock product catalogue (kept consistent with the KB docs in ../../kb_docs)
# ---------------------------------------------------------------------------
CATALOGUE = [
    {
        "fund": "HealthFirst",
        "product": "Basic Hospital Saver",
        "tier": "Basic",
        "cover_type": "hospital",
        "monthly_premium": 95,
        "states": ["NSW", "VIC", "QLD"],
        "key_inclusions": ["restricted rehab", "restricted psychiatric"],
        "notable_exclusions": ["joint reconstruction", "pregnancy", "extras"],
        "extras_strength": 0,
        "phis": "https://example-phis.demo/healthfirst/basic-hospital-saver",
        "tradeoff": "Cheapest way to avoid MLS/LHC, but hospital-only and no joint reconstruction or pregnancy cover.",
    },
    {
        "fund": "MediSure",
        "product": "Bronze Secure Hospital",
        "tier": "Bronze",
        "cover_type": "hospital",
        "monthly_premium": 120,
        "states": ["NSW", "VIC", "QLD", "WA", "SA"],
        "key_inclusions": ["dental surgery", "hernia/appendix", "endoscopy"],
        "notable_exclusions": ["joint reconstruction", "cataracts", "extras"],
        "extras_strength": 0,
        "phis": "https://example-phis.demo/medisure/bronze-secure",
        "tradeoff": "Covers common lower-cost procedures, but still no joint reconstruction and no extras.",
    },
    {
        "fund": "HealthFirst",
        "product": "Silver Plus Hospital & Extras",
        "tier": "Silver",
        "cover_type": "both",
        "monthly_premium": 165,
        "states": ["NSW", "VIC", "QLD", "SA"],
        "key_inclusions": ["joint reconstruction", "cataracts", "basic extras"],
        "notable_exclusions": ["pregnancy", "major dental"],
        "extras_strength": 2,
        "phis": "https://example-phis.demo/healthfirst/silver-plus",
        "tradeoff": "Good all-rounder with joint reconstruction, but only modest extras limits and no pregnancy cover.",
    },
    {
        "fund": "SunWell",
        "product": "Silver Active Hospital & Extras",
        "tier": "Silver",
        "cover_type": "both",
        "monthly_premium": 185,
        "states": ["NSW", "VIC", "QLD", "WA"],
        "key_inclusions": ["joint reconstruction", "cataracts", "strong dental/physio extras"],
        "notable_exclusions": ["pregnancy", "comprehensive psychiatric"],
        "extras_strength": 3,
        "phis": "https://example-phis.demo/sunwell/silver-active",
        "tradeoff": "Strong everyday extras (dental/physio) on top of Silver hospital, but pricier than plain Silver and no pregnancy cover.",
    },
    {
        "fund": "MediSure",
        "product": "Gold Complete Hospital & Extras",
        "tier": "Gold",
        "cover_type": "both",
        "monthly_premium": 310,
        "states": ["NSW", "VIC", "QLD", "WA", "SA", "ACT", "TAS"],
        "key_inclusions": ["pregnancy", "joint replacement", "comprehensive psychiatric", "comprehensive extras"],
        "notable_exclusions": [],
        "extras_strength": 4,
        "phis": "https://example-phis.demo/medisure/gold-complete",
        "tradeoff": "Most comprehensive incl. pregnancy and comprehensive psychiatric, but the most expensive by a wide margin.",
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
def get_quotes(state="", cover_type="", priorities="", budget_max=0):
    """Return a small ranked list of mock quotes for the customer's needs."""
    state = (state or "").strip().upper()
    cover_type = (cover_type or "").strip().lower()
    prio = (priorities or "").lower()
    try:
        budget_max = float(budget_max or 0)
    except (TypeError, ValueError):
        budget_max = 0

    scored = []
    for p in CATALOGUE:
        score = 0.0
        # availability
        if state and state not in p["states"]:
            continue  # not available in the customer's state
        # cover type match
        if cover_type in ("hospital", "extras", "both"):
            if cover_type == "both" and p["cover_type"] == "both":
                score += 3
            elif cover_type == p["cover_type"]:
                score += 3
            elif cover_type == "extras" and p["extras_strength"] > 0:
                score += 2
            elif cover_type == "hospital" and p["cover_type"] in ("hospital", "both"):
                score += 2
        # budget
        if budget_max:
            if p["monthly_premium"] <= budget_max:
                score += 2
            else:
                score -= 2  # over budget, penalise but still show
        # keyword priorities -> reward matching inclusions.
        def has_any(*words):
            return any(w in prio for w in words)

        incl = " ".join(p["key_inclusions"]).lower()
        if has_any("entry", "basic", "minimum"):
            score += 3 if p["tier"] in ("Basic", "Bronze") else -1
        if has_any("mls", "avoid the tax", "surcharge"):
            score += 2 if p["cover_type"] in ("hospital", "both") else 0
        # extras interest counted ONCE (not once per keyword) so it can't swamp price
        if has_any("dental", "physio", "optical", "extras", "ancillary"):
            score += p["extras_strength"]
        if has_any("silver"):  # they want Silver-equivalent, not an upsell
            score += 3 if p["tier"] == "Silver" else (-2 if p["tier"] == "Gold" else 0)
        if has_any("joint"):
            score += 3 if "joint" in incl else -1
        if has_any("pregnan"):
            score += 5 if "pregnancy" in incl else -4
        if has_any("psychiat", "mental health"):
            score += 5 if "comprehensive psychiatric" in incl else -2
        # explicit price-sensitivity: penalise proportional to premium
        if has_any("cheap", "cheaper", "affordable", "budget", "save", "value", "better price"):
            score -= p["monthly_premium"] / 50.0
        # slight preference for cheaper when scores otherwise tie
        score -= p["monthly_premium"] / 1000.0
        scored.append((score, p))

    scored.sort(key=lambda t: t[0], reverse=True)
    top = scored[:3] if scored else []

    quotes = []
    for _, p in top:
        quotes.append({
            "fund": p["fund"],
            "product": p["product"],
            "tier": p["tier"],
            "cover_type": p["cover_type"],
            "monthly_premium_aud": p["monthly_premium"],
            "key_inclusions": p["key_inclusions"],
            "notable_exclusions": p["notable_exclusions"],
            "phis_url": p["phis"],
            "tradeoff_note": p["tradeoff"],
        })

    return {
        "disclaimer": "MOCK quotes for demonstration only — not real products or prices.",
        "matched_count": len(quotes),
        "state_filter": state or "any",
        "quotes": quotes,
        "note": "No matches available in that state." if state and not quotes else "",
    }


def estimate_rebate_and_lhc(age=0, income=0, dependents=0):
    """Very rough, clearly-labelled MOCK rebate + LHC loading estimate."""
    try:
        age = int(age or 0)
        income = float(income or 0)
        dependents = int(dependents or 0)
    except (TypeError, ValueError):
        return {"error": "age, income and dependents must be numeric."}

    # Mock, income-tested rebate (single thresholds, illustrative only).
    if income <= 97000:
        base_rebate = 24.6
    elif income <= 113000:
        base_rebate = 16.4
    elif income <= 151000:
        base_rebate = 8.2
    else:
        base_rebate = 0.0
    # small age uplift, illustrative
    if age >= 70:
        base_rebate = min(base_rebate + 8.2, 32.8) if base_rebate else 0
    elif age >= 65:
        base_rebate = min(base_rebate + 4.1, 28.7) if base_rebate else 0

    # Mock LHC loading: 2% per year over age 31, capped at 70%.
    lhc_loading = min(max(0, age - 31) * 2, 70)

    # Mock MLS flag: higher earners without hospital cover would face MLS.
    mls_applies_without_cover = income > 97000

    return {
        "disclaimer": "MOCK estimate — illustrative only, NOT actuarially accurate. "
                      "Customer must confirm actual entitlement.",
        "inputs": {"age": age, "income": income, "dependents": dependents},
        "estimated_rebate_percent": round(base_rebate, 1),
        "estimated_lhc_loading_percent": lhc_loading,
        "mls_likely_if_no_hospital_cover": mls_applies_without_cover,
        "explanation": (
            f"At an income around ${income:,.0f}, the illustrative rebate is "
            f"~{round(base_rebate,1)}%. Because the customer is {age}, an "
            f"illustrative LHC loading of {lhc_loading}% would apply if taking out "
            f"hospital cover for the first time now."
        ),
    }


def schedule_callback(reason="", customer_id="unknown"):
    """Return a fake callback confirmation."""
    reason = (reason or "unspecified").strip()
    customer_id = (customer_id or "unknown").strip()
    # Deterministic-ish fake id from the inputs (no randomness needed for a demo).
    conf = "CB-" + format(abs(hash((reason, customer_id))) % 1_000_000, "06d")
    return {
        "disclaimer": "MOCK callback — no real booking was made.",
        "confirmation_id": conf,
        "scheduled_for": "tomorrow between 10:00–11:00 (local time)",
        "customer_id": customer_id,
        "reason_logged": reason,
        "message": "A human consultant will call the customer back at the time above.",
    }


# Keyword sets for the (mock) document classifier.
_HEALTH_TERMS = [
    "health insurance", "hospital cover", "extras", "ancillary", "premium",
    "waiting period", "phis", "private health", "rebate", "medicare levy",
    "lifetime health cover", "lhc", "policy", "fund", "excess", "tier",
    "bronze", "silver", "gold", "basic hospital", "joint reconstruction",
]


def process_uploaded_document(document_text=""):
    """Heuristically classify + extract fields from pasted document text."""
    text = document_text or ""
    low = text.lower()

    hits = [t for t in _HEALTH_TERMS if t in low]
    is_health = len(hits) >= 2  # needs a couple of signals to count

    if not is_health:
        return {
            "is_health_insurance_document": False,
            "rejection_reason": (
                "The text doesn't contain enough health-insurance signals "
                "(e.g. hospital cover, extras, premium, waiting period, tier, "
                "PHIS). It looks like it might be a different kind of document."
            ),
            "matched_terms": hits,
        }

    # Field extraction (best-effort regex over free text).
    def first(pattern, group=1, flags=re.I):
        m = re.search(pattern, text, flags)
        return m.group(group).strip() if m else None

    known_funds = ["HealthFirst", "MediSure", "SunWell"]
    fund = next((f for f in known_funds if f.lower() in low), None) \
        or first(r"(?:fund|insurer)\s*[:\-]\s*([A-Za-z0-9 &]+)")

    tier = first(r"\b(Basic|Bronze|Silver|Gold)\b")
    premium = first(r"\$\s?([0-9]+(?:\.[0-9]{2})?)\s*(?:/|per\s*)?(?:month|mo|pm)")
    excess = first(r"excess\s*[:\-]?\s*\$?\s?([0-9]+)")
    if excess is None:
        excess = first(r"\$\s?([0-9]+)\s*(?:per admission|excess)")
    cover_type = None
    if "extras" in low and ("hospital" in low):
        cover_type = "both"
    elif "hospital" in low:
        cover_type = "hospital"
    elif "extras" in low or "ancillary" in low:
        cover_type = "extras"
    state = first(r"\b(NSW|VIC|QLD|WA|SA|ACT|TAS)\b")

    extracted = {
        "fund": fund,
        "product": first(r"(?:product|plan|policy name)\s*[:\-]\s*([A-Za-z0-9 &]+)"),
        "tier": tier,
        "cover_type": cover_type,
        "monthly_premium_aud": float(premium) if premium else None,
        "excess_aud": int(excess) if excess and excess.isdigit() else None,
        "state": state,
    }
    return {
        "is_health_insurance_document": True,
        "matched_terms": hits[:8],
        "extracted_fields": {k: v for k, v in extracted.items() if v is not None},
        "note": "Fields extracted heuristically from pasted text (mock, no OCR).",
    }


# ---------------------------------------------------------------------------
# Bedrock Agent plumbing
# ---------------------------------------------------------------------------
DISPATCH = {
    "get_quotes": get_quotes,
    "estimate_rebate_and_lhc": estimate_rebate_and_lhc,
    "schedule_callback": schedule_callback,
    "process_uploaded_document": process_uploaded_document,
}

# Coerce Bedrock's string parameter values to the type the schema declares.
_CASTS = {"number": float, "integer": int, "boolean": lambda v: str(v).lower() == "true"}


def _params_to_kwargs(parameters):
    kwargs = {}
    for p in parameters or []:
        name = p.get("name")
        value = p.get("value")
        cast = _CASTS.get(p.get("type"))
        if cast and value not in (None, ""):
            try:
                value = cast(value)
            except (TypeError, ValueError):
                pass
        kwargs[name] = value
    return kwargs


def _respond(event, payload):
    """Wrap a python result in the envelope Bedrock Agents expect."""
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup"),
            "function": event.get("function"),
            "functionResponse": {
                "responseBody": {"TEXT": {"body": json.dumps(payload)}}
            },
        },
        # Echo session state back so it persists within the session.
        "sessionAttributes": event.get("sessionAttributes", {}) or {},
        "promptSessionAttributes": event.get("promptSessionAttributes", {}) or {},
    }


def lambda_handler(event, context):
    print("BEDROCK_EVENT:", json.dumps(event)[:2000])
    function = event.get("function")
    fn = DISPATCH.get(function)
    if not fn:
        return _respond(event, {"error": f"Unknown function '{function}'."})
    kwargs = _params_to_kwargs(event.get("parameters"))
    try:
        # Only pass kwargs the target function actually accepts.
        import inspect
        allowed = set(inspect.signature(fn).parameters)
        result = fn(**{k: v for k, v in kwargs.items() if k in allowed})
    except Exception as exc:  # noqa: BLE001 - demo: never hard-fail the agent
        result = {"error": f"Tool '{function}' failed: {exc}"}
    return _respond(event, result)
