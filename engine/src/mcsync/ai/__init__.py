"""Speech and vision analysis that runs on this computer: transcription, speakers, search and AI sync.

Models are open-weight networks run with ONNX Runtime (through sherpa-onnx): Whisper for speech recognition, Silero
for voice activity, 3D-Speaker ERes2Net for voice fingerprints. The default models ship with the app; larger ones
are downloaded on request. Nothing is sent anywhere.
"""
