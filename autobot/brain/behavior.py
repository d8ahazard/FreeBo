"""Behavior controller — decides WHETHER the robot should roam, and why.

The old loop drove every tick (chaotic, backs into things). Instead, each reason cycle this picks a movement
SCOPE that the safety floor hard-enforces, plus an INTENT that shapes the prompt:

  * roam   — may drive across the room (explore / greet / patrol / pursue / return).
  * adjust — may only rotate in place to look/track (observe / converse). No translation.
  * hold   — no motion at all (stopped / resting / asleep).

Default is OBSERVE (adjust): the robot mostly watches and comments. Roaming only unlocks for a reason:
  - it sees a person -> GREET (approach + say hi),
  - it's been idle a while -> PATROL (a short look around the house for anything noteworthy),
  - it's in command/conversational mode, or
  - a voice order set an override (go explore / come here / go home / stop).

`mode == explore` is the "alive at home" companion mode and runs this whole state machine; it does NOT mean
"drive constantly". Voice "go explore" sets a stronger, time-boxed ACTIVE-explore override that does roam.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

ROAM, ADJUST, HOLD = "roam", "adjust", "hold"


@dataclass
class Behavior:
    scope: str
    intent: str
    detail: str = ""


class BehaviorController:
    def __init__(self) -> None:
        self.idle_patrol_seconds = float(os.environ.get("AUTOBOT_IDLE_PATROL_SECONDS", "180"))
        self.patrol_duration = float(os.environ.get("AUTOBOT_PATROL_SECONDS", "30"))
        self.greet_seconds = float(os.environ.get("AUTOBOT_GREET_SECONDS", "12"))
        now = time.time()
        self._last_activity = now           # last speech/touch/command — resets the idle-patrol timer
        self._last_patrol = 0.0
        self._patrol_until = 0.0
        self._greet_until = 0.0
        self._greet_name = ""
        self._voice_intent: str | None = None   # "stopped"|"explore"|"pursue"|"return"
        self._voice_until = 0.0
        self._voice_detail = ""
        self.current = Behavior(ADJUST, "observe")

    # --- external signals ---
    def note_activity(self) -> None:
        self._last_activity = time.time()

    def set_voice_intent(self, intent: str, seconds: float = 90.0, detail: str = "") -> None:
        """A spoken order sets a time-boxed behavior override (cleared by 'stop' or expiry)."""
        self._voice_intent = intent
        self._voice_until = time.time() + seconds
        self._voice_detail = detail
        self.note_activity()

    def clear_voice_intent(self) -> None:
        self._voice_intent = None
        self._voice_until = 0.0

    def trigger_greet(self, name: str) -> None:
        self._greet_until = time.time() + self.greet_seconds
        self._greet_name = name or "someone"
        self.note_activity()

    # --- the decision tree ---
    def decide(self, s, *, resting: bool, present_people: list[str], owner_name: str = "") -> Behavior:
        now = time.time()
        voice = self._voice_intent if now < self._voice_until else None
        if voice is None:
            self._voice_intent = None

        if getattr(s, "asleep", False):
            return self._set(HOLD, "asleep")
        if resting:
            return self._set(HOLD, "resting")
        if voice == "stopped":
            return self._set(HOLD, "stopped", "you were told to stop")
        if voice == "explore":
            return self._set(ROAM, "explore_active")
        if voice == "pursue":
            return self._set(ROAM, "pursue", self._voice_detail)
        if voice == "return":
            return self._set(ROAM, "return")

        mode = getattr(s, "mode", "explore")
        if mode == "command" and (getattr(s, "directive", "") or "").strip():
            return self._set(ROAM, "pursue", s.directive.strip())
        if mode == "conversational":
            return self._set(ADJUST, "converse")

        # --- explore = companion behavior (observe by default; roam only for a reason) ---
        if now < self._patrol_until:
            return self._set(ROAM, "patrol")
        if present_people:
            who = present_people[0]
            if now >= self._greet_until and who != self._greet_name:
                self.trigger_greet(who)            # a (new) person showed up -> greet them
            if now < self._greet_until:
                return self._set(ROAM, "greet", self._greet_name)
        else:
            self._greet_name = ""                  # they left; allow greeting them again later
        idle = now - self._last_activity
        if idle > self.idle_patrol_seconds and (now - self._last_patrol) > self.idle_patrol_seconds:
            self._last_patrol = now
            self._patrol_until = now + self.patrol_duration
            return self._set(ROAM, "patrol")
        return self._set(ADJUST, "observe")

    def _set(self, scope: str, intent: str, detail: str = "") -> Behavior:
        self.current = Behavior(scope, intent, detail)
        return self.current

    # --- prompt + status ---
    def prompt_block(self, robot_name: str = "your") -> str:
        b = self.current
        name = self._greet_name or "someone"
        texts = {
            "observe": ("RIGHT NOW: You are OBSERVING from where you are. STAY PUT — you may turn in place to "
                        "look around, but do NOT drive across the room. Watch your surroundings and call out "
                        "anything new or noteworthy (a person or pet, an open door/window, a spill or mess, "
                        "anything unusual); `remember` it and `send_alert` if it matters."),
            "greet": (f"RIGHT NOW: You see {name}! Go to them — drive toward them and greet them warmly by "
                      "name. Keep them in view."),
            "patrol": ("RIGHT NOW: IDLE PATROL — take a short roam around to check the area for anything I "
                       "should know about (open doors/windows, spills/messes, people or pets, anything out of "
                       "place). Note and alert what you find, then settle down."),
            "explore_active": ("RIGHT NOW: EXPLORE — actively roam and cover new ground. Head toward open "
                               "space and doorways, look around, and describe what you find."),
            "pursue": (f"RIGHT NOW: Pursue your directive: \"{b.detail}\". Find the target by turning to scan, "
                       "then drive toward it and keep it in view."),
            "converse": ("RIGHT NOW: Stay where you are and converse. You may only turn in place to keep the "
                         "person you're talking with centered — do not drive around."),
            "return": ("RIGHT NOW: Head home — `dock` / go to your charger. Drive gently toward it and verify."),
            "stopped": ("RIGHT NOW: HOLD STILL — you were told to stop moving. Do not drive at all until asked "
                        "to move again. You may still look and talk."),
            "resting": ("RIGHT NOW: You are charging/docked/resting — do not drive. Just observe or chat."),
            "asleep": "RIGHT NOW: You are asleep.",
        }
        return texts.get(b.intent, texts["observe"])

    def state(self) -> dict:
        b = self.current
        return {"scope": b.scope, "intent": b.intent, "detail": b.detail,
                "voice_intent": self._voice_intent,
                "idle_s": round(time.time() - self._last_activity, 1)}
