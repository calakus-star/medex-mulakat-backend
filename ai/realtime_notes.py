"""OpenAI Realtime integration notes.

This file documents the planned socket/WebRTC direction. It is not wired into
main.py yet because v1 must remain safe.

Target behavior:
- Browser streams microphone audio to OpenAI Realtime.
- AI speaks naturally.
- Candidate can interrupt; AI stops.
- No 'send' button for L2/L3.
- Backend keeps session metadata, transcript, snapshots, and report jobs.
"""
