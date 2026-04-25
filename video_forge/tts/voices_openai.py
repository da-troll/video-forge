"""OpenAI TTS voice catalog. Lifted verbatim from voice-palette/backend/main.py."""

OPENAI_VOICES = [
    {"id": "alloy",   "name": "Alloy",   "persona": "The Narrator",    "description": "Neutral, clear, and authoritative. Versatile across all content types.", "tags": ["balanced", "clear", "versatile"], "color": "blue", "provider": "openai"},
    {"id": "ash",     "name": "Ash",     "persona": "The Anchor",      "description": "Confident and steady. A grounded, trustworthy presence.",               "tags": ["confident", "steady", "grounded"], "color": "slate", "provider": "openai"},
    {"id": "ballad",  "name": "Ballad",  "persona": "The Poet",        "description": "Melodic and flowing. Carries rhythm in every phrase.",                  "tags": ["melodic", "flowing", "lyrical"], "color": "indigo", "provider": "openai"},
    {"id": "coral",   "name": "Coral",   "persona": "The Companion",   "description": "Warm, friendly, and approachable. Like talking to a close friend.",    "tags": ["warm", "friendly", "natural"], "color": "orange", "provider": "openai"},
    {"id": "echo",    "name": "Echo",    "persona": "The Broadcaster",  "description": "Warm, resonant, and measured. Built for professional delivery.",       "tags": ["warm", "resonant", "professional"], "color": "violet", "provider": "openai"},
    {"id": "fable",   "name": "Fable",   "persona": "The Storyteller",  "description": "Expressive with a British warmth. Draws you in for the long haul.",   "tags": ["expressive", "british", "engaging"], "color": "amber", "provider": "openai"},
    {"id": "marin",   "name": "Marin",   "persona": "The Calm",        "description": "Smooth and serene. A soothing voice for focused listening.",           "tags": ["smooth", "serene", "soothing"], "color": "sky", "provider": "openai"},
    {"id": "nova",    "name": "Nova",    "persona": "The Guide",        "description": "Bright, energetic, approachable. Perfect for instructional content.",  "tags": ["bright", "energetic", "friendly"], "color": "emerald", "provider": "openai"},
    {"id": "onyx",    "name": "Onyx",    "persona": "The Authority",    "description": "Deep, powerful, commanding. Every word lands with weight.",            "tags": ["deep", "powerful", "dramatic"], "color": "zinc", "provider": "openai"},
    {"id": "sage",    "name": "Sage",    "persona": "The Scholar",      "description": "Thoughtful and precise. Measured delivery with quiet confidence.",     "tags": ["thoughtful", "precise", "calm"], "color": "teal", "provider": "openai"},
    {"id": "shimmer", "name": "Shimmer", "persona": "The Empath",       "description": "Soft, expressive, nuanced. Carries emotional depth in every line.",   "tags": ["soft", "expressive", "emotive"], "color": "rose", "provider": "openai"},
    {"id": "verse",   "name": "Verse",   "persona": "The Performer",    "description": "Dynamic and theatrical. Commands attention with flair.",               "tags": ["dynamic", "theatrical", "bold"], "color": "fuchsia", "provider": "openai"},
    {"id": "cedar",   "name": "Cedar",   "persona": "The Elder",        "description": "Rich and warm. A deep, seasoned voice with gravitas.",                "tags": ["rich", "warm", "seasoned"], "color": "stone", "provider": "openai"},
]

OPENAI_VOICE_IDS = {v["id"] for v in OPENAI_VOICES}
