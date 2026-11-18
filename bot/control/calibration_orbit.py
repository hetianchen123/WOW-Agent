"""Stationary camera-turn feedback and bounded keyboard recovery, without I/O."""
import math

from .navigation import heading_error
from .quest_travel import TravelCommand


class CalibrationOrbit:
    def __init__(self, now, nav, heading, pixels, convention, *, keyboard=False):
        self.started = self.last_input = now
        self.origin, self.height, self.sequence = nav.position, nav.height, nav.sequence
        self.heading = self.last_heading = heading
        self.target = (heading + math.radians(12) * pixels / 110) % math.tau
        self.convention, self.keyboard = convention, keyboard
        self.camera = (nav.camera.view, nav.camera.zoom)
        self.pulses = self.no_progress = 0
        self.last_error = None

    def step(self, ctx, now, nav, heading):
        if now - self.started > 5:
            return TravelCommand(outcome='auto_orbit_turn_timeout')
        if (nav.camera is None or getattr(nav.camera, 'paused', False)
                or (nav.camera.view, nav.camera.zoom) != self.camera):
            return TravelCommand(outcome='auto_orbit_camera_changed')
        if (math.hypot(*((b-a)*s for a,b,s in zip(self.origin, nav.position, nav.world_size))) > .08
                or abs(nav.height-self.height) > .1):
            return TravelCommand(outcome='auto_orbit_player_moved')
        yaw = heading_error(self.heading, heading)
        if abs(yaw) > math.radians(35):
            return TravelCommand(outcome='auto_orbit_turn_excessive')
        if not (self.keyboard and self.pulses == 0):
            delay = .35 if self.keyboard else .7
            if (now-self.last_input < delay or nav.sequence == self.sequence
                    or now-ctx.heading_age() < self.last_input+.2):
                return TravelCommand()
            if not self.keyboard:
                if abs(yaw) >= math.radians(4):
                    return TravelCommand(outcome='auto_orbit_ready')
                self.keyboard = True
                ctx.log_event('quest_calibration_orbit_fallback', {
                    'reason': 'mouse_turn_unconfirmed', 'yaw_deg': round(math.degrees(yaw), 2),
                    'fallback': 'verified_turn_keys', 'position': nav.position})
            else:
                moved = heading_error(self.last_heading, heading)
                if abs(moved) < math.radians(1):
                    self.no_progress += 1
                elif moved * self.last_error < 0:
                    return TravelCommand(outcome='auto_orbit_turn_direction_mismatch')
                if self.no_progress >= 2:
                    return TravelCommand(outcome='auto_orbit_turn_unconfirmed')
        error = heading_error(heading, self.target)
        if abs(error) <= math.radians(2.5) and abs(yaw) >= math.radians(4):
            return TravelCommand(outcome='auto_orbit_ready')
        if self.pulses >= 4:
            return TravelCommand(outcome='auto_orbit_turn_budget')
        key = 'left' if (error > 0) == (self.convention == 'ccw_west') else 'right'
        self.last_input, self.sequence, self.last_heading, self.last_error = now, nav.sequence, heading, error
        self.pulses += 1
        # Duration only bounds the attempt. Native yaw, then image registration,
        # must confirm its result; timing is never treated as an angle receipt.
        duration = min(.07, max(.02, abs(error)/2.8))
        ctx.log_event('quest_calibration_orbit_key', {'key': key, 'hold_s': duration,
            'attempt': self.pulses, 'remaining_deg': round(math.degrees(error), 2)})
        return TravelCommand(tap=key, hold_s=duration, precise=True)
