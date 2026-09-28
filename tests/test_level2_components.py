import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.request_manager import EdgeRequestManager, EdgeRequest
from core.kv_policies.async_evacuation import AsyncShadowEvacuator, KVAllocation
from core.thermal_token_bucket import ThermalTokenBucket
from core.safety_fallback import SafetyFallback

class TestEdgeRequestManager(unittest.TestCase):
    def test_prioritizes_bursty_requests(self):
        manager = EdgeRequestManager(max_batch_size=2, burst_threshold_tokens=100)
        req1 = EdgeRequest(request_id="1", arrival_time_s=0.1, prompt_tokens=200) # not bursty
        req2 = EdgeRequest(request_id="2", arrival_time_s=0.2, prompt_tokens=50)  # bursty
        
        manager.submit_request(req1)
        manager.submit_request(req2)
        
        batch = manager.get_next_batch()
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[0].request_id, "2") # bursty request should be first

class TestAsyncShadowEvacuator(unittest.TestCase):
    def test_closed_allocation_eviction_safety(self):
        evacuator = AsyncShadowEvacuator()
        
        # open/active allocation
        alloc1 = KVAllocation(page_id=1, is_open=True)
        # closed allocation
        alloc2 = KVAllocation(page_id=2, is_open=False)
        
        evacuator.register_allocation(alloc1)
        evacuator.register_allocation(alloc2)
        
        evicted = evacuator.mark_and_evacuate([1, 2])
        
        # alloc1 should NOT be evicted because it is open
        self.assertNotIn(1, evicted)
        self.assertIn(2, evicted)
        
        evacuator.commit_dma_transfers()
        self.assertFalse(alloc1.is_evicted)
        self.assertTrue(alloc2.is_evicted)

class TestThermalTokenBucket(unittest.TestCase):
    def test_thermal_throttling(self):
        bucket = ThermalTokenBucket(max_tokens=100.0, replenish_rate_hz=10.0)
        bucket.tokens = 0.0
        
        # High headroom -> normal replenish
        bucket.step(dt_s=1.0, thermal_headroom_c=20.0)
        self.assertAlmostEqual(bucket.tokens, 10.0)
        
        # Low headroom -> throttled replenish (10%)
        bucket.step(dt_s=1.0, thermal_headroom_c=3.0)
        self.assertAlmostEqual(bucket.tokens, 11.0)
        
        # Zero headroom -> no replenish
        bucket.step(dt_s=1.0, thermal_headroom_c=0.0)
        self.assertAlmostEqual(bucket.tokens, 11.0)

class TestSafetyFallback(unittest.TestCase):
    def test_mmu_freeze_trigger(self):
        fallback = SafetyFallback()
        self.assertFalse(fallback.mmu_frozen)
        
        # Below critical temp
        triggered = fallback.check_and_trigger(temperature_c=80.0, thermal_critical_c=95.0)
        self.assertFalse(triggered)
        self.assertFalse(fallback.mmu_frozen)
        
        # Above critical temp
        triggered = fallback.check_and_trigger(temperature_c=96.0, thermal_critical_c=95.0)
        self.assertTrue(triggered)
        self.assertTrue(fallback.mmu_frozen)
        self.assertEqual(fallback.emergency_evacuations, 1)

if __name__ == "__main__":
    unittest.main()
