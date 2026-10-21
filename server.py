import os, json, time, uuid, re, requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__, static_folder="static", static_url_path="")
os.makedirs("recordings", exist_ok=True)
DB = "calls.json"
KEY = os.getenv("GEMINI_API_KEY", "").strip()
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
FALLBACK = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite")
STATUSES = ["Converted", "Interested", "Follow-up Required", "Not Interested", "Demo Requested", "No Response"]

DEFAULT_PRODUCT = "FlowDesk"
DEFAULT_DETAILS = ("FlowDesk is a CRM for small businesses. Plans: Starter Rs 499/user/month, Pro Rs 999/user/month, "
                   "14-day free trial, free demo available. Features: lead tracking, WhatsApp/email automation, reports, mobile app.")

SYSTEM = """You are Riya, a friendly, smart voice assistant who also works as a sales agent for {product}, a SaaS product.
Product details given by the business: {details}
Use only these details for facts such as pricing and features. If something is not listed, do not invent it; say you will check and offer a free demo or a follow-up instead.
You are on a VOICE call: speak naturally, no markdown, no lists, no bullet points.
Reply ONLY in this language: {lang}.
Rules:
- ALWAYS answer exactly what the customer asked, first and directly, on ANY topic (for example AI, machine learning, business, technology, general knowledge). Never refuse a harmless question and never dodge it to pitch the product.
- For simple questions reply in 1-3 short sentences. For explanations, use at most 4 short spoken sentences with a simple example.
- Mention {product} only when it is relevant to what the customer said or asked about the product, price, features or demo. Do not force a sales pitch into every reply.
- Never repeat the name {product} in every reply. The customer already knows it from your greeting.
- If the customer shows interest, or the topic connects to their business, you may end with one short, soft question about their needs or offer a free trial or demo. Ask at most one question at a time.
- If you are not sure about a fact, say so honestly instead of guessing."""

def gemini(system, contents, as_json=False):
    body = {"system_instruction": {"parts": [{"text": system}]}, "contents": contents}
    if as_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    last = None
    for model in dict.fromkeys([MODEL, FALLBACK]):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            r = requests.post(url, json=body, headers={"x-goog-api-key": KEY}, timeout=40)
            if r.ok:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            last = f"HTTP {r.status_code} (model={model}): {r.text[:300]}"
            print("Gemini error:", last)
            if r.status_code not in (429, 500, 503):
                break
            time.sleep(2)
    raise RuntimeError(last)

def rule_reply(text, lang, product):
    t = text.lower()
    hi = lang.startswith("hi")
    if re.search(r"demo|डेमो", t):
        return "Zaroor! Main aapke liye free demo book kar deti hoon. Aapko kaunsa time suit karega?" if hi else "Sure! I can book a free demo for you. What time works best?"
    if re.search(r"not interested|no thanks|nahi chahiye|नहीं", t):
        return "Koi baat nahi, aapke time ke liye shukriya. Zarurat ho to zaroor bataiye." if hi else "No problem, thanks for your time. Reach out anytime you need us."
    if re.search(r"price|cost|kitn|rate|कीमत|कितना|feature|kya karta|what does|क्या", t):
        return f"{product} ke price aur features main free demo me dikha sakti hoon. Aapko sabse zyada kis cheez ki zarurat hai?" if hi else f"I can walk you through {product}'s pricing and features in a free demo. What do you need most?"
    return "Achha, aur batayiye, aapki sabse badi problem kya hai?" if hi else "Got it. Could you tell me more about what you need?"

def heuristic_status(user_texts):
    if not user_texts: return "No Response"
    t = " ".join(user_texts).lower()
    if re.search(r"not interested|no thanks|nahi chahiye|नहीं चाहिए", t): return "Not Interested"
    if re.search(r"demo|डेमो", t): return "Demo Requested"
    if re.search(r"i'll buy|sign me up|let's do it|go ahead|lena hai|le lunga|kharid|ok done|book it", t): return "Converted"
    if re.search(r"later|call back|think|baad me|sochna", t): return "Follow-up Required"
    return "Interested"

def load():
    return json.load(open(DB, encoding="utf-8")) if os.path.exists(DB) else []

@app.get("/")
def index():
    return send_from_directory("static", "index.html")

@app.post("/api/chat")
def chat():
    d = request.json
    msgs, lang = d["messages"], d.get("lang", "en-IN")
    product = (d.get("product") or DEFAULT_PRODUCT).strip()[:80]
    details = (d.get("details") or DEFAULT_DETAILS).strip()[:1500]
    if KEY:
        try:
            lname = {"en": "English", "hi": "Hindi (Devanagari)", "te": "Telugu"}[lang[:2]]
            contents = [{"role": "user", "parts": [{"text": "(call started)"}]}]
            for m in msgs:
                contents.append({"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["text"]}]})
            system = SYSTEM.format(product=product, details=details, lang=lname)
            return jsonify(reply=gemini(system, contents).strip())
        except Exception as e:
            print("Gemini error:", e)
    return jsonify(reply=rule_reply(msgs[-1]["text"], lang, product))

@app.post("/api/finish")
def finish():
    transcript = json.loads(request.form["transcript"])
    duration = int(request.form.get("duration", 0))
    product = (request.form.get("product") or DEFAULT_PRODUCT).strip()[:80]
    cid = uuid.uuid4().hex[:10]
    audio = request.files.get("audio")
    rec = None
    if audio:
        rec = f"{cid}.webm"
        audio.save(os.path.join("recordings", rec))
    user_texts = [m["text"] for m in transcript if m["role"] == "user"]
    info = None
    if KEY and user_texts:
        try:
            convo = "\n".join(f"{m['role']}: {m['text']}" for m in transcript)
            prompt = (f"Analyse this sales call for the product '{product}'. Return JSON with keys: summary (2-3 sentences), "
                      f"requirements (string), objections (string), next_steps (string), status (one of {STATUSES}).\n\n{convo}")
            info = json.loads(gemini("You are a sales call analyst. Answer in English.", [{"role": "user", "parts": [{"text": prompt}]}], True))
            if info.get("status") not in STATUSES: info["status"] = heuristic_status(user_texts)
        except Exception as e:
            print("Summary error:", e)
    if not info:
        info = {"summary": f"Call lasted {duration}s with {len(user_texts)} customer message(s). Customer said: " + (" / ".join(user_texts)[:200] or "nothing."),
                "requirements": "See transcript.", "objections": "See transcript.",
                "next_steps": "Follow up with the customer." if user_texts else "Retry the call.",
                "status": heuristic_status(user_texts)}
    call = {"id": cid, "time": time.strftime("%d %b %Y, %I:%M %p"), "duration": duration, "product": product,
            "recording": rec, "transcript": transcript, **info}
    db = load(); db.insert(0, call)
    json.dump(db, open(DB, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return jsonify(call)

@app.get("/api/calls")
def calls():
    return jsonify(load())

@app.get("/recordings/<f>")
def recordings(f):
    return send_from_directory("recordings", f)

if __name__ == "__main__":
    print("AI mode:", "Gemini" if KEY else "Demo mode (no API key)", "| model:", MODEL)
    app.run(port=5000, debug=False)
