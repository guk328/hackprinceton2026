"""Step 5 boundary, history and local-control regression proof."""
import unittest
from dataclasses import replace
from physics import PlantState, Command, predict, step
from physics.predict import _apply_command

def codes(state, cmd=None, horizon=0):
    result=predict(state,cmd,horizon_min=horizon)
    assert result.assessment_complete, result
    return {v.code for v in result.violations}


class RemainingLimitsTests(unittest.TestCase):
    def test_boundaries_history_and_safe_control(self):
        assert not codes(PlantState(level_pct=15, chlorine_mgl=.2))
        assert codes(PlantState(level_pct=14.99, chlorine_mgl=.199)) == {'PRESSURE_LOSS','CHLORINE_LOW'}
        for field,value in [('level_high_sp',49.9),('level_high_sp',95.1),('level_low_sp',19.9),('level_low_sp',70.1),('chlorine_target_mgl',.19),('chlorine_target_mgl',4.01),('level_low_sp',85)]:
            assert 'SETPOINT_OUT_OF_RANGE' in codes(replace(PlantState(),**{field:value}))
        for raw in (101,65535):
            result=predict(PlantState(),Command('register',0,raw),horizon_min=0)
            assert not result.safe and result.violations[0].code=='SETPOINT_OUT_OF_RANGE'
        state=PlantState(pump_on=False,auto_mode=False,elapsed_s=3600,pump_start_times_s=(1,100,200,300,400,500))
        assert not codes(state)
        assert 'PUMP_SHORT_CYCLE' in codes(state,Command('coil',0,1))
        assert _apply_command(state,Command('coil',0,1)).pump_start_times_s[-1]==3600
        assert 'PUMP_SHORT_CYCLE' not in codes(replace(state,pump_on=True,pump_start_times_s=(0,1,100,200,300,400,500)))
        assert step(replace(state,level_pct=39,auto_mode=True)).pump_start_times_s[-1]==3600
        assert step(replace(state,pump_start_times_s=(0,)),1).pump_start_times_s==()
        for history in (None,[],(float('nan'),),(3601,),(200,100)):
            result=predict(replace(state,pump_start_times_s=history),None)
            assert not result.safe and not result.assessment_complete
        safe=step(PlantState(level_pct=39,pump_on=False,safe_mode=True,level_low_sp=5,level_high_sp=10))
        assert safe.pump_on and not safe.doser_on
        assert not step(replace(safe,level_pct=86)).pump_on
        assert step(replace(safe,level_pct=60)).pump_on
        for addr in (0,1,2):
            assert _apply_command(safe,Command('coil',addr,0))==safe
        assert _apply_command(safe,Command('register',0,100))==safe
        for state,expected in [(PlantState(chlorine_mgl=.201,pump_on=False,auto_mode=False), 'CHLORINE_LOW'),(PlantState(level_pct=15.01,pump_on=False,auto_mode=False),'PRESSURE_LOSS')]:
            result=predict(state,None,horizon_min=10)
            violation=next(v for v in result.violations if v.code==expected)
            assert violation.minutes_until>0 and violation.pre_existing
