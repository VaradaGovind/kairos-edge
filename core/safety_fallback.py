class SafetyFallback:
    """
    L2-C4: Safety fallback mode (MMU freeze and DMA evacuation).
    """
    def __init__(self):
        self.mmu_frozen = False
        self.emergency_evacuations = 0

    def check_and_trigger(self, temperature_c: float, thermal_critical_c: float = 95.0) -> bool:
        """
        Checks if hardware safety limits are breached and triggers MMU freeze.
        """
        if temperature_c >= thermal_critical_c:
            if not self.mmu_frozen:
                self.mmu_frozen = True
                self.emergency_evacuations += 1
            return True
        else:
            self.mmu_frozen = False
            return False
