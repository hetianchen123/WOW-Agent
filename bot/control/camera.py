"""Scene camera transactions; only the addon owns zoom and relative pitch.

The camera key lease is released by normal completion, cancellation, or the existing
input emergency stop. No camera work may hold up flight or combat control.
"""
from __future__ import annotations

from ..perception.travel_channel import CameraResult, CameraScene, camera_binding_key


class CameraController:
    def __init__(self):
        self.pending = None
        self.held_key = None
        self.held_bindings = None
        self.sequence = None
        self.frame_at = float("-inf")
        self.candidate = None
        self.candidate_at = 0.0
        self.candidate_sequence = None
        self.handled = None
        self.close_latched = False
        self.close_since = None
        self.attempts = 0
        self.cooldown = float("-inf")
        self.settle_until = float("-inf")
        self.paused = False

    def forget(self):
        """Used immediately before the caller's full input release."""
        self.pending = None
        self.held_key = None
        self.held_bindings = None
        self.handled = self.candidate = None

    def cancel(self, ctx, now, reason):
        if self.pending is None:
            return
        old = self.pending
        self.pending = None
        if self.held_key is not None:
            ctx.input.up(self.held_key)
            self.held_key = None
        self.held_bindings = None
        self.cooldown = now + 5
        if reason in {"input_window_closed", "travel_input_priority"} and self.attempts < 2:
            self.handled = None
        ctx.log_event("quest_camera_interrupted", {"reason": reason, "scene": old[2].name})

    def guard(self, ctx, now, nav):
        if nav is not None and nav.sequence != self.sequence:
            self.sequence, self.frame_at = nav.sequence, now
        if nav is None or nav.camera is None or now - self.frame_at > .75:
            self.cancel(ctx, now, "telemetry_missing_or_stale")
            return False
        if self.pending and (nav.map_id, nav.quest_id) != self.pending[0]:
            self.cancel(ctx, now, "task_changed")
        if self.pending and (camera_binding_key(nav) != self.held_key
                             or getattr(nav, "camera_bindings", None) != self.held_bindings):
            self.cancel(ctx, now, "binding_changed")
        if self.pending and (now >= self.pending[3] or nav.camera.paused
                             or nav.camera.scene != self.pending[2]):
            self.cancel(ctx, now, "deadline_or_scene_changed")
        return True

    def tick(self, ctx, now, nav, *, locked=False, background=False, reason=None,
             before_start=lambda: None, after_result=lambda _: None):
        """Return True only when ground progression must wait for settling."""
        if not self.guard(ctx, now, nav):
            return False
        sample = nav.camera
        scene = sample.scene
        identity = (nav.map_id, nav.quest_id)
        if sample.paused or scene in {CameraScene.UNKNOWN, CameraScene.SWIMMING, CameraScene.GAME_CONTROL}:
            self.cancel(ctx, now, "manual_or_game_control")
            self.paused = True
            return False
        if self.paused:
            self.handled = self.candidate = None
            self.paused = False
        key = camera_binding_key(nav)
        if locked or not nav.camera_restore_available or key is None:
            self.cancel(ctx, now, "input_window_closed")
            return False
        if self.pending:
            _, old_request, requested_scene, deadline, was_background = self.pending
            if scene != requested_scene:
                self.cancel(ctx, now, "scene_changed")
                return False
            expected = (old_request + 1) % 8
            if sample.request == expected and sample.applied == requested_scene:
                if sample.result not in {CameraResult.PENDING, CameraResult.IDLE}:
                    self.pending = None
                    ctx.input.up(self.held_key)
                    self.held_key = None
                    self.held_bindings = None
                    success = sample.result == CameraResult.DISTANCE_VERIFIED
                    if not success:
                        self.close_latched = True
                    self.cooldown = now + (2 if success else 5)
                    self.settle_until = now + (.45 if not was_background else 0)
                    result = {
                        "id": nav.quest_id, "scene": scene.name, "request": expected,
                        "result": sample.result.name, "zoom": sample.zoom, "view": sample.view,
                        "calibrated": sample.calibrated, "composition_verified": False,
                    }
                    ctx.log_event("quest_camera_result", result)
                    after_result(result)
                    return not background and not was_background
            elif sample.request != old_request:
                self.cancel(ctx, now, "unexpected_receipt")
                return False
            if now >= deadline:
                self.cancel(ctx, now, "receipt_timeout")
                return False
            return not background and not was_background
        if now < self.settle_until:
            return not background
        candidate = (nav.map_id, scene)
        if candidate != self.candidate:
            self.candidate, self.candidate_at = candidate, now
            self.candidate_sequence = nav.sequence
            self.close_latched = False
            self.close_since, self.attempts = None, 0
        if not nav.camera_zoom_close:
            self.close_latched = False
            self.close_since = None
        elif self.close_since is None:
            self.close_since = now
        needs_restore = self.handled != candidate or reason is not None or (
            nav.camera_zoom_close and not self.close_latched and now - self.close_since >= .5)
        if reason in {"landing", "resurrection"} and not background:
            # A prior airborne interruption must not let NPC interaction race
            # ahead of the first settled ground view.
            self.cooldown = min(self.cooldown, now)
        if not needs_restore or now < self.cooldown:
            return False
        if now - self.candidate_at < .6 or nav.sequence == self.candidate_sequence:
            return not background
        # A failed request is handled once per scene/obstruction episode.
        # Stable close readings must never create an infinite retry loop.
        before_start()
        self.attempts += 1
        self.handled, self.close_latched = candidate, nav.camera_zoom_close
        self.pending = identity, sample.request, scene, now + 8.6, background
        ctx.log_event("quest_camera_requested", {
            "id": nav.quest_id, "scene": scene.name, "zoom": sample.zoom,
            "request": (sample.request + 1) % 8, "reason": reason or "scene_or_obstruction",
            "background": background, "key": key,
        })
        self.held_key = key
        self.held_bindings = getattr(nav, "camera_bindings", None)
        ctx.input.down(key)
        return not background
