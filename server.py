import os, json, time, uuid, re, requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__, static_folder="static", static_url_path="")
os.makedirs("recordings", exist_ok=True)
DB = "calls.json"
KEY = os.getenv("GEMINI_API_KEY", "").strip()
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
STATUSES = ["Converted", "Interested", "Follow-up Required", "Not Interested", "Demo Requested", "No Response"]

SYSTEM = """You are Riya, a friendly, smart voice assistant who also works as a sales agent for FlowDesk, a SaaS CRM for small businesses.
Plans: Starter Rs 499/user/month, Pro Rs 999/user/month, 14-day free trial, free demo available.
Features: lead tracking, WhatsApp/email automation, reports, mobile app.
You are on a VOICE call: speak naturally, no markdown, no lists, no bullet points.
Reply ONLY in this language: {lang}.
Rules:
- ALWAYS answer exactly what the customer asked, first and directly, on ANY topic (for example AI, machine learning, business, technology, general knowledge). Never refuse a harmless question and never dodge it to pitch the product.
- For simple questions reply in 1-3 short sentences. For explanations, use at most 4 short spoken sentences with a simple example.
- Mention FlowDesk only when it is relevant to what the customer said or asked about our product, price, features or demo. Do not force a sales pitch into every reply.
- Never repeat the name FlowDesk in every reply. The customer already knows it from your greeting.
- If the customer shows interest in a CRM, or the topic connects to their business, you may end with one short, soft question about their needs or offer a free trial or demo. Ask at most one question at a time.
- If you are not sure about a fact, say so honestly instead of guessing."""

FALLBACK = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite")

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

def rule_reply(text, lang):
    t = text.lower()
    hi = lang.startswith("hi")
    if re.search(r"price|cost|kitn|rate|कीमत|कितना", t):
        return "Starter plan 499 rupaye per user per month hai aur Pro 999. 14 din ka free trial bhi hai. Aapki team me kitne log hain?" if hi else "Our Starter plan is 499 rupees per user per month and Pro is 999, with a 14-day free trial. How many people are on your team?"
    if re.search(r"demo|डेमो", t):
        return "Zaroor! Main aapke liye free demo book kar deti hoon. Aapko kaunsa time suit karega?" if hi else "Sure! I can book a free demo for you. What time works best?"
    if re.search(r"feature|kya karta|what does|क्या", t):
        return "FlowDesk lead tracking, WhatsApp automation aur reports deta hai. Aap abhi leads kaise manage karte hain?" if hi else "FlowDesk gives you lead tracking, WhatsApp and email automation, and reports. How do you manage leads today?"
    if re.search(r"not interested|no thanks|nahi chahiye|नहीं", t):
        return "Koi baat nahi, aapke time ke liye shukriya. Zarurat ho to zaroor bataiye." if hi else "No problem, thanks for your time. Reach out anytime you need us."
    return "Achha, aur batayiye, aapki sabse badi problem kya hai?" if hi else "Got it. Could you tell me more about what you need from a CRM?"

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
    if KEY:
        try:
            lname = {"en": "English", "hi": "Hindi (Devanagari)", "te": "Telugu"}[lang[:2]]
            contents = [{"role": "user", "parts": [{"text": "(call started)"}]}]
            for m in msgs:
                contents.append({"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["text"]}]})
            return jsonify(reply=gemini(SYSTEM.format(lang=lname), contents).strip())
        except Exception as e:
            print("Gemini error:", e)
    return jsonify(reply=rule_reply(msgs[-1]["text"], lang))

@app.post("/api/finish")
def finish():
    transcript = json.loads(request.form["transcript"])
    duration = int(request.form.get("duration", 0))
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
            prompt = ("Analyse this sales call. Return JSON with keys: summary (2-3 sentences), requirements (string), "
                      f"objections (string), next_steps (string), status (one of {STATUSES}).\n\n{convo}")
            info = json.loads(gemini("You are a sales call analyst. Answer in English.", [{"role": "user", "parts": [{"text": prompt}]}], True))
            if info.get("status") not in STATUSES: info["status"] = heuristic_status(user_texts)
        except Exception as e:
            print("Summary error:", e)
    if not info:
        info = {"summary": f"Call lasted {duration}s with {len(user_texts)} customer message(s). Customer said: " + (" / ".join(user_texts)[:200] or "nothing."),
                "requirements": "See transcript.", "objections": "See transcript.",
                "next_steps": "Follow up with the customer." if user_texts else "Retry the call.",
                "status": heuristic_status(user_texts)}
    call = {"id": cid, "time": time.strftime("%d %b %Y, %I:%M %p"), "duration": duration,
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
    if KEY:
        try:
            print("Gemini TEST OK:", gemini("Reply briefly.", [{"role": "user", "parts": [{"text": "Say hi"}]}])[:60])
        except Exception as e:
            print("Gemini TEST FAILED ->", e)
    app.run(port=5000, debug=False)