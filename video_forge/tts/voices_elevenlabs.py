"""ElevenLabs TTS voice catalog — minimal for tonight.

# TODO phase 2: pull the full ElevenLabs voice library (1000+) via their /v1/voices
# endpoint, cache locally, expose with persona/tag metadata equivalent to the
# OpenAI/Gemini lists. Tonight: 2 voices, both ElevenLabs defaults, enough to
# prove the third synth branch wires up without bloating the catalog.
"""

ELEVENLABS_VOICES = [
    {
        "id": "21m00Tcm4TlvDq8ikWAM",  # Rachel — ElevenLabs default narrator
        "name": "Rachel",
        "persona": "The Narrator",
        "description": "Calm, narrator-friendly female voice. ElevenLabs default for long-form.",
        "tags": ["calm", "narrative", "default"],
        "color": "blue",
        "provider": "elevenlabs",
    },
    {
        "id": "29vD33N1CtxCmqQRPOHJ",  # Drew — ElevenLabs default male
        "name": "Drew",
        "persona": "The Anchor",
        "description": "Well-rounded male voice. ElevenLabs default for confident delivery.",
        "tags": ["confident", "broadcast", "default"],
        "color": "slate",
        "provider": "elevenlabs",
    },
]

ELEVENLABS_VOICE_IDS = {v["id"] for v in ELEVENLABS_VOICES}
