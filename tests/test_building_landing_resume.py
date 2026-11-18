"""Landing handoff regressions. Synthetic receipts are not live entry evidence."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from bot.control.building_access import BuildingAccess,ExteriorPoint
from bot.control.quest_travel import TravelCommand
from bot.perception.travel_channel import TravelFlags as F
from test_building_access import setup,observe
import time


def test_mount_occlusion_dismounts_once_then_recaptures_before_allowing_travel(tmp_path,monkeypatch):
    obj,ctx,nav,events,requests,_=setup(tmp_path,monkeypatch,'unknown')
    nav=replace(nav,flags=nav.flags|F.MOUNTED,sky_dismount_key='z')
    command=observe(obj,ctx,nav)
    assert command.tap=='z' and obj.state=='dismount' and len(requests)==1
    assert obj.step(ctx,1.9,replace(nav,sequence=4),0)==TravelCommand()
    assert len(requests)==1 and not obj.points
    # A recycled navigation sequence does not confirm dismount.
    off=replace(nav,flags=nav.flags&~F.MOUNTED,sequence=3)
    assert obj.step(ctx,2.1,off,0)==TravelCommand() and obj.state=='dismount'
    off=replace(off,sequence=5)
    assert obj.step(ctx,2.2,off,0)==TravelCommand() and obj.state=='review'
    obj.step(ctx,2.3,off,0)
    obj.step(ctx,2.9,replace(off,sequence=6),0)
    assert len(requests)==2 and obj.review_context=='after_dismount'
    requests[-1].result['surface']='exterior'
    assert obj.step(ctx,3,replace(off,sequence=7),0)==TravelCommand()
    assert obj.state=='pass' and obj.points
    assert len([e for e,_ in events if e=='quest_building_dismount'])==1


def test_slow_surface_review_does_not_consume_dismount_receipt_budget(tmp_path,monkeypatch):
    obj,ctx,nav,_,requests,_=setup(tmp_path,monkeypatch)
    nav=replace(nav,flags=nav.flags|F.MOUNTED)
    obj.step(ctx,1,nav,0);obj.step(ctx,1.6,replace(nav,sequence=2),0)
    saved=requests[-1].result;requests[-1].result=None
    for i in range(3,12):
        assert obj.step(ctx,i,replace(nav,sequence=i),0)==TravelCommand()
    requests[-1].result=saved;obj.ground_only=True
    assert obj.step(ctx,12,replace(nav,sequence=12),0)==TravelCommand()
    assert obj.state=='ground_travel'
    assert obj.step(ctx,12.1,replace(nav,sequence=13),0).tap=='8'
    assert obj.state=='dismount' and obj.dismount_receipt[0]==12.1
    off=replace(nav,flags=nav.flags&~F.MOUNTED,sequence=14)
    assert obj.step(ctx,12.5,off,0)==TravelCommand() and obj.state=='ground_travel'
    obj.entry_factory=lambda *_:SimpleNamespace(close=lambda:None)
    assert obj.step(ctx,12.6,replace(off,sequence=15),0)==TravelCommand()
    assert obj.state=='entry'


def test_unclear_on_foot_changes_view_and_keeps_review_origin(tmp_path,monkeypatch):
    obj,ctx,nav,events,requests,_=setup(tmp_path,monkeypatch,'unknown')
    observe(obj,ctx,nav)
    assert obj.state=='surface_view' and len(requests)==1
    assert any(e=='quest_building_surface_reframe' for e,_ in events)
    obj.surface_view=SimpleNamespace(step=lambda *_:None)
    assert obj.step(ctx,1.8,replace(nav,sequence=4),0)==TravelCommand()
    assert obj.state=='review' and obj.surface_view_used and obj.review_context=='initial'
    obj.step(ctx,1.9,replace(nav,sequence=5),0)
    obj.step(ctx,2.5,replace(nav,sequence=6),0)
    assert len(requests)==2
    requests[-1].result['surface']='exterior'
    obj.step(ctx,2.6,replace(nav,sequence=7),0)
    assert obj.state=='pass' and obj.failures==0


def test_dismount_timeout_does_not_send_repeated_keys(tmp_path,monkeypatch):
    obj,ctx,nav,events,_,_=setup(tmp_path,monkeypatch,'unknown')
    nav=replace(nav,flags=nav.flags|F.MOUNTED)
    assert observe(obj,ctx,nav).tap=='8'
    for i in range(4):
        assert obj.step(ctx,2+i,replace(nav,sequence=4+i),0)==TravelCommand()
    assert obj.step(ctx,5.8,replace(nav,sequence=9),0).outcome=='building_dismount_unconfirmed'
    assert sum(e=='quest_building_dismount' for e,_ in events)==1


def test_airborne_or_falling_unknown_surface_cannot_authorize_dismount(tmp_path,monkeypatch):
    obj,ctx,nav,_,_,_=setup(tmp_path,monkeypatch)
    for state in (F.FLYING,F.FALLING,F.SWIMMING):
        cmd=obj._begin_dismount(ctx,1,replace(nav,flags=nav.flags|F.MOUNTED|state),'review')
        assert cmd.outcome=='building_dismount_unsafe' and cmd.tap is None


def test_roof_intent_survives_restart_on_ground_and_reaches_entry_controller(tmp_path,monkeypatch):
    obj,ctx,nav,_,requests,run=setup(tmp_path,monkeypatch,'structure')
    observe(obj,ctx,nav)
    assert obj.intents and obj.ground_only
    loaded=BuildingAccess(obj.config,capture=obj.capture,convention=obj.convention,
        request_factory=obj.request_factory,memory_path=obj.memory_path)
    # Future picture is mocked as clear ground, never passed off as a live image.
    ground=replace(nav,position=(.54,.5))
    loaded.step(ctx,2,ground,0)
    assert loaded.ground_only
    loaded.step(ctx,2.6,replace(ground,sequence=2),0)
    requests[-1].result['surface']='exterior'
    loaded.step(ctx,2.7,replace(ground,sequence=3),0)
    assert loaded.state=='ground_travel'
    run.building_access=loaded
    assert run.building_pending  # Area-marker shortcuts must stay disabled.
    created=[]
    loaded.entry_factory=lambda *args: created.append(args) or SimpleNamespace(close=lambda:None)
    assert loaded.step(ctx,2.8,replace(ground,sequence=4),0)==TravelCommand()
    assert loaded.state=='entry' and len(created)==1
    assert created[0][1].position==ground.position  # No walk toward the pin first.


def test_new_objective_does_not_inherit_old_building_intent(tmp_path,monkeypatch):
    obj,ctx,nav,_,_,_=setup(tmp_path,monkeypatch,'structure')
    observe(obj,ctx,nav)
    loaded=BuildingAccess(obj.config,capture=obj.capture,convention=obj.convention,
        request_factory=obj.request_factory,memory_path=obj.memory_path)
    loaded.step(ctx,2,replace(nav,objective_index=2),0)
    assert not loaded.ground_only


def test_corrupt_intent_image_is_not_a_roof_claim(tmp_path,monkeypatch):
    obj,ctx,nav,_,_,_=setup(tmp_path,monkeypatch,'structure')
    observe(obj,ctx,nav)
    Path(obj.intents[0]['image']).write_bytes(b'corrupt')
    loaded=BuildingAccess(obj.config,capture=obj.capture,convention=obj.convention,
        request_factory=obj.request_factory,memory_path=obj.memory_path)
    assert not loaded.intents


def test_existing_ground_memory_without_intents_is_still_readable(tmp_path,monkeypatch):
    obj,ctx,nav,_,_,_=setup(tmp_path,monkeypatch)
    observe(obj,ctx,nav)
    old=json.loads(obj.memory_path.read_text());old.pop('building_intents');old['schema']=1
    obj.memory_path.write_text(json.dumps(old))
    loaded=BuildingAccess(obj.config,capture=obj.capture,convention=obj.convention,memory_path=obj.memory_path)
    assert loaded.points and not loaded.intents


def test_roof_flight_obscured_landing_dismount_ground_and_entry_handoff(tmp_path,monkeypatch):
    obj,ctx,nav,events,requests,run=setup(tmp_path,monkeypatch,'structure')
    obj.points=[ExteriorPoint(nav.map_id,nav.world_size,(.25,.5),time.time(),'evidence','hash')]
    observe(obj,ctx,nav)
    assert obj.state=='recover'
    flight=replace(nav,flags=nav.flags|F.MOUNTED|F.FLYING,sequence=4)
    assert obj.step(ctx,1.8,flight,0).outcome is None
    landed=replace(flight,position=(.54,.5),flags=flight.flags&~F.FLYING,sequence=5)
    obj.step(ctx,2,landed,0);obj.step(ctx,2.6,replace(landed,sequence=6),0)
    requests[-1].result['surface']='unknown'
    assert obj.step(ctx,2.7,replace(landed,sequence=7),0).tap=='8'
    off=replace(landed,flags=landed.flags&~F.MOUNTED,sequence=8)
    obj.step(ctx,3.1,off,0);obj.step(ctx,3.2,replace(off,sequence=9),0)
    obj.step(ctx,3.8,replace(off,sequence=10),0)
    requests[-1].result['surface']='exterior'
    obj.step(ctx,3.9,replace(off,sequence=11),0)
    assert obj.state=='ground_travel' and obj.recovery is None
    run.building_access=obj
    assert run.building_pending and obj.ground_only
    entered=[]
    obj.entry_factory=lambda *args:entered.append(args) or SimpleNamespace(close=lambda:None)
    obj.step(ctx,4,replace(off,sequence=12),0)
    assert obj.state=='entry' and len(entered)==1
    assert entered[0][1].position==off.position
    assert any(name=='quest_building_entry_started' for name,_ in events)


def test_post_dismount_roof_is_not_accepted_as_ground(tmp_path,monkeypatch):
    obj,ctx,nav,_,requests,_=setup(tmp_path,monkeypatch,'unknown')
    nav=replace(nav,flags=nav.flags|F.MOUNTED)
    assert observe(obj,ctx,nav).tap=='8'
    off=replace(nav,flags=nav.flags&~F.MOUNTED,sequence=4)
    obj.step(ctx,2.1,off,0);obj.step(ctx,2.2,replace(off,sequence=5),0)
    obj.step(ctx,2.8,replace(off,sequence=6),0)
    requests[-1].result['surface']='structure'
    result=obj.step(ctx,2.9,replace(off,sequence=7),0)
    assert result.outcome=='building_exterior_landing_unavailable'
    assert not obj.points and obj.state!='ground_travel' and obj.ground_only
